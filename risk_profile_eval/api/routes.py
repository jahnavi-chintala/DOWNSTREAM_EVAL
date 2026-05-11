"""Browser UI: upload USDM + Risk Profile JSON, run eval, download JSON/YAML/DOCX."""

from __future__ import annotations

import io
import json
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse

from core.eval_scenario1 import classify_failures
from utils.miss_explanation import write_miss_explanation as rp_write_miss_explanation
from utils.protocol_study_id import extract_protocol_study_id
from scripts.run_eval import run_eval

_PKG = Path(__file__).resolve().parent
def _first_existing(candidates) -> Path | None:
    """Return the first candidate path that is a real file, or None."""
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return p
    return None


# Ground-truth files are hard-coded so the evaluation can run even when the
# user doesn't upload them. Each constant is a tuple of candidates tried in
# order; the first existing path wins at request time.
_DEFAULT_RISKS_CANDIDATES = (
    _PKG.parent / "data" / "risk_profile_ground_truth.csv",
    _PKG.parent.parent / "risk_profile_ground_truth.csv",
)
_DEFAULT_FACTORS_CANDIDATES = (
    _PKG.parent / "data" / "critical_factors_ground_truth.csv",
    _PKG.parent.parent / "critical_factors_ground_truth.csv",
)
_DEFAULT_RISKS = _first_existing(_DEFAULT_RISKS_CANDIDATES) or _DEFAULT_RISKS_CANDIDATES[0]
_DEFAULT_FACTORS = _first_existing(_DEFAULT_FACTORS_CANDIDATES) or _DEFAULT_FACTORS_CANDIDATES[0]

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SEC = 7200


def _ensure_yaml_from_json(json_path: Path, yaml_path: Path) -> None:
    """If YAML is missing but JSON exists, write a YAML mirror for download/preview."""
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


def _risk_preview(result: Dict[str, Any]) -> Dict[str, Any]:
    sc = int(result.get("scenario") or 1)
    prev: Dict[str, Any] = {
        "product": "risk_profile",
        "scenario": sc,
        "headline": {
            "verdict": result.get("verdict"),
            "ta": result.get("ta"),
            "phase": result.get("phase"),
        },
    }
    if sc == 1:
        m = result.get("metrics") or {}
        m1 = m.get("m1_risk_name_recall") or {}
        m2 = m.get("m2_rpn_tier_accuracy") or {}
        m3 = m.get("m3_critical_factor_match") or {}
        m4 = m.get("m4_hallucination_detection") or {}
        oos = m4.get("out_of_scope_not_penalized") or []

        # Weighted overall: M1 40% · M2 20% (skip → drop) · M3 20% · M4 20%
        # M4 uses a penalty of 1.0 when zero provenance defects, else 0.
        _m1s = float(m1.get("score") or 0.0)
        _m2s = None if m2.get("skipped") else float(m2.get("score") or 0.0)
        _m3s = None if m3.get("skipped") else float(m3.get("score") or 0.0)
        _defects = int(m4.get("provenance_defect_count") or m4.get("hallucinations_found") or 0)
        _m4s = 1.0 if _defects == 0 else max(0.0, 1.0 - 0.15 * _defects)
        _pairs = [
            (0.40, _m1s),
            (0.20, _m2s),
            (0.20, _m3s),
            (0.20, _m4s),
        ]
        _active = [(w, v) for (w, v) in _pairs if v is not None]
        _wsum = sum(w for w, _v in _active) or 1.0
        _overall = 100.0 * sum(w * v for w, v in _active) / _wsum
        prev["headline"]["overall_score_percent"] = round(_overall, 1)

        prev["metric_rows"] = [
            {
                "metric": "Overall score",
                "detail": f"{_overall:.1f}%",
                "pass": bool(str(result.get("verdict") or "").upper() == "GO"),
                "hero": True,
                "tooltip": (
                    "Weighted aggregate: M1 Risk recall 40% · M2 RPN tier 20% · M3 Critical factors 20% · "
                    "M4 provenance 20%. Skipped metrics drop out and weights renormalise."
                ),
            },
            {
                "metric": "M1 Risk name recall",
                "detail": _fmt_pct(m1.get("score")),
                "pass": bool(m1.get("passed")),
                "tooltip": "Share of GT risk names the generator produced (verbatim + near-miss credit).",
            },
            {
                "metric": "M2 RPN tier (±1)",
                "detail": "SKIP" if m2.get("skipped") else _fmt_pct(m2.get("score")),
                "pass": True if m2.get("skipped") else bool(m2.get("passed")),
                "tooltip": "Agreement on Risk Priority Number tier within ±1 band. Skipped when no benchmark RPNs available.",
            },
            {
                "metric": "M3 Critical factors",
                "detail": "SKIP" if m3.get("skipped") else _fmt_pct(m3.get("score")),
                "pass": True if m3.get("skipped") else bool(m3.get("passed")),
                "tooltip": "Critical factor coverage vs GT.",
            },
            {
                "metric": "M4 Traceability flags",
                "detail": str(m4.get("traceability_flag_count", m4.get("provenance_defect_count", m4.get("hallucinations_found", "—")))),
                "pass": bool(m4.get("passed")),
                "tooltip": (
                    "USDM traceability flags: asserted usdm_id missing from protocol, or entity type "
                    "absent from protocol when no resolvable id/name match. Type-only trace "
                    "(instanceType present, no concrete id) matches PIPD handover and is not penalized."
                ),
            },
            {
                "metric": "Out-of-scope (not penalized)",
                "detail": str(len(oos)),
                "pass": True,
                "tooltip": (
                    "Rows classified by LLM as not predictable from protocol text "
                    "(operational/runtime/system issues); excluded from M4 penalties."
                ),
            },
        ]
        notes: list[str] = []
        for item in classify_failures(result)[:12]:
            if isinstance(item, dict):
                line = str(item.get("metric") or "—")
                det = str(item.get("detail") or "")[:650]
                if det:
                    line += ": " + det
                notes.append(line)
        prev["failure_notes"] = notes
        prev["counts"] = {
            "near_misses": len(m1.get("near_misses") or []),
            "out_of_scope_not_penalized": len(oos),
        }

        # Surface USDM-backed tracing details so the UI can render a
        # per-reference table ("why did M4 flag this?").
        trace_meta = result.get("usdm_trace") or {}
        tracing_rows: list[dict] = []
        status_counter: Dict[str, int] = {
            "id_match": 0,
            "name_match": 0,
            "type_only": 0,
            "unresolved": 0,
            "no_usdm": 0,
        }
        for item in (m4.get("flagged_fields") or []):
            if not isinstance(item, dict):
                continue
            status = item.get("trace_status")
            if status:
                status_counter[status] = status_counter.get(status, 0) + 1
                tracing_rows.append({
                    "field_path": item.get("field_path"),
                    "entity": (item.get("value") or {}).get("entity"),
                    "signal": (item.get("value") or {}).get("signal"),
                    "usdm_id": (item.get("value") or {}).get("usdm_id"),
                    "status": status,
                    "reason": item.get("reason"),
                    "candidates": item.get("candidates") or [],
                    "matched_node_id": item.get("matched_node_id"),
                })
        prev["tracing"] = {
            "usdm_loaded": bool(trace_meta.get("usdm_loaded")),
            "usdm_node_count": int(trace_meta.get("usdm_node_count") or 0),
            "usdm_instance_types": trace_meta.get("usdm_instance_types") or [],
            "status_counts": status_counter,
            "rows": tracing_rows[:200],
            "truncated": len(tracing_rows) > 200,
        }
    else:
        signals = result.get("signals") or {}
        # Compute / surface the weighted signal-health score.
        health = result.get("signal_health") or {}
        if not health:
            try:
                from reports.rp_scenario2_report import compute_signal_health

                health = compute_signal_health(signals)
            except Exception:
                health = {}
        score_pct = health.get("percent")
        if score_pct is None:
            score_pct = float(result.get("overall_score_percent") or 0.0)
        prev["headline"]["overall_score_percent"] = round(float(score_pct or 0.0), 1)

        _titles = {
            "S1": "S1 Hallucination Check",
            "S2": "S2 RPN Confidence Distribution",
            "S3": "S3 Risk Count Sanity",
            "S4": "S4 USDM Traceability",
            "S5": "S5 Critical Factor Completeness",
            "S6": "S6 Placeholder ID Presence",
            "S7": "S7 RPN Formula Integrity",
        }
        _tips = {
            "S1": "Every risk must have non-empty usdm_drivers AND a non-null benchmark_source.",
            "S2": ">40% LOW confidence risks triggers WARN.",
            "S3": "Total risks must be 3–6 (floor=3, ceiling=6).",
            "S4": "Every risk must have usdm_traceability = FULL.",
            "S5": "Expected critical factors for TA/Phase must all be present.",
            "S6": "All risk_ids must be SR-PLACEHOLDER-XXX. No real IRMS IDs.",
            "S7": "impact × likelihood × detectability must equal rpn for every risk.",
        }
        rows = [{
            "metric": "Overall score",
            "detail": f"{prev['headline']['overall_score_percent']}%",
            "pass": str(result.get("verdict") or "").upper() in ("GREEN", "AMBER"),
            "hero": True,
            "tooltip": (
                "Weighted signal-health across S1–S7 (PASS=1 · WARN=0.5 · FAIL=0). "
                "GREEN if all pass, AMBER with warnings, RED on any fail."
            ),
        }]
        for sid in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
            blk = signals.get(sid) or {}
            st = str(blk.get("status") or "—").upper()
            rows.append({
                "metric": _titles.get(sid, sid),
                "detail": st,
                "pass": st == "PASS",
                "na": st == "—",
                "tooltip": _tips.get(sid, ""),
            })
        prev["metric_rows"] = rows
        vd = result.get("verdict_detail") or {}
        notes = []
        if vd.get("fail_signals"):
            notes.append("Failed: " + ", ".join(str(x) for x in vd["fail_signals"]))
        if vd.get("warn_signals"):
            notes.append("Warnings: " + ", ".join(str(x) for x in vd["warn_signals"]))
        prev["failure_notes"] = notes
        prev["counts"] = {
            "human_review_items": int(result.get("review_list_count") or 0),
            "signal_pass": int(health.get("pass") or 0),
            "signal_warn": int(health.get("warn") or 0),
            "signal_fail": int(health.get("fail") or 0),
        }
    prev["doc_hint"] = "Download DOCX below for the full stakeholder report (tables and narrative)."
    return prev


def _norm_prefix(route_prefix: str) -> str:
    return (route_prefix or "").strip().rstrip("/")


def _dl(base: str, tail: str) -> str:
    tail = tail if tail.startswith("/") else f"/{tail}"
    return f"{base}{tail}" if base else tail


_RISK_UI_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Risk Profile Eval</title>
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
<h1>Risk Profile evaluation</h1>
<p>USDM JSON and generator (Risk Profile) JSON must carry the <strong>same study id</strong>.
Ground truth CSVs default to packaged <code>data/</code> if omitted.</p>
<form id="f">
<label>USDM JSON<input type="file" name="usdm" accept=".json,application/json" required></label>
<label>Generator JSON<input type="file" name="gen" accept=".json,application/json" required></label>
<label>Ground truth risks CSV (optional)<input type="file" name="risks" accept=".csv,text/csv"></label>
<label>Ground truth factors CSV (optional)<input type="file" name="factors" accept=".csv,text/csv"></label>
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
  html += '<p><strong>Study:</strong> ' + esc(j.study_id) + ' · <strong>Verdict:</strong> ' + esc(h.verdict||j.verdict) + '</p>';
  if (h.ta) html += '<p><strong>TA:</strong> ' + esc(h.ta) + (h.phase ? ' · <strong>Phase:</strong> ' + esc(h.phase) : '') + '</p>';
  if (p.metric_rows && p.metric_rows.length) {
    html += '<table class="mt"><thead><tr><th>Metric</th><th>Value</th><th>Pass</th></tr></thead><tbody>';
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
        return _RISK_UI_SHELL.replace("__PREFIX__", json.dumps(base))

    @router.post("/eval/upload-session")
    async def eval_upload_session(
        usdm: UploadFile = File(...),
        gen: UploadFile = File(...),
        risks: UploadFile | None = File(None),
        factors: UploadFile | None = File(None),
    ) -> Dict[str, Any]:
        _cleanup_sessions()
        work = Path(tempfile.mkdtemp(prefix="rp_eval_"))
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

        if risks and risks.filename:
            risks_path = work / "risk_profile_ground_truth.csv"
            risks_path.write_bytes(await risks.read())
        else:
            resolved = _first_existing(_DEFAULT_RISKS_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No risks CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_RISKS_CANDIDATES)
                    ),
                )
            risks_path = resolved

        if factors and factors.filename:
            factors_path = work / "critical_factors_ground_truth.csv"
            factors_path.write_bytes(await factors.read())
        else:
            resolved = _first_existing(_DEFAULT_FACTORS_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No factors CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_FACTORS_CANDIDATES)
                    ),
                )
            factors_path = resolved

        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        result = run_eval(
            generator_json=str(gen_path),
            ground_truth_risks_csv=str(risks_path),
            ground_truth_factors_csv=str(factors_path),
            study_id=study_id,
            output_dir=str(out_dir),
            write_supplementary=False,
            usdm_json_path=str(usdm_path),
        )

        stem = f"risk_profile_eval_{study_id}"
        json_p = out_dir / f"{stem}.json"
        yaml_p = out_dir / f"{stem}.yaml"
        docx_p = out_dir / f"Risk_Profile_Eval_Report_{study_id}.docx"

        _ensure_yaml_from_json(json_p, yaml_p)

        miss_json_p: Path | None = None
        miss_md_p: Path | None = None
        try:
            paths = rp_write_miss_explanation(
                result,
                str(usdm_path.resolve()),
                out_dir,
                stem,
            )
            miss_json_p = paths["json"]
            miss_md_p = paths["md"]
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[WARN] Risk Profile miss explanation skipped: {exc}")

        token = secrets.token_urlsafe(24)
        preview = _risk_preview(result)
        SESSIONS[token] = {
            "t": time.time(),
            "workdir": str(work),
            "study_id": study_id,
            "preview": preview,
            "paths": {
                "json": str(json_p) if json_p.is_file() else "",
                "yaml": str(yaml_p) if yaml_p.is_file() else "",
                "docx": str(docx_p) if docx_p.is_file() else "",
                "miss_json": str(miss_json_p) if miss_json_p and miss_json_p.is_file() else "",
                "miss_md": str(miss_md_p) if miss_md_p and miss_md_p.is_file() else "",
            },
        }

        verdict = result.get("verdict", "")
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
                "miss_json": _dl(base, f"/eval/session/{token}/file/miss_json"),
                "miss_md": _dl(base, f"/eval/session/{token}/file/miss_md"),
            },
        }

    @router.get("/eval/session/{token}/bundle.zip")
    async def session_bundle(token: str) -> StreamingResponse:
        meta = SESSIONS.get(token)
        if not meta:
            raise HTTPException(status_code=404, detail="Unknown or expired session.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, p in meta["paths"].items():
                if p and Path(p).is_file():
                    zf.write(p, arcname=Path(p).name)
        buf.seek(0)
        sid = meta.get("study_id", "study")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="risk_profile_eval_{sid}.zip"'},
        )

    @router.get("/eval/session/{token}/file/{kind}")
    async def session_file(token: str, kind: str) -> FileResponse:
        meta = SESSIONS.get(token)
        if not meta:
            raise HTTPException(status_code=404, detail="Unknown or expired session.")
        key = kind.lower()
        if key not in ("json", "yaml", "docx", "miss_json", "miss_md"):
            raise HTTPException(
                status_code=400,
                detail="kind must be json, yaml, docx, miss_json, or miss_md",
            )
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
