"""
Single-port gateway for all four protocol evals (Risk, PIPD, CMP, DMP).

Mounts each product's upload UI and API under a path prefix:
  /risk/..., /pipd/..., /cmp/..., /dmp/...

Home page (/) lets you pick the product and uploads USDM + generator JSON.

Run (from repo root Pfizer, or set PFIZER_ROOT)::

    pip install fastapi uvicorn
    cd protocol_eval_hub
    uvicorn unified_eval_app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

_HUB = Path(__file__).resolve().parent
_SPA_DIST = _HUB / "ui" / "dist"
_SPA_INDEX = _SPA_DIST / "index.html"
_SPA_ASSETS = _SPA_DIST / "assets"


def _pfizer_root() -> Path:
    env = (os.environ.get("PFIZER_ROOT") or "").strip()
    if env:
        return Path(env).resolve()
    return _HUB.parent


# Names that several product repos all use at their top level. If we let the
# first product to load win the cache in :data:`sys.modules`, later products
# silently bind their own handlers to the first product's implementation —
# that's what caused PIPD to throw ``run_eval() got an unexpected keyword
# argument 'generator_json_path'`` when run after Risk Profile. We pop these
# between registrations so each product's ``from run_eval import run_eval``
# (etc.) resolves to *its own* file.
# Module names that appear identically in two or more product repos. We must
# flush these between product registrations so each product's top-level
# ``from <name> import X`` resolves to *its own* file rather than whichever
# product registered first. Product-prefixed names (``pipd_*``, ``risk_*``,
# ``dmp_*``, ``cmp_*``, ``scorer_*``) are unique per product and must NOT be
# flushed — doing so would wipe out the prewarmed helper modules that hold
# already-resolved ``from eval_scenario1 import NULL_PLACEHOLDERS``-style
# bindings.
_CONFLICTING_MODULE_NAMES = (
    "run_eval",
    "eval_scenario1",
    "eval_scenario2",
    "protocol_study_id",
    "align_to_reference",
    "reference_shape_verify",
    "api",
)


def _clear_conflicts() -> None:
    for name in _CONFLICTING_MODULE_NAMES:
        sys.modules.pop(name, None)


def _register_eval_module(app: FastAPI, module_id: str, folder: str, route_prefix: str) -> None:
    root = (_pfizer_root() / folder).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Eval repo not found: {root} (set PFIZER_ROOT)")
    path = root / "eval_upload_routes.py"
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}")

    unique_name = f"eval_upload_{module_id}"
    root_str = str(root)
    _clear_conflicts()
    # Move this product's root to the FRONT of sys.path while its imports
    # resolve, so every ``from run_eval import run_eval``,
    # ``from eval_scenario1 import NULL_PLACEHOLDERS`` etc. binds to this
    # product's copy. Keep it in sys.path afterwards so lazy imports at
    # request time still succeed.
    try:
        sys.path.remove(root_str)
    except ValueError:
        pass
    sys.path.insert(0, root_str)
    try:
        spec = importlib.util.spec_from_file_location(unique_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = mod
        spec.loader.exec_module(mod)

        # Eagerly import every other .py file in the product's root so their
        # top-level ``from <shared> import X`` bindings are resolved now,
        # while ``sys.modules["run_eval"]`` / ``eval_scenario1`` etc. still
        # point at *this* product. Once the helper module is imported, its
        # references are locked in — lazy re-imports later become cheap
        # sys.modules lookups and do not re-resolve through sys.path.
        _skip_prewarm_prefixes = ("run_", "show_", "generate_")
        _skip_prewarm_names = {
            "__init__",
            "eval_upload_routes",
            "api",
            "align_to_reference",
            "reference_shape_verify",
        }
        for py in sorted(root.glob("*.py")):
            name = py.stem
            if name in _skip_prewarm_names:
                continue
            if any(name.startswith(p) for p in _skip_prewarm_prefixes):
                continue
            if name in sys.modules:
                continue
            try:
                importlib.import_module(name)
            except BaseException:  # noqa: BLE001 - best-effort prewarm
                # Some modules run argparse / network / sys.exit at import
                # time; skipping them is fine as they're not referenced by
                # the upload handlers.
                pass

        mod.register_eval_upload_routes(app, route_prefix=route_prefix)
    finally:
        # Demote this product's root to the end so the next product can take
        # priority during its own registration, but leave it on sys.path so
        # any lazy imports done at request time still resolve.
        try:
            sys.path.remove(root_str)
        except ValueError:
            pass
        sys.path.append(root_str)
        _clear_conflicts()


app = FastAPI(
    title="Protocol eval gateway",
    description="Risk Profile, PIPD, CMP, and DMP evaluations on one port",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Register redirects for the legacy per-product /ui pages BEFORE the product
# modules get a chance to claim those paths. FastAPI uses first-match routing,
# so these win over each module's /ui handler and funnel users into the SPA.
def _ui_redirect(product_key: str):
    def _h() -> RedirectResponse:
        return RedirectResponse(url=f"/?product={product_key}", status_code=307)

    _h.__name__ = f"legacy_{product_key}_ui_redirect"
    return _h


for _pfx, _pkey in (("/risk", "risk"), ("/pipd", "pipd"), ("/cmp", "cmp"), ("/dmp", "dmp")):
    app.add_api_route(
        f"{_pfx}/ui",
        _ui_redirect(_pkey),
        methods=["GET"],
        include_in_schema=False,
    )

for _mid, _dir, _pfx in (
    ("risk", "risk_profile_eval", "/risk"),
    ("pipd", "ppid_py", "/pipd"),
    ("cmp", "cmd_py", "/cmp"),
    ("dmp", "DMP_py", "/dmp"),
):
    _register_eval_module(app, _mid, _dir, _pfx)

# Mount the React SPA's hashed assets under /assets when a build exists.
if _SPA_ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_SPA_ASSETS)), name="spa-assets")

_GATEWAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Protocol evaluations</title>
<style>
body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem}
label{display:block;margin-top:.75rem;font-weight:600}
.opts{margin-top:.5rem;padding:.75rem;background:#f8f8f8;border-radius:8px}
button{margin-top:1rem;padding:.55rem 1.1rem}
#out{margin-top:1.25rem;font-size:.9rem}
#out .err{white-space:pre-wrap;background:#fee;padding:1rem;border-radius:8px}
#out .pv{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;margin-bottom:1rem}
#out .pv h3{margin:0 0 .5rem;font-size:1rem}
#out table.mt{width:100%;border-collapse:collapse;font-size:.88rem}
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
<h1>Run evaluation</h1>
<p class="desc">Choose the product, upload <strong>USDM JSON</strong> and the matching <strong>generator JSON</strong>.
Study ids must match. Optional ground-truth files default to each repo&rsquo;s packaged <code>data/</code> (or <code>Data/</code> for CMP).</p>

<label>Product
<select id="product" style="margin-top:.35rem;font-size:1rem">
  <option value="/risk">Risk Profile</option>
  <option value="/pipd">PIPD</option>
  <option value="/cmp">CMP</option>
  <option value="/dmp">DMP</option>
</select>
</label>

<label>USDM JSON <input type="file" id="usdm" accept=".json,application/json" required></label>
<label><span id="genLab">Generator (Risk Profile) JSON</span> <input type="file" id="gen" accept=".json,application/json" required></label>

<div id="opt-risk" class="opts">
  <strong>Risk — optional</strong>
  <label>risk_profile_ground_truth.csv <input type="file" id="risks" accept=".csv"></label>
  <label>critical_factors_ground_truth.csv <input type="file" id="factors" accept=".csv"></label>
</div>
<div id="opt-pipd" class="opts" style="display:none">
  <strong>PIPD — optional</strong>
  <label>pipd_ground_truth CSV <input type="file" id="gt" accept=".csv"></label>
  <label>deviation_subcategories CSV <input type="file" id="dev" accept=".csv"></label>
</div>
<div id="opt-cmp" class="opts" style="display:none">
  <strong>CMP — optional</strong>
  <label>KRI ground truth CSV <input type="file" id="kri" accept=".csv"></label>
  <label>QTL ground truth CSV <input type="file" id="qtl" accept=".csv"></label>
  <label>Study metadata CSV <input type="file" id="meta" accept=".csv"></label>
</div>
<div id="opt-dmp" class="opts" style="display:none">
  <strong>DMP — optional</strong>
  <label>dmp_ground_truth JSON <input type="file" id="dmpgt" accept=".json"></label>
  <label>sds_non_crf CSV <input type="file" id="sds" accept=".csv"></label>
</div>

<button type="button" id="go">Run eval</button>
<p class="desc">Per-product pages: <a href="/risk/ui">/risk/ui</a> · <a href="/pipd/ui">/pipd/ui</a> ·
<a href="/cmp/ui">/cmp/ui</a> · <a href="/dmp/ui">/dmp/ui</a> · <a href="/docs">OpenAPI</a></p>
<div id="out"></div>

<script>
function prefix() { return document.getElementById('product').value; }
function u(path) { const p = prefix(); return (p || '') + path; }

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
    const serverHtmlUrl = prefix() + '/eval/session/' + tok + '/preview/docx-html';
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
    '<div class="panel" id="pw"><p class="desc" style="font-size:.8rem;margin:0 0 .5rem">DOCX shown via browser or server conversion; layout may differ from Microsoft Word. You can always open the downloaded .docx.</p>' +
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
    let h = '<p class="desc">No structured preview returned; use downloads below.</p>';
    h += '<p class="dl"><strong>Downloads</strong></p>';
    h += '<a class="dlbtn" href="' + esc(j.downloads.zip) + '">ZIP (all)</a>';
    h += '<a class="dlbtn" href="' + esc(j.downloads.json) + '">JSON</a>';
    h += '<a class="dlbtn" href="' + esc(j.downloads.yaml) + '">YAML</a>';
    h += '<a class="dlbtn" href="' + esc(j.downloads.docx) + '">DOCX</a>';
    h += artifactPreviewsShell();
    return h;
  }
  const h = p.headline || {};
  let html = '<div class="pv"><h3>Results preview</h3>';
  html += '<p><strong>Study:</strong> ' + esc(j.study_id);
  const verdict = h.verdict || j.verdict;
  if (verdict) html += ' · <strong>Verdict / GO:</strong> ' + esc(verdict);
  if (h.document_score != null && h.document_score !== '') html += ' · <strong>Doc score:</strong> ' + esc(h.document_score);
  html += '</p>';
  if (h.rag_traffic_light) html += '<p><strong>Signal verdict:</strong> ' + esc(h.rag_traffic_light) + '</p>';
  const ta = h.ta || h.therapeutic_area;
  if (ta || h.phase) {
    html += '<p style="font-size:.9rem">';
    if (ta) html += '<strong>TA:</strong> ' + esc(ta);
    if (h.phase) html += (ta ? ' · ' : '') + '<strong>Phase:</strong> ' + esc(h.phase);
    html += '</p>';
  }
  if (p.metric_rows && p.metric_rows.length) {
    html += '<table class="mt"><thead><tr><th>Metric / signal</th><th>Detail</th><th>Pass</th></tr></thead><tbody>';
    for (const row of p.metric_rows) {
      const ok = row.pass !== false;
      html += '<tr class="' + (ok ? 'pass' : 'fail') + '"><td>' + esc(row.metric) + '</td><td>' + esc(row.detail) + '</td><td>' + (ok ? 'Yes' : 'No') + '</td></tr>';
    }
    html += '</tbody></table>';
  }
  if (p.counts && Object.keys(p.counts).length) {
    html += '<p style="font-size:.85rem;margin:.5rem 0 0;color:#475569">' + esc(JSON.stringify(p.counts)) + '</p>';
  }
  if (p.failure_notes && p.failure_notes.length) {
    html += '<p style="margin:.6rem 0 .2rem;font-weight:600">Notes</p><ul class="notes">';
    for (const n of p.failure_notes) html += '<li>' + esc(n) + '</li>';
    html += '</ul>';
  }
  if (p.doc_hint) html += '<p style="font-size:.85rem;color:#475569;margin:.6rem 0 0">' + esc(p.doc_hint) + '</p>';
  html += '</div>';
  html += '<p class="dl"><strong>Downloads</strong> (open DOCX for full formatted report)</p>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.zip) + '">ZIP (all)</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.json) + '">JSON</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.yaml) + '">YAML</a>';
  html += '<a class="dlbtn" href="' + esc(j.downloads.docx) + '">DOCX</a>';
  html += artifactPreviewsShell();
  return html;
}

const labels = {
  '/risk': 'Generator (Risk Profile) JSON',
  '/pipd': 'PIPD JSON',
  '/cmp': 'CMP JSON',
  '/dmp': 'DMP JSON'
};

function syncProduct() {
  const p = prefix();
  document.getElementById('genLab').textContent = labels[p] || 'Generator JSON';
  ['opt-risk','opt-pipd','opt-cmp','opt-dmp'].forEach(id => {
    document.getElementById(id).style.display = 'none';
  });
  if (p === '/risk') document.getElementById('opt-risk').style.display = 'block';
  if (p === '/pipd') document.getElementById('opt-pipd').style.display = 'block';
  if (p === '/cmp') document.getElementById('opt-cmp').style.display = 'block';
  if (p === '/dmp') document.getElementById('opt-dmp').style.display = 'block';
}
document.getElementById('product').onchange = syncProduct;
syncProduct();

document.getElementById('go').onclick = async () => {
  const out = document.getElementById('out');
  const usdm = document.getElementById('usdm').files[0];
  const gen = document.getElementById('gen').files[0];
  if (!usdm || !gen) { out.innerHTML = '<div class="err">Select USDM and generator JSON.</div>'; return; }
  const p = prefix();
  const fd = new FormData();
  fd.append('usdm', usdm);
  fd.append('gen', gen);
  if (p === '/risk') {
    const x = document.getElementById('risks').files[0]; if (x) fd.append('risks', x);
    const y = document.getElementById('factors').files[0]; if (y) fd.append('factors', y);
  } else if (p === '/pipd') {
    const x = document.getElementById('gt').files[0]; if (x) fd.append('gt', x);
    const y = document.getElementById('dev').files[0]; if (y) fd.append('dev', y);
  } else if (p === '/cmp') {
    const x = document.getElementById('kri').files[0]; if (x) fd.append('kri', x);
    const y = document.getElementById('qtl').files[0]; if (y) fd.append('qtl', y);
    const z = document.getElementById('meta').files[0]; if (z) fd.append('meta', z);
  } else if (p === '/dmp') {
    const x = document.getElementById('dmpgt').files[0]; if (x) fd.append('dmpgt', x);
    const y = document.getElementById('sds').files[0]; if (y) fd.append('sds', y);
  }
  out.innerHTML = '<div class="err">Running…</div>';
  try {
    const r = await fetch(u('/eval/upload-session'), { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) { out.innerHTML = '<div class="err">' + esc(JSON.stringify(j, null, 2)) + '</div>'; return; }
    out.innerHTML = '<p><b>Done.</b></p>' + renderPreview(j);
    loadArtifactPreviews(j);
  } catch (e) {
    out.innerHTML = '<div class="err">' + esc(String(e)) + '</div>';
  }
};
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def gateway_home():
    # Prefer the built SPA. If the operator hasn't run `npm run build` yet,
    # fall back to the self-contained legacy HTML so the gateway still works.
    if _SPA_INDEX.is_file():
        return FileResponse(str(_SPA_INDEX), media_type="text/html")
    return HTMLResponse(_GATEWAY_HTML)


@app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
def gateway_legacy() -> str:
    """Original single-file HTML console, kept as an escape hatch."""
    return _GATEWAY_HTML


# Expose a couple of SPA-owned root files (favicon, manifest, etc.) if present.
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fav = _SPA_DIST / "favicon.ico"
    if fav.is_file():
        return FileResponse(str(fav))
    return HTMLResponse(status_code=204, content="")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gateway": "protocol_eval_hub",
        "pfizer_root": str(_pfizer_root()),
        "spa_built": _SPA_INDEX.is_file(),
    }
