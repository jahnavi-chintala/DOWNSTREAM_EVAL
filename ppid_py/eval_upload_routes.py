"""Browser UI: upload USDM + PIPD JSON, run eval, download JSON/YAML/DOCX."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from eval_scenario1 import NUM_CATEGORIES, classify_failures as pipd_classify_failures
from pipd_eval_config import category_weights_and_names, load_pipd_eval_config
from protocol_study_id import extract_protocol_study_id
from run_eval import run_eval

_PKG = Path(__file__).resolve().parent


def _first_existing(candidates) -> Path | None:
    """Return the first candidate path that is a real file, or None."""
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return p
    return None


# Hard-coded ground-truth lookups. Each tuple is tried in order; the first
# file that exists on disk is used so the eval can run even when the UI user
# doesn't upload the ground-truth CSV.
_DEFAULT_GT_CANDIDATES = (
    _PKG / "data" / "pipd_ground_truth_clean.csv",
    _PKG / "data" / "pipd_ground_truth.csv",
    _PKG.parent / "pipd_ground_truth.csv",
)
_DEFAULT_DEV_CANDIDATES = (
    _PKG / "data" / "deviation_subcategories_clean.csv",
    _PKG / "data" / "deviation_subcategories.csv",
    _PKG.parent / "deviation_subcategories.csv",
)
_DEFAULT_GT = _first_existing(_DEFAULT_GT_CANDIDATES) or _DEFAULT_GT_CANDIDATES[0]
_DEFAULT_DEV = _first_existing(_DEFAULT_DEV_CANDIDATES) or _DEFAULT_DEV_CANDIDATES[0]

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SEC = 7200


def _ensure_yaml_from_json(json_path: Path, yaml_path: Path) -> None:
    """If YAML is missing but JSON exists, write a YAML mirror (e.g. PIPD scenario 2)."""
    if yaml_path.is_file() or not json_path.is_file():
        return
    try:
        import yaml  # type: ignore
    except ImportError:
        return
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        yaml_path.write_text(
            yaml.safe_dump(
                data,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _fmt_pct(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, float) and 0.0 <= val <= 1.0:
        return f"{val * 100:.1f}%"
    return str(val)


def _near_misses_by_category_preview(result: Dict[str, Any]) -> list[dict[str, Any]]:
    """
    One row per category 1..NUM_CATEGORIES with algorithmic M1 near-miss pairs
    (generated_text vs gt_text) and optional semantic-review LLM pairings.
    """
    _, cat_names = category_weights_and_names(load_pipd_eval_config())
    raw = result.get("near_misses") or []
    by_cat: dict[int, list[dict[str, Any]]] = {i: [] for i in range(1, NUM_CATEGORIES + 1)}
    for nm in raw:
        if not isinstance(nm, dict):
            continue
        try:
            cn = int(nm.get("category_num") or 0)
        except (TypeError, ValueError):
            continue
        if cn not in by_cat:
            continue
        by_cat[cn].append(
            {
                "gt_text": nm.get("gt_text"),
                "generated_text": nm.get("generated_text"),
                "credit": nm.get("credit"),
                "root_cause": nm.get("root_cause"),
                "tier": nm.get("tier"),
                "distance": nm.get("distance"),
                "source": "algorithmic",
            }
        )

    sem_by_cat: dict[int, list[dict[str, Any]]] = {i: [] for i in range(1, NUM_CATEGORIES + 1)}
    sr = result.get("semantic_review") or {}
    if isinstance(sr, dict):
        for key, block in (sr.get("categories") or {}).items():
            try:
                cn = int(key)
            except (TypeError, ValueError):
                continue
            if cn not in sem_by_cat or not isinstance(block, dict):
                continue
            for p in block.get("pairings") or []:
                if not isinstance(p, dict):
                    continue
                reason = p.get("reason")
                sem_by_cat[cn].append(
                    {
                        "gt_text": p.get("gt_text"),
                        "generated_text": p.get("generated_text"),
                        "credit": p.get("credit"),
                        "verdict": p.get("verdict"),
                        "reason": (str(reason)[:400] if reason is not None else ""),
                        "source": "semantic_review",
                    }
                )

    out: list[dict[str, Any]] = []
    for cn in range(1, NUM_CATEGORIES + 1):
        cname = (cat_names.get(cn) or "").strip() or f"Category {cn}"
        algo = by_cat[cn]
        sem = sem_by_cat[cn]
        out.append(
            {
                "category_num": cn,
                "category_name": cname,
                "algorithmic_near_misses": algo,
                "semantic_review_pairs": sem,
                "total_pairs": len(algo) + len(sem),
            }
        )
    return out


def _pipd_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    sc = int(result.get("scenario") or 1)
    prev: Dict[str, Any] = {"product": "pipd", "scenario": sc, "headline": {}}
    if sc == 1:
        prev["headline"] = {
            "verdict": result.get("go_no_go"),
            "ta": result.get("ta"),
            "phase": result.get("phase"),
            "overall_score_percent": result.get("overall_score_percent"),
        }
        m = result.get("metrics") or {}
        m1 = m.get("m1_subcategory_recall") or {}
        m2 = m.get("m2_flag_accuracy") or {}
        m3 = m.get("m3_empty_category_accuracy") or {}
        m4 = m.get("m4_hallucination_detection") or {}
        m5 = m.get("m5_severity_match") or {}
        m6 = m.get("m6_gsop_coverage") or {}

        # M6 is N/A when the GT CSV has no gsop_codes column.
        m6_na = (m6.get("status") == "no_gt_gsop_column") or m6.get("score") is None
        m6_detail = "N/A — GT missing gsop_codes column" if m6_na else _fmt_pct(m6.get("score"))

        prev["metric_rows"] = [
            {
                "metric": "Overall score",
                "detail": (
                    f"{result['overall_score_percent']:.1f}%"
                    if result.get("overall_score_percent") is not None
                    else "—"
                ),
                "pass": bool(result.get("overall_pass")),
                "tooltip": (
                    "Weighted aggregate (M1 F1 50% · M2 15% · M3 10% · M4 15% · M5 5% · M6 5%), "
                    "renormalised when a metric is N/A. Matches the 'Final composite' line in the DOCX report."
                ),
                "hero": True,
            },
            {
                "metric": "M1 Subcategory recall",
                "detail": _fmt_pct(m1.get("score")),
                "pass": bool(m1.get("pass")),
                "tooltip": (
                    "Share of GT subcategory lines the generator reproduced, weighted by match quality: "
                    "verbatim 1.0 · numbering-error / criterion-format 0.99 · truncation 0.60 · paraphrase 0.75 · "
                    "LLM-confirmed semantic equivalent 0.85. F1 target 0.70 · recall target 0.75."
                ),
            },
            {
                "metric": "M2 Flag (auto_confirmed) accuracy",
                "detail": _fmt_pct(m2.get("auto_confirmed_accuracy")),
                "pass": bool(m2.get("pass")),
                "tooltip": "Fraction of matched auto_confirmed rows whose include_in_csr flag matches GT.",
            },
            {
                "metric": "M3 Empty-category accuracy",
                "detail": _fmt_pct(m3.get("score")),
                "pass": bool(m3.get("pass")),
                "tooltip": "Agreement on which of the 11 categories are empty (none_identified).",
            },
            {
                "metric": "M4 Provenance defects (USDM)",
                "detail": str(m4.get("hallucinations_found", "—")),
                "pass": bool(m4.get("pass")),
                "tooltip": (
                    "Generated subcategories whose usdm_entity_id is null or not in the protocol USDM JSON. "
                    "Different from M1 'extras' (extras are lines with no matching GT row — content precision; "
                    "M4 is provenance: the USDM reference itself is missing or fabricated)."
                ),
            },
            {
                "metric": "M5 Severity match",
                "detail": _fmt_pct(m5.get("score")),
                "pass": bool(m5.get("pass")),
                "tooltip": "Confidence-tier vs GT rationale_if_no (exact 1.0 · one tier 0.5 · else 0).",
            },
            {
                "metric": "M6 GSOP coverage",
                "detail": m6_detail,
                "pass": None if m6_na else bool(m6.get("pass")),
                "tooltip": (
                    "GSOP codes should come from a SOP reference table (GT / USDM / registry), not be "
                    "freely generated. When the GT CSV lacks a gsop_codes column M6 is reported as N/A "
                    "and excluded from the overall aggregate."
                ),
                "na": m6_na,
            },
        ]

        # Summarise semantic-review results (if auto-enabled via OPENAI_API_KEY).
        sr = result.get("semantic_review") or {}
        sr_applied = bool(sr.get("applied_to_m1"))
        sr_pairs = 0
        for cat_block in (sr.get("categories") or {}).values():
            sr_pairs += len(cat_block.get("pairings") or [])

        notes: list[str] = []
        for item in pipd_classify_failures(result)[:12]:
            if isinstance(item, dict):
                ft = str(item.get("failure_type") or item.get("metric") or "—")
                ex = str(item.get("example") or item.get("detail") or "")[:520]
                line = f"{ft}: {ex}" if ex else ft
                notes.append(line)
        prev["failure_notes"] = notes
        prev["counts"] = {
            "near_misses": len(result.get("near_misses") or []),
            "semantic_review_pairs": sr_pairs,
            "semantic_review_applied_to_m1": sr_applied,
        }
        prev["near_misses_by_category"] = _near_misses_by_category_preview(result)
    else:
        ov = result.get("overall_verdict") or {}
        rag = ov.get("verdict") if isinstance(ov, dict) else str(ov)
        prev["headline"] = {
            "verdict": result.get("go_no_go"),
            "rag_traffic_light": rag,
            "ta": result.get("ta"),
            "phase": result.get("phase"),
        }
        signals = result.get("signals") or {}
        rows = []
        for sid, blk in signals.items():
            st = str(blk.get("status") or "")
            msg = str(blk.get("message") or "")[:180]
            detail = st + (f" — {msg}" if msg else "")
            rows.append({"metric": str(sid), "detail": detail, "pass": st == "PASS"})
        prev["metric_rows"] = rows
        prev["failure_notes"] = []
        if isinstance(ov, dict):
            for note in (ov.get("remediation_notes") or [])[:8]:
                prev["failure_notes"].append(str(note)[:500])
        prev["counts"] = {"human_review_items": int(result.get("human_review_count") or 0)}
    prev["doc_hint"] = "Download DOCX below for the full report (narrative and tables)."
    return prev


def _norm_prefix(route_prefix: str) -> str:
    return (route_prefix or "").strip().rstrip("/")


def _dl(base: str, tail: str) -> str:
    tail = tail if tail.startswith("/") else f"/{tail}"
    return f"{base}{tail}" if base else tail


_PIPD_UI_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>PIPD Eval</title>
<style>
body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem}
label{display:block;margin-top:.9rem;font-weight:600}
button{margin-top:1.2rem;padding:.5rem 1rem}
#out{margin-top:1.5rem}
#out .err{white-space:pre-wrap;background:#fee;padding:1rem;border-radius:6px}
#out .pv{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;margin-bottom:1rem}
#out .pv h3{margin:0 0 .5rem;font-size:1rem}
#out table.mt{width:100%;border-collapse:collapse;font-size:.9rem}
#out table.mt th,#out table.mt td{border:1px solid #e2e8f0;padding:.35rem .5rem;text-align:left}
#out table.mt tr.pass td:first-child{border-left:3px solid #16a34a}
#out table.mt tr.fail td:first-child{border-left:3px solid #dc2626}
#out ul.notes{margin:.4rem 0;padding-left:1.2rem;font-size:.88rem}
#out .dl{margin-top:.75rem}
#out a.dlbtn{display:inline-block;margin:.25rem .5rem 0 0;padding:.35rem .65rem;background:#1d4ed8;color:#fff;border-radius:6px;text-decoration:none;font-size:.88rem}
#out a.dlbtn:hover{background:#1e40af}
#out .ap{margin-top:1rem;border:1px solid #e2e8f0;border-radius:8px;background:#fff;overflow:hidden}
#out .ap h4{margin:0;padding:.6rem .75rem;background:#f1f5f9;font-size:.95rem;border-bottom:1px solid #e2e8f0}
#out .ap .tabs{display:flex;flex-wrap:wrap;gap:0;border-bottom:1px solid #e2e8f0;background:#f8fafc}
#out .ap .tabs button{margin:0;border:none;background:transparent;padding:.5rem .9rem;cursor:pointer;font-size:.85rem;border-bottom:2px solid transparent}
#out .ap .tabs button.on{border-bottom-color:#1d4ed8;font-weight:600}
#out .ap .panel{display:none;max-height:28rem;overflow:auto;padding:.75rem;font-size:.82rem}
#out .ap .panel.on{display:block}
#out .ap pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,monospace;font-size:.78rem}
#out .ap .docx-preview{font-family:Georgia,serif;line-height:1.45;font-size:.9rem}
#out .ap .docx-preview table{border-collapse:collapse;margin:.5rem 0}
#out .ap .docx-preview td,#out .ap .docx-preview th{border:1px solid #ccc;padding:.2rem .4rem}
</style>
<script src="https://cdn.jsdelivr.net/npm/mammoth@1.6.0/mammoth.browser.min.js"></script>
</head><body>
<h1>PIPD evaluation</h1>
<p>USDM and PIPD JSON must list the <strong>same study id</strong>. Ground truth defaults to packaged <code>data/</code>.</p>
<form id="f">
<label>USDM JSON<input type="file" name="usdm" accept=".json,application/json" required></label>
<label>PIPD JSON<input type="file" name="gen" accept=".json,application/json" required></label>
<label>pipd_ground_truth CSV (optional)<input type="file" name="gt" accept=".csv"></label>
<label>deviation_subcategories CSV (optional)<input type="file" name="dev" accept=".csv"></label>
<button type="submit">Run eval</button>
</form>
<div id="out"></div>
<script>
const P = __PREFIX__;
function u(path) { return (P || '') + path; }
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}
function artifactPreviewsShell() {
  return '<div class="ap" id="artifact-previews-wrap"><h4>File previews (JSON · YAML · DOCX)</h4><p class="desc" style="padding:.5rem .75rem;margin:0">Loading…</p></div>';
}
async function loadArtifactPreviews(j) {
  const wrap = document.getElementById('artifact-previews-wrap');
  if (!wrap || !j.downloads) return;
  let jsonStr = '';
  let yamlText = '';
  let yamlWarn = '';
  let docxHtml = '';
  let docxExtra = '';
  let jsonErr = '';
  try {
    const jsonRes = await fetch(j.downloads.json);
    if (!jsonRes.ok) jsonErr = 'JSON HTTP ' + jsonRes.status;
    else jsonStr = JSON.stringify(await jsonRes.json(), null, 2);
  } catch (e) { jsonErr = String(e); }
  if (jsonErr) {
    wrap.innerHTML = '<h4>File previews</h4><div class="err" style="margin:.75rem">' + esc(jsonErr) + '</div>';
    return;
  }
  try {
    const yamlRes = await fetch(j.downloads.yaml);
    if (!yamlRes.ok) {
      yamlWarn = 'No YAML file for this run (HTTP ' + yamlRes.status + '). The JSON tab has the same data; some scenarios only emit JSON until a fallback is applied.';
    } else {
      yamlText = await yamlRes.text();
    }
  } catch (e) { yamlWarn = String(e); }
  try {
    const tok = encodeURIComponent(j.session_token || '');
    const serverHtmlUrl = u('/eval/session/' + tok + '/preview/docx-html');
    const docxRes = await fetch(j.downloads.docx);
    if (!docxRes.ok) {
      docxExtra = 'No Word file for this run (HTTP ' + docxRes.status + '). Use JSON/YAML or downloads above.';
    } else {
      let converted = false;
      if (typeof mammoth !== 'undefined') {
        try {
          const buf = await docxRes.arrayBuffer();
          const conv = await mammoth.convertToHtml({arrayBuffer: buf});
          docxHtml = conv.value;
          converted = true;
          if (conv.messages && conv.messages.length)
            docxExtra = conv.messages.map(function(m) { return m.message; }).join('; ');
        } catch (e) {
          docxExtra = 'Browser DOCX conversion failed: ' + String(e);
        }
      }
      if (!converted) {
        try {
          const sr = await fetch(serverHtmlUrl);
          if (sr.ok) {
            docxHtml = await sr.text();
            docxExtra = (docxExtra ? docxExtra + ' ' : '') + '[Rendered on server]';
          } else if (sr.status === 501) {
            docxExtra = (docxExtra ? docxExtra + ' ' : '') + 'Run: pip install mammoth (same Python env as uvicorn).';
          } else if (!docxExtra) {
            docxExtra = 'Server preview HTTP ' + sr.status;
          }
        } catch (e) {
          if (!docxExtra) docxExtra = String(e);
        }
      }
    }
  } catch (e) {
    docxExtra = String(e);
  }
  const yamlPanel = yamlWarn ? '<pre>' + esc(yamlWarn) + '</pre>' : '<pre>' + esc(yamlText) + '</pre>';
  wrap.innerHTML = '<h4>File previews (JSON · YAML · DOCX)</h4>' +
    '<div class="tabs">' +
    '<button type="button" class="on" data-tab="pj">JSON</button>' +
    '<button type="button" data-tab="py">YAML</button>' +
    '<button type="button" data-tab="pw">DOCX (HTML)</button>' +
    '</div>' +
    '<div class="panel on" id="pj"><pre>' + esc(jsonStr) + '</pre></div>' +
    '<div class="panel" id="py">' + yamlPanel + '</div>' +
    '<div class="panel" id="pw"><p class="desc" style="font-size:.8rem;margin:0 0 .5rem">DOCX via browser or server; layout may differ from Word. Open the downloaded .docx for the official file.</p>' +
    (docxExtra ? '<p class="desc" style="font-size:.78rem;color:#64748b;margin:0 0 .5rem">' + esc(docxExtra) + '</p>' : '') +
    '<div class="docx-preview">' + docxHtml + '</div></div>';
  wrap.querySelectorAll('.tabs button').forEach(function(btn) {
    btn.onclick = function() {
      wrap.querySelectorAll('.tabs button').forEach(function(b) { b.classList.remove('on'); });
      wrap.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('on'); });
      btn.classList.add('on');
      wrap.querySelector('#' + btn.getAttribute('data-tab')).classList.add('on');
    };
  });
}
function renderPreview(j) {
  const p = j.preview;
  if (!p) {
    let html = '<p class="desc">No summary preview.</p><p class="dl"><strong>Downloads</strong></p>';
    html += '<a class="dlbtn" href="' + esc(j.downloads.zip) + '">ZIP (all)</a>';
    html += '<a class="dlbtn" href="' + esc(j.downloads.json) + '">JSON</a>';
    html += '<a class="dlbtn" href="' + esc(j.downloads.yaml) + '">YAML</a>';
    html += '<a class="dlbtn" href="' + esc(j.downloads.docx) + '">DOCX</a>';
    html += artifactPreviewsShell();
    return html;
  }
  const h = p.headline || {};
  let html = '<div class="pv"><h3>Results preview</h3>';
  html += '<p><strong>Study:</strong> ' + esc(j.study_id) + ' · <strong>GO/NO-GO:</strong> ' + esc(h.verdict||j.verdict) + '</p>';
  if (h.rag_traffic_light) html += '<p><strong>Signal verdict:</strong> ' + esc(h.rag_traffic_light) + '</p>';
  if (h.ta) html += '<p><strong>TA:</strong> ' + esc(h.ta) + (h.phase ? ' · <strong>Phase:</strong> ' + esc(h.phase) : '') + '</p>';
  if (p.metric_rows && p.metric_rows.length) {
    html += '<table class="mt"><thead><tr><th>Metric / signal</th><th>Detail</th><th>Pass</th></tr></thead><tbody>';
    for (const row of p.metric_rows) {
      const ok = row.pass !== false;
      html += '<tr class="' + (ok ? 'pass' : 'fail') + '"><td>' + esc(row.metric) + '</td><td>' + esc(row.detail) + '</td><td>' + (ok ? 'Yes' : 'No') + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  if (p.counts && Object.keys(p.counts).length) {
    html += '<p style="font-size:.88rem;margin:.5rem 0 0">' + esc(JSON.stringify(p.counts)) + '</p>';
  }
  if (p.failure_notes && p.failure_notes.length) {
    html += '<p style="margin:.6rem 0 .2rem;font-weight:600">Notes</p><ul class="notes">';
    for (const n of p.failure_notes) html += '<li>' + esc(n) + '</li>';
    html += '</ul>';
  }
  if (p.doc_hint) html += '<p style="font-size:.85rem;color:#475569;margin:.6rem 0 0">' + esc(p.doc_hint) + '</p>';
  html += '</div>';
  html += '<p class="dl"><strong>Downloads</strong></p>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.zip) + '">ZIP (all)</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.json) + '">JSON</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.yaml) + '">YAML</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.docx) + '">DOCX</a>';
  html += artifactPreviewsShell();
  return html;
}
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const out = document.getElementById('out');
  out.innerHTML = '<div class="err">Running…</div>';
  const fd = new FormData(e.target);
  const r = await fetch(u('/eval/upload-session'), { method: 'POST', body: fd });
  const j = await r.json();
  if (!r.ok) { out.innerHTML = '<div class="err">' + esc(JSON.stringify(j, null, 2)) + '</div>'; return; }
  out.innerHTML = '<p><b>Done.</b></p>' + renderPreview(j);
  loadArtifactPreviews(j);
};
</script>
</body></html>"""


def _cleanup_sessions() -> None:
    now = time.time()
    for key, meta in list(SESSIONS.items()):
        if now - float(meta.get("t", 0)) > SESSION_TTL_SEC:
            wd = meta.get("workdir")
            if wd:
                shutil.rmtree(wd, ignore_errors=True)
            SESSIONS.pop(key, None)


def register_eval_upload_routes(app, *, route_prefix: str = "") -> None:
    base = _norm_prefix(route_prefix)
    router = APIRouter(prefix=route_prefix.strip() or "", tags=["upload-ui"])

    @router.get("/ui", response_class=HTMLResponse)
    async def eval_ui_page() -> str:
        return _PIPD_UI_SHELL.replace("__PREFIX__", json.dumps(base))

    @router.post("/eval/upload-session")
    async def eval_upload_session(
        usdm: UploadFile = File(...),
        gen: UploadFile = File(...),
        gt: UploadFile | None = File(None),
        dev: UploadFile | None = File(None),
    ) -> Dict[str, Any]:
        _cleanup_sessions()
        work = Path(tempfile.mkdtemp(prefix="pipd_eval_"))
        usdm_path = work / "usdm.json"
        gen_path = work / "generator.json"
        usdm_path.write_bytes(await usdm.read())
        gen_path.write_bytes(await gen.read())
        try:
            usdm_obj = json.loads(usdm_path.read_text(encoding="utf-8"))
            gen_obj = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

        sid_u = extract_protocol_study_id(usdm_obj)
        sid_g = extract_protocol_study_id(gen_obj)
        if not sid_u or not sid_g:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Could not read study id from both files (usdm={sid_u!r}, generator={sid_g!r}).",
            )
        if sid_u.strip().upper() != sid_g.strip().upper():
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Study id mismatch: USDM={sid_u} vs generator={sid_g}.",
            )
        study_id = sid_g.strip()

        if gt and gt.filename:
            gt_path = work / "pipd_ground_truth.csv"
            gt_path.write_bytes(await gt.read())
        else:
            resolved = _first_existing(_DEFAULT_GT_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No PIPD ground-truth CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_GT_CANDIDATES)
                    ),
                )
            gt_path = resolved

        if dev and dev.filename:
            dev_path = work / "deviation_subcategories.csv"
            dev_path.write_bytes(await dev.read())
        else:
            resolved = _first_existing(_DEFAULT_DEV_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No deviation subcategories CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_DEV_CANDIDATES)
                    ),
                )
            dev_path = resolved

        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_out = out_dir / f"pipd_eval_{study_id}.json"

        # Auto-enable the OpenAI semantic review when an API key is configured
        # on the server. This is how the UI catches missed-vs-extra pairs that
        # are clearly the same deviation but token-overlap too low for
        # ``find_paraphrase_pairs`` (e.g. "Participant randomized after the
        # 5th day of COVID-19 symptom onset" <-> "Randomized out of protocol
        # defined window"). Credits flow straight into M1 so the headline
        # score reflects the semantic match.
        has_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
        result = run_eval(
            generator_json_path=str(gen_path),
            ground_truth_csv_path=str(gt_path),
            deviation_benchmarks_path=str(dev_path),
            study_id=study_id,
            output_path=str(json_out),
            output_dir=str(out_dir),
            usdm_json_path=str(usdm_path),
            write_report_yaml=True,
            write_report_docx=True,
            with_semantic_review=has_openai,
            semantic_review_affects_m1=has_openai,
        )

        yaml_p = out_dir / f"pipd_eval_{study_id}.yaml"
        docx_p = out_dir / f"PIPD_Eval_Report_{study_id}.docx"

        _ensure_yaml_from_json(json_out, yaml_p)

        if int(result.get("scenario") or 1) == 2 and not docx_p.is_file():
            try:
                from pipd_markdown_to_docx import write_docx_from_markdown

                ov = result.get("overall_verdict") or {}
                md = (
                    f"# PIPD evaluation (scenario 2)\n\n**Study:** {study_id}\n\n"
                    f"**GO / NO-GO:** {result.get('go_no_go', '')}\n\n"
                    f"**Traffic light:** {ov.get('verdict', '')}\n\n"
                    "## Proxy signals\n\n"
                )
                for sid, blk in sorted((result.get("signals") or {}).items()):
                    md += f"- **{sid}** — {blk.get('status', '')}: {blk.get('message', '')}\n"
                n_rev = int(result.get("human_review_count") or 0)
                if n_rev:
                    md += f"\n**Human review items:** {n_rev}\n"
                md += "\n_Full machine-readable output is in the JSON download._\n"
                write_docx_from_markdown(md, str(docx_p), reference_eval=False)
            except Exception:
                pass

        token = secrets.token_urlsafe(24)
        verdict = str(result.get("go_no_go") or "")
        preview = _pipd_preview(result)
        SESSIONS[token] = {
            "t": time.time(),
            "workdir": str(work),
            "study_id": study_id,
            "preview": preview,
            "paths": {
                "json": str(json_out) if json_out.is_file() else "",
                "yaml": str(yaml_p) if yaml_p.is_file() else "",
                "docx": str(docx_p) if docx_p.is_file() else "",
            },
        }

        return {
            "study_id": study_id,
            "verdict": verdict,
            "preview": preview,
            "session_token": token,
            "downloads": {
                "zip": _dl(base, f"/eval/session/{token}/bundle.zip"),
                "json": _dl(base, f"/eval/session/{token}/file/json"),
                "yaml": _dl(base, f"/eval/session/{token}/file/yaml"),
                "docx": _dl(base, f"/eval/session/{token}/file/docx"),
            },
        }

    @router.get("/eval/session/{token}/bundle.zip")
    async def session_bundle(token: str) -> StreamingResponse:
        meta = SESSIONS.get(token)
        if not meta:
            raise HTTPException(status_code=404, detail="Unknown or expired session.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for _, p in meta["paths"].items():
                if p and Path(p).is_file():
                    zf.write(p, arcname=Path(p).name)
        buf.seek(0)
        sid = meta.get("study_id", "study")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="pipd_eval_{sid}.zip"'},
        )

    @router.get("/eval/session/{token}/file/{kind}")
    async def session_file(token: str, kind: str) -> FileResponse:
        meta = SESSIONS.get(token)
        if not meta:
            raise HTTPException(status_code=404, detail="Unknown or expired session.")
        key = kind.lower()
        if key not in ("json", "yaml", "docx"):
            raise HTTPException(status_code=400, detail="kind must be json, yaml, or docx")
        p = meta["paths"].get(key) or ""
        if not p or not Path(p).is_file():
            raise HTTPException(status_code=404, detail=f"Artifact {key} not available.")
        return FileResponse(p, filename=Path(p).name)

    @router.get("/eval/session/{token}/preview/docx-html", response_class=HTMLResponse)
    async def session_docx_html_preview(token: str) -> HTMLResponse:
        meta = SESSIONS.get(token)
        if not meta:
            raise HTTPException(status_code=404, detail="Unknown or expired session.")
        p = meta["paths"].get("docx") or ""
        if not p or not Path(p).is_file():
            raise HTTPException(status_code=404, detail="DOCX not available for this run.")
        try:
            import mammoth  # type: ignore
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="Install mammoth on the server: pip install mammoth",
            ) from None
        try:
            with open(p, "rb") as docx_f:
                conv = mammoth.convert_to_html(docx_f)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"DOCX conversion failed: {exc}") from exc
        style = (
            "<style>.docx-preview table{border-collapse:collapse}"
            ".docx-preview td,.docx-preview th{border:1px solid #ccc;padding:2px 6px}</style>"
        )
        return HTMLResponse(content=f"{style}<div class=\"docx-preview\">{conv.value}</div>")

    app.include_router(router)
