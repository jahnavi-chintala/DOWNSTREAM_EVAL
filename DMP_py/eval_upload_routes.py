"""Browser UI: upload USDM + DMP JSON, run eval, download JSON/YAML/DOCX."""

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
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from dmp_data import infer_study_id
from dmp_eval_report_docx import write_dmp_eval_docx
from dmp_eval_report_yaml import write_dmp_eval_report_yaml
from eval_d4_dmp import run_eval as dmp_run_eval

from protocol_study_id import extract_protocol_study_id

_PKG = Path(__file__).resolve().parent
_CONFIG = _PKG / "eval_config" / "dmp_eval_config.yaml"


def _first_existing(candidates) -> Path | None:
    """Return the first candidate path that is a real file, or None."""
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return p
    return None


# Hard-coded ground-truth lookups so the eval still runs when the UI user
# doesn't upload the optional files. Candidates are tried in order.
_DEFAULT_DMP_GT_CANDIDATES = (
    _PKG / "data" / "dmp_ground_truth_clean.json",
    _PKG / "data" / "dmp_ground_truth.json",
    _PKG.parent / "dmp_ground_truth_clean.json",
    _PKG.parent / "dmp_ground_truth.json",
)
_DEFAULT_SDS_CANDIDATES = (
    _PKG / "data" / "sds_non_crf_ground_truth_clean.csv",
    _PKG / "data" / "sds_non_crf_ground_truth.csv",
    _PKG / "data" / "sds_non_crf.csv",
    _PKG.parent / "sds_non_crf_ground_truth_clean.csv",
)
_DEFAULT_DMP_GT = _first_existing(_DEFAULT_DMP_GT_CANDIDATES) or _DEFAULT_DMP_GT_CANDIDATES[0]
_DEFAULT_SDS = _first_existing(_DEFAULT_SDS_CANDIDATES) or _DEFAULT_SDS_CANDIDATES[0]

SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL_SEC = 7200


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


def _dmp_preview(report: Dict[str, Any]) -> Dict[str, Any]:
    sm = report.get("summary_metrics") or {}
    em = report.get("eval_metadata") or {}
    _doc = report.get("document_score")
    try:
        _doc_num = float(_doc) if _doc is not None and str(_doc).strip() != "" else None
    except (TypeError, ValueError):
        _doc_num = None
    rows: list[dict] = []
    if _doc_num is not None:
        rows.append(
            {
                "metric": "Overall score",
                "detail": f"{_doc_num:.1f}%",
                "pass": bool(str(sm.get("go_no_go") or "").upper() == "GO"),
                "hero": True,
                "tooltip": (
                    "DMP document score (0–100): structure weight + section content scores "
                    "(S5/S6/S8/S11), minus hallucination penalty."
                ),
            }
        )
    rows += [
        {
            "metric": "M1 S5 system accuracy",
            "detail": _fmt_pct(sm.get("m1_s5_system_accuracy")),
            "pass": bool(sm.get("m1_pass")),
            "tooltip": "Accuracy of Section 5 (Systems) content vs ground truth.",
        },
        {
            "metric": "M2 S6 vendor recall",
            "detail": _fmt_pct(sm.get("m2_s6_vendor_recall")),
            "pass": bool(sm.get("m2_pass")),
            "tooltip": "Recall of Section 6 (Vendors/Suppliers) ground-truth entries.",
        },
        {
            "metric": "M3 S8 module recall",
            "detail": _fmt_pct(sm.get("m3_s8_module_recall")),
            "pass": bool(sm.get("m3_pass")),
            "tooltip": "Recall of Section 8 (Critical data modules) ground-truth entries.",
        },
        {
            "metric": "M4 S11 reconciliation",
            "detail": _fmt_pct(sm.get("m4_reconciliation_accuracy")),
            "pass": bool(sm.get("m4_pass")),
            "tooltip": "Accuracy of Section 11 (External data reconciliation) coverage.",
        },
        {
            "metric": "Hallucinations flagged",
            "detail": str(sm.get("m4_hallucinations") if sm.get("m4_hallucinations") is not None else "—")
            + f" (target ≤ {sm.get('m4_hallucination_target', '—')})",
            "pass": bool(sm.get("m4_hallucination_pass")),
            "tooltip": (
                "Generated DMP entries (systems, vendors, modules) not present in ground truth. "
                "Counted as content hallucinations — distinct from provenance checks."
            ),
        },
    ]
    notes: list[str] = []
    sec = report.get("section_scores") or {}
    for key, label in (
        ("s5_systems", "S5 Systems"),
        ("s6_vendors", "S6 Vendors"),
        ("s8_critical_data", "S8 Critical data"),
        ("s11_reconciliation", "S11 Reconciliation"),
    ):
        blk = sec.get(key) if isinstance(sec, dict) else None
        if isinstance(blk, dict) and blk.get("score") is not None:
            line = f"{label}: score {blk.get('score')}"
            if blk.get("matched") is not None and blk.get("ground_truth_count") is not None:
                line += f" · matched {blk.get('matched')}/{blk.get('ground_truth_count')}"
            notes.append(line)
    for act in (report.get("improvement_actions") or [])[:10]:
        if isinstance(act, dict) and act.get("action"):
            pri = act.get("priority") or ""
            notes.append(f"[{pri}] {act['action']}"[:480])
        elif isinstance(act, str):
            notes.append(act[:480])
    return {
        "product": "dmp",
        "headline": {
            "verdict": sm.get("go_no_go"),
            "document_score": report.get("document_score"),
            "overall_score_percent": _doc_num,
            "therapeutic_area": em.get("therapeutic_area"),
            "phase": em.get("phase"),
        },
        "metric_rows": rows,
        "failure_notes": notes,
        "doc_hint": "Download DOCX for the formatted DMP evaluation report.",
    }


def _norm_prefix(route_prefix: str) -> str:
    return (route_prefix or "").strip().rstrip("/")


def _dl(base: str, tail: str) -> str:
    tail = tail if tail.startswith("/") else f"/{tail}"
    return f"{base}{tail}" if base else tail


_DMP_UI_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>DMP Eval</title>
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
<h1>DMP evaluation</h1>
<p>USDM and DMP JSON must carry the <strong>same study id</strong>. Ground truth defaults to <code>data/</code>.</p>
<form id="f">
<label>USDM JSON<input type="file" name="usdm" accept=".json,application/json" required></label>
<label>DMP JSON<input type="file" name="gen" accept=".json,application/json" required></label>
<label>dmp_ground_truth JSON (optional)<input type="file" name="dmpgt" accept=".json"></label>
<label>sds_non_crf CSV (optional)<input type="file" name="sds" accept=".csv"></label>
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
  if (h.therapeutic_area || h.phase) {
    html += '<p style="font-size:.9rem">' + (h.therapeutic_area ? '<strong>TA:</strong> ' + esc(h.therapeutic_area) : '');
    if (h.phase) html += (h.therapeutic_area ? ' · ' : '') + '<strong>Phase:</strong> ' + esc(h.phase);
    html += '</p>';
  }
  if (p.metric_rows && p.metric_rows.length) {
    html += '<table class="mt"><thead><tr><th>Metric</th><th>Value</th><th>Pass</th></tr></thead><tbody>';
    for (const row of p.metric_rows) {
      const ok = row.pass !== false;
      html += '<tr class="' + (ok ? 'pass' : 'fail') + '"><td>' + esc(row.metric) + '</td><td>' + esc(row.detail) + '</td><td>' + (ok ? 'Yes' : 'No') + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  if (p.failure_notes && p.failure_notes.length) {
    html += '<p style="margin:.6rem 0 .2rem;font-weight:600">Sections & improvement actions</p><ul class="notes">';
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
        return _DMP_UI_SHELL.replace("__PREFIX__", json.dumps(base))

    @router.post("/eval/upload-session")
    async def eval_upload_session(
        usdm: UploadFile = File(...),
        gen: UploadFile = File(...),
        dmpgt: UploadFile | None = File(None),
        sds: UploadFile | None = File(None),
    ) -> Dict[str, Any]:
        _cleanup_sessions()
        work = Path(tempfile.mkdtemp(prefix="dmp_eval_"))
        usdm_path = work / "usdm.json"
        gen_path = work / "dmp.json"
        usdm_path.write_bytes(await usdm.read())
        gen_path.write_bytes(await gen.read())
        try:
            usdm_obj = json.loads(usdm_path.read_text(encoding="utf-8"))
            dmp_obj = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

        sid_u = extract_protocol_study_id(usdm_obj)
        sid_g = extract_protocol_study_id(dmp_obj) or infer_study_id(dmp_obj, gen_path)
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
        study_id = str(sid_g).strip().upper()

        if dmpgt and dmpgt.filename:
            dmp_gt_path = work / "dmp_ground_truth.json"
            dmp_gt_path.write_bytes(await dmpgt.read())
        else:
            resolved = _first_existing(_DEFAULT_DMP_GT_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No DMP ground-truth JSON uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_DMP_GT_CANDIDATES)
                    ),
                )
            dmp_gt_path = resolved

        if sds and sds.filename:
            sds_path = work / "sds.csv"
            sds_path.write_bytes(await sds.read())
        else:
            resolved = _first_existing(_DEFAULT_SDS_CANDIDATES)
            if resolved is None:
                shutil.rmtree(work, ignore_errors=True)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No SDS non-CRF CSV uploaded and no default found. Looked for: "
                        + ", ".join(str(p) for p in _DEFAULT_SDS_CANDIDATES)
                    ),
                )
            sds_path = resolved

        if not _CONFIG.is_file():
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(status_code=500, detail="eval_config/dmp_eval_config.yaml missing.")

        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        report = dmp_run_eval(
            gen_path,
            study_id,
            _CONFIG,
            dmp_gt_path,
            sds_path,
            output_dir=out_dir,
            write_yaml=False,
            write_word=False,
        )
        em = report.setdefault("eval_metadata", {})
        if isinstance(em, dict):
            em["usdm_protocol_json_path"] = str(usdm_path.resolve())

        stem = f"dmp_eval_{study_id}"
        json_p = out_dir / f"{stem}.json"
        yaml_p = out_dir / f"{stem}.yaml"
        docx_p = out_dir / f"DMP_Eval_Report_{study_id}.docx"
        json_p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        ed = str(report.get("eval_metadata", {}).get("eval_date", "unknown"))
        write_dmp_eval_report_yaml(yaml_p, report, config_source_path=_CONFIG, eval_date=ed)
        _ensure_yaml_from_json(json_p, yaml_p)
        write_dmp_eval_docx(docx_p, report)

        token = secrets.token_urlsafe(24)
        ok = bool(report.get("summary_metrics", {}).get("overall_pass"))
        preview = _dmp_preview(report)
        SESSIONS[token] = {
            "t": time.time(),
            "workdir": str(work),
            "study_id": study_id,
            "preview": preview,
            "paths": {
                "json": str(json_p) if json_p.is_file() else "",
                "yaml": str(yaml_p) if yaml_p.is_file() else "",
                "docx": str(docx_p) if docx_p.is_file() else "",
            },
        }

        return {
            "study_id": study_id,
            "verdict": "GO" if ok else "NO-GO",
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
            headers={"Content-Disposition": f'attachment; filename="dmp_eval_{sid}.zip"'},
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
