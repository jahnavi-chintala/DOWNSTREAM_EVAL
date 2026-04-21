# Railway Deployment Guide

This setup uses two Railway services:

- Backend API service rooted at `eval_gateway`
- Frontend UI service rooted at `eval_gateway/ui`

## 1) Backend Service (`eval_gateway`)

Use the included [`railway.json`](railway.json) and set service root to `eval_gateway`.

Required env vars:

- `PFIZER_ROOT` = absolute path where `ppid_py`, `risk_profile_eval`, `cmd_py`, `DMP_py`, and `protocol_eval_hub` exist
- `BUNDLES_PARENT` = path to `protocol_eval_hub/protocol_bundles`
- `HUB_RUNNER` = path to `protocol_eval_hub/run_protocol_eval_bundle.py`
- `DATABASE_URL` = Railway Postgres URL
- `JWT_SECRET` = strong random secret
- `APP_USERNAME` / `APP_PASSWORD` = dashboard login
- `CORS_ALLOW_ORIGINS` = frontend URL (comma-separated)

Healthcheck: `GET /health`

## 2) Frontend Service (`eval_gateway/ui`)

Use the included [`railway.json`](ui/railway.json) and set service root to `eval_gateway/ui`.

Required env var:

- `VITE_API_BASE_URL` = backend public URL

Healthcheck: `GET /`

## 3) Verify after deploy

1. Open frontend URL.
2. Login with `APP_USERNAME` / `APP_PASSWORD`.
3. Trigger a protocol run from the Runs page.
4. Confirm status transitions: `queued` -> `running` -> `completed`/`completed_with_issues`.
5. Download artifacts (JSON/YAML/DOCX/PNG where available).

## Notes

- Risk mode is enforced as `scenario1_forced` via the backend orchestration path.
- Runner logs are available in `GET /runs/{id}/log`.
