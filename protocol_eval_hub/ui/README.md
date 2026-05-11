# Protocol Evaluation Console (SPA)

A Vite + React + TypeScript single-page app that replaces the legacy HTML gateway.
It is served by the FastAPI app in `protocol_eval_hub/unified_eval_app.py` when a
production build exists at `ui/dist/`. If no build is present, FastAPI falls back
to the legacy inline HTML, so the gateway keeps working during setup.

## Layout

- `src/App.tsx` — top-level shell (sidebar + main workspace).
- `src/components/` — UI building blocks (sidebar, upload panel, results, tabs, DOCX preview).
- `src/lib/` — formatting, local-storage recent runs.
- `src/products.ts` — per-product fields, labels, copy.
- `src/api.ts` — fetch helpers talking to `/risk`, `/pipd`, `/cmp`, `/dmp`.
- `src/styles.css` — clinical-minimal design tokens and component styles.

## Prerequisites

- Node 18+ (tested with 18 / 20).
- The FastAPI gateway (only needed for the dev proxy). Vite defaults to `http://127.0.0.1:8010`; set `EVAL_HUB_URL` if you use another port.

## One-time install

```powershell
cd protocol_eval_hub/ui
npm install
```

## Development (hot reload)

In one terminal, run FastAPI as usual:

```powershell
cd protocol_eval_hub
python -m uvicorn unified_eval_app:app --host 0.0.0.0 --port 8010
```

If you use port **8080** (or anything else) for uvicorn, start Vite with the same target, e.g.  
`$env:EVAL_HUB_URL='http://127.0.0.1:8080'; npm run dev`

In another terminal:

```powershell
cd protocol_eval_hub/ui
npm run dev
```

Open http://localhost:5173 — Vite proxies `/risk`, `/pipd`, `/cmp`, `/dmp`, `/health`,
`/docs`, and `/openapi.json` to the FastAPI process (by default on port 8010).

## Production build

```powershell
cd protocol_eval_hub/ui
npm run build
```

That emits `ui/dist/` (HTML + hashed JS/CSS). FastAPI automatically detects
`ui/dist/index.html` and mounts it at `/`, plus `ui/dist/assets/*` at `/assets/*`.
Restart uvicorn and open the app at the same host/port you use for the API (e.g. `http://localhost:8010/`), or use `npm run dev` and open http://localhost:5173/ for the proxied dev UI.

Legacy per-product pages (`/risk/ui`, `/pipd/ui`, `/cmp/ui`, `/dmp/ui`) now
redirect into the SPA with the correct product pre-selected. The old inline HTML
is still reachable at `/legacy` as a fallback.

## Network / offline

Three things are loaded from public CDNs:

- Google Fonts (Inter, Source Serif 4, JetBrains Mono).
- `mammoth.browser.min.js` (DOCX → HTML client-side conversion).

If your deployment forbids CDNs, vendor those assets under `ui/public/` and update
`index.html` accordingly. The FastAPI `/risk/eval/session/{token}/preview/docx-html`
endpoint is used as a server-side fallback whenever the browser mammoth is
unavailable or fails.
