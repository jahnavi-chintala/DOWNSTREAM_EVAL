"""Browser UI: upload USDM + CMP JSON, run eval, download JSON/YAML/DOCX."""

from __future__ import annotations

import io
import json
import os
import csv
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from reports.cmp_eval_report_yaml import write_cmp_eval_report_yaml
from core.eval_d3_cmp import (
    CONFIG_PATH,
    GroundTruth,
    _write_word_report_path,
    infer_study_id,
    load_config,
    run_eval as cmp_run_eval,
)
from core.eval_scenario2 import run_scenario2_eval as cmp_run_scenario2
from utils.miss_explanation import write_miss_explanation as cmp_write_miss_explanation

from utils.protocol_study_id import extract_protocol_study_id

_PKG = Path(__file__).resolve().parent.parent


def _first_existing(candidates) -> Path | None:
    """Return the first candidate path that is a real file, or None."""
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return p
    return None


# Hard-coded ground-truth lookups. Each tuple is tried in order so the eval
# runs even without UI uploads; casing variants cover Windows vs POSIX repos.
_DEFAULT_KRI_CANDIDATES = (
    _PKG / "Data" / "cmp_kri_ground_truth.csv",
    _PKG / "data" / "cmp_kri_ground_truth.csv",
    _PKG.parent / "cmp_kri_ground_truth.csv",
)
_DEFAULT_QTL_CANDIDATES = (
    _PKG / "Data" / "cmp_qtl_ground_truth.csv",
    _PKG / "data" / "cmp_qtl_ground_truth.csv",
    _PKG.parent / "cmp_qtl_ground_truth.csv",
)
_DEFAULT_META_CANDIDATES = (
    _PKG / "Data" / "cmp_study_metadata.csv",
    _PKG / "data" / "cmp_study_metadata.csv",
    _PKG.parent / "cmp_study_metadata.csv",
)
_DEFAULT_KRI = _first_existing(_DEFAULT_KRI_CANDIDATES) or _DEFAULT_KRI_CANDIDATES[0]
_DEFAULT_QTL = _first_existing(_DEFAULT_QTL_CANDIDATES) or _DEFAULT_QTL_CANDIDATES[0]
_DEFAULT_META = _first_existing(_DEFAULT_META_CANDIDATES) or _DEFAULT_META_CANDIDATES[0]

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SEC = 7200

# Keep CMP aligned with the same verify set used by PIPD/Risk.
VERIFY_STUDIES = {
    "B7981027",
    "C4891023",
    "C1071003",
    "C1071005",
    "C3651021",
    "C4591081",
    "C3671059",
    "C5091017",
}


def _ensure_yaml_from_json(json_path: Path, yaml_path: Path) -> None:
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


def _study_has_cmp_ground_truth(study_id: str, kri_path: Path, qtl_path: Path) -> bool:
    sid = str(study_id or "").strip().upper()
    if not sid:
        return False
    # Verify studies are always evaluated in Scenario 1 mode for CMP.
    if sid in VERIFY_STUDIES:
        return True
    for p in (kri_path, qtl_path):
        if not p.is_file():
            return False
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    row_sid = str(row.get("study_id") or row.get("study_folder") or "").strip().upper()
                    if row_sid == sid:
                        return True
        except OSError:
            return False
    return False


def _cmp_preview(report: Dict[str, Any]) -> Dict[str, Any]:
    if int(report.get("scenario") or 1) == 2:
        vd = report.get("verdict_detail") or {}
        sm = report.get("summary_metrics") or {}
        rows = [
            {
                "metric": "Overall score",
                "detail": (
                    f"{sm.get('signal_pass', 0)} PASS · "
                    f"{sm.get('signal_warn', 0)} WARN · "
                    f"{sm.get('signal_fail', 0)} FAIL"
                ),
                "pass": bool(sm.get("overall_pass")),
                "hero": True,
                "tooltip": "Scenario 2 proxy signal health (no study-level CMP ground truth available).",
            }
        ]
        for sid in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
            sig = (report.get("signals") or {}).get(sid) or {}
            st = str(sig.get("status") or "—").upper()
            rows.append(
                {
                    "metric": f"{sid} {sig.get('name', '')}".strip(),
                    "detail": st,
                    "pass": st in ("PASS", "WARN"),
                    "tooltip": sig.get("description", ""),
                }
            )
        notes = []
        for sid in vd.get("fail_signals", []):
            notes.append(f"[FAIL] {sid}")
        for sid in vd.get("warn_signals", []):
            notes.append(f"[WARN] {sid}")
        return {
            "product": "cmp",
            "scenario": 2,
            "headline": {
                "verdict": sm.get("go_no_go", "NO-GO"),
                "overall_score_percent": None,
            },
            "metric_rows": rows,
            "failure_notes": notes[:10],
            "doc_hint": "Scenario 2 run generated JSON/YAML artifacts (no GT-based scorecard).",
        }

    sm = report.get("summary_metrics") or {}
    _doc = report.get("document_score")
    _doc_num: float | None
    try:
        _doc_num = float(_doc) if _doc is not None and str(_doc).strip() != "" else None
    except (TypeError, ValueError):
        _doc_num = None
    rows: list[dict] = []
    if _doc_num is not None:
        # Document score from report.json is already the overall 0–100 measure.
        rows.append(
            {
                "metric": "Overall score",
                "detail": f"{_doc_num:.1f}%" if _doc_num <= 1.0 else f"{_doc_num:.1f}%",
                "pass": bool(str(sm.get("go_no_go") or "").upper() == "GO"),
                "hero": True,
                "tooltip": (
                    "CMP document score (0–100): structure weight + KRI content score (M1/M2) + "
                    "QTL content score (M3), minus hallucination penalty (M4)."
                ),
            }
        )
    rows += [
        {
            "metric": "M1 KRI recall",
            "detail": _fmt_pct(sm.get("m1_kri_recall")),
            "pass": bool(sm.get("m1_pass")),
            "tooltip": "Share of ground-truth KRIs the generator produced (matched by name).",
        },
        {
            "metric": "M2 Threshold accuracy",
            "detail": _fmt_pct(sm.get("m2_threshold_accuracy")),
            "pass": bool(sm.get("m2_pass")),
            "tooltip": "For matched KRIs, share whose threshold expression matches ground truth.",
        },
        {
            "metric": "M3 QTL recall",
            "detail": _fmt_pct(sm.get("m3_qtl_recall")),
            "pass": bool(sm.get("m3_pass")),
            "tooltip": "Share of ground-truth QTLs the generator produced.",
        },
        {
            "metric": "M4 Hallucinations flagged",
            "detail": str(sm.get("m4_hallucinations") if sm.get("m4_hallucinations") is not None else "—"),
            "pass": bool(sm.get("m4_pass")),
            "tooltip": (
                "Generated KRIs/QTLs that do not exist in the ground truth reference set "
                "(content precision, distinct from provenance checks)."
            ),
        },
    ]
    notes: list[str] = []
    sec = report.get("section_scores") or {}
    for key, label in (
        ("global_kris", "Global KRIs"),
        ("study_specific_kris", "Study-specific KRIs"),
        ("qtls", "QTLs"),
    ):
        blk = sec.get(key) if isinstance(sec, dict) else None
        if isinstance(blk, dict):
            notes.append(
                f"{label}: score {blk.get('score')} · matched {blk.get('matched')}/"
                f"{blk.get('ground_truth_count')}"
            )
    for act in (report.get("improvement_actions") or [])[:8]:
        if isinstance(act, str):
            notes.append(act[:420])
        elif isinstance(act, dict):
            line = act.get("summary") or act.get("action") or act.get("title") or act.get("detail")
            if line:
                notes.append(str(line)[:420])
    return {
        "product": "cmp",
        "headline": {
            "verdict": sm.get("go_no_go"),
            "document_score": report.get("document_score"),
            "overall_score_percent": _doc_num,
        },
        "metric_rows": rows,
        "failure_notes": notes,
        "doc_hint": "Download DOCX for the full CMP report (structure, sections, and improvement actions).",
    }


def _norm_prefix(route_prefix: str) -> str:
    return (route_prefix or "").strip().rstrip("/")


def _dl(base: str, tail: str) -> str:
    tail = tail if tail.startswith("/") else f"/{tail}"
    return f"{base}{tail}" if base else tail


_CMP_UI_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>CMP Eval</title>
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
<h1>CMP evaluation</h1>
<p>USDM and CMP JSON must carry the <strong>same study id</strong>. KRI/QTL ground truth defaults to <code>Data/</code>.</p>
<form id="f">
<label>USDM JSON<input type="file" name="usdm" accept=".json,application/json" required></label>
<label>CMP JSON<input type="file" name="gen" accept=".json,application/json" required></label>
<label>KRI ground truth CSV (optional)<input type="file" name="kri" accept=".csv"></label>
<label>QTL ground truth CSV (optional)<input type="file" name="qtl" accept=".csv"></label>
<label>Study metadata CSV (optional)<input type="file" name="meta" accept=".csv"></label>
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
  html += '<p><strong>Study:</strong> ' + esc(j.study_id) + ' · <strong>GO/NO-GO:</strong> ' + esc(h.verdict||j.verdict);
  if (h.document_score != null && h.document_score !== '') html += ' · <strong>Doc score:</strong> ' + esc(h.document_score);
  html += '</p>';
  if (p.metric_rows && p.metric_rows.length) {
    html += '<table class="mt"><thead><tr><th>Metric</th><th>Value</th><th>Pass</th></tr></thead><tbody>';
    for (const row of p.metric_rows) {
      const ok = row.pass !== false;
      html += '<tr class="' + (ok ? 'pass' : 'fail') + '"><td>' + esc(row.metric) + '</td><td>' + esc(row.detail) + '</td><td>' + (ok ? 'Yes' : 'No') + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  if (p.failure_notes && p.failure_notes.length) {
    html += '<p style="margin:.6rem 0 .2rem;font-weight:600">Section summary & actions</p><ul class="notes">';
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
        return _CMP_UI_SHELL.replace("__PREFIX__", json.dumps(base))

    @router.post("/eval/upload-session")
    async def eval_upload_session(
        usdm: UploadFile = File(...),
        gen: UploadFile = File(...),
        kri: UploadFile | None = File(None),
        qtl: UploadFile | None = File(None),
        meta: UploadFile | None = File(None),
    ) -> Dict[str, Any]:
        _cleanup_sessions()
        work = Path(tempfile.mkdtemp(prefix="cmp_eval_"))
        usdm_path = work / "usdm.json"
        gen_path = work / "cmp.json"
        usdm_path.write_bytes(await usdm.read())
        gen_path.write_bytes(await gen.read())
        try:
            usdm_obj = json.loads(usdm_path.read_text(encoding="utf-8"))
            cmp_obj = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

        sid_u = extract_protocol_study_id(usdm_obj)
        sid_g = extract_protocol_study_id(cmp_obj) or infer_study_id(cmp_obj, str(gen_path))
        if not sid_u or not sid_g:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Could not read study id (usdm={sid_u!r}, generator={sid_g!r}).",
            )
        if sid_u.strip().upper() != str(sid_g).strip().upper():
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Study id mismatch: USDM={sid_u} vs generator={sid_g}.",
            )
        study_id = str(sid_g).strip()

        if kri and kri.filename:
            kri_path = work / "kri_gt.csv"
            kri_path.write_bytes(await kri.read())
        else:
            resolved = _first_existing(_DEFAULT_KRI_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No KRI CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_KRI_CANDIDATES)
                    ),
                )
            kri_path = resolved

        if qtl and qtl.filename:
            qtl_path = work / "qtl_gt.csv"
            qtl_path.write_bytes(await qtl.read())
        else:
            resolved = _first_existing(_DEFAULT_QTL_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No QTL CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_QTL_CANDIDATES)
                    ),
                )
            qtl_path = resolved

        sm_path: Path | None
        if meta and meta.filename:
            sm_path = work / "study_meta.csv"
            sm_path.write_bytes(await meta.read())
        else:
            # Study metadata is genuinely optional — fall back silently.
            sm_path = _first_existing(_DEFAULT_META_CANDIDATES)

        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        has_gt_for_study = _study_has_cmp_ground_truth(study_id, Path(kri_path), Path(qtl_path))
        if has_gt_for_study:
            prev_usdm = os.environ.get("CMP_USDM_JSON_PATH")
            os.environ["CMP_USDM_JSON_PATH"] = str(usdm_path.resolve())
            try:
                config = load_config(Path(CONFIG_PATH))
                ground_truth = GroundTruth(
                    str(kri_path),
                    str(qtl_path),
                    str(sm_path) if sm_path and sm_path.is_file() else None,
                )
                report = cmp_run_eval(
                    cmp_json=cmp_obj,
                    study_id=study_id,
                    config=config,
                    ground_truth=ground_truth,
                    verbose=False,
                    artifact_paths={
                        "config": str(Path(CONFIG_PATH).resolve()),
                        "kri_gt": str(kri_path.resolve()),
                        "qtl_gt": str(qtl_path.resolve()),
                        "study_meta": str(sm_path.resolve()) if sm_path and sm_path.is_file() else "",
                        "usdm_protocol": str(usdm_path.resolve()),
                    },
                )
            finally:
                if prev_usdm is None:
                    os.environ.pop("CMP_USDM_JSON_PATH", None)
                else:
                    os.environ["CMP_USDM_JSON_PATH"] = prev_usdm
        else:
            report = cmp_run_scenario2(cmp_obj, study_id, usdm_path=str(usdm_path.resolve()))
            report.setdefault("eval_metadata", {})
            if isinstance(report["eval_metadata"], dict):
                report["eval_metadata"]["usdm_protocol_json_path"] = str(usdm_path.resolve())

        json_p = out_dir / f"cmp_eval_{study_id}.json"
        json_p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        yaml_p = out_dir / f"cmp_eval_{study_id}.yaml"
        if has_gt_for_study:
            write_cmp_eval_report_yaml(yaml_p, report)
        _ensure_yaml_from_json(json_p, yaml_p)
        docx_p = out_dir / f"CMP_Eval_Report_{study_id}.docx"
        if has_gt_for_study:
            _write_word_report_path(report, docx_p)

        miss_json_p: Path | None = None
        miss_md_p: Path | None = None
        if has_gt_for_study:
            try:
                paths = cmp_write_miss_explanation(
                    report,
                    str(usdm_path.resolve()),
                    out_dir,
                    f"cmp_eval_{study_id}",
                )
                miss_json_p = paths["json"]
                miss_md_p = paths["md"]
                em = report.setdefault("eval_metadata", {})
                if isinstance(em, dict):
                    em["miss_explanation_json_path"] = str(miss_json_p)
                    em["miss_explanation_md_path"] = str(miss_md_p)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[WARN] CMP miss explanation skipped: {exc}")

        token = secrets.token_urlsafe(24)
        preview = _cmp_preview(report)
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

        if int(report.get("scenario") or 1) == 2:
            doc_pass = bool((report.get("summary_metrics") or {}).get("overall_pass"))
        else:
            doc_pass = report.get("document_passed", report.get("document_pass"))
        return {
            "study_id": study_id,
            "verdict": "GO" if doc_pass else "NO-GO",
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
            for _, p in meta["paths"].items():
                if p and Path(p).is_file():
                    zf.write(p, arcname=Path(p).name)
        buf.seek(0)
        sid = meta.get("study_id", "study")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="cmp_eval_{sid}.zip"'},
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
