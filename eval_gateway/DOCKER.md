# Docker Usage

## Option A: Run full local stack with Docker Compose

From `C:\Users\jahna\OneDrive\Desktop\Pfizer`:

```powershell
docker compose -f docker-compose.unified-eval.yml up --build
```

Endpoints:

- UI: `http://localhost:8080`
- Backend health: `http://localhost:9001/health`
- Backend docs: `http://localhost:9001/docs`

Default login:

- Username: `admin`
- Password: `change-me`

Update these in `docker-compose.unified-eval.yml` for real use.

## Option B: Build images separately

From Pfizer root:

```powershell
docker build -f eval_gateway/Dockerfile.backend -t unified-eval-backend:latest .
docker build -f eval_gateway/ui/Dockerfile.ui -t unified-eval-ui:latest eval_gateway/ui --build-arg VITE_API_BASE_URL=http://localhost:9001
```

## Notes

- Backend container includes `ppid_py`, `risk_profile_eval`, `cmd_py`, `DMP_py`, and `protocol_eval_hub` so orchestration works.
- Risk execution remains forced to Scenario 1 via the hub runner argument wiring.
- `protocol_bundles` are mounted from host at `/data/protocol_bundles` in compose.
