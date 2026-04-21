# DOWNSTREAM_EVAL

Protocol downstream evaluation tooling: unified web UI, scenario evaluators (PIPD, Risk Profile, CMP, DMP), and optional Docker-backed gateway.

## Contents (this repository)

| Path | Description |
|------|-------------|
| [`protocol_eval_hub/`](protocol_eval_hub/) | FastAPI **unified eval app** (single process, product routes), React **eval console** (`ui/`), bundle runner. |
| [`ppid_py/`](ppid_py/) | **PIPD** Scenario 1–2 eval (`eval_scenario1.py`), upload routes, reports, semantic review hooks. |
| [`risk_profile_eval/`](risk_profile_eval/) | **Risk Profile** scenario eval, USDM tracing for M4, upload routes. |
| [`cmd_py/`](cmd_py/) | **CMP** (content monitoring protocol) eval API and upload routes. |
| [`DMP_py/`](DMP_py/) | **DMP** eval API and upload routes. |
| [`eval_gateway/`](eval_gateway/) | Optional **gateway** UI/API Docker build (used by `docker-compose.unified-eval.yml`). |
| [`docker-compose.unified-eval.yml`](docker-compose.unified-eval.yml) | Compose stack (Postgres, backend, frontend) for gateway mode. |

## Quick start (unified hub, local)

1. **Python 3.11+** with project dependencies installed (each package may have its own `requirements` / `pyproject`; start from `protocol_eval_hub` and the four product roots).

2. Set **`PFIZER_ROOT`** to the directory that **contains** the product folders (the parent of `protocol_eval_hub`, `ppid_py`, etc.).

3. From that parent directory:

   ```bash
   python -m uvicorn protocol_eval_hub.unified_eval_app:app --host 127.0.0.1 --port 8010
   ```

4. Open the static UI served by the app (or build the React app under `protocol_eval_hub/ui` with `npm install && npm run build` so `ui/dist` is used).

## Products (HTTP prefixes)

Typical upload session endpoints (see `unified_eval_app` for exact prefixes):

- **PIPD** — `POST /pipd/eval/upload-session`
- **Risk Profile** — `POST /risk/eval/upload-session`
- **CMP** — `POST /cmp/eval/upload-session`
- **DMP** — `POST /dmp/eval/upload-session`

Each returns JSON with `preview`, `downloads` (zip/json/yaml/docx), and `session_token`.

## Environment

- **`OPENAI_API_KEY`** — optional; enables PIPD LLM semantic review when set.
- **`PFIZER_ROOT`** — required for dynamic imports of product evaluators from sibling directories.

## License

Use and distribution are subject to your organization’s policies. Do not commit confidential study data or credentials.

---

Repository: [github.com/jahnavi-chintala/DOWNSTREAM_EVAL](https://github.com/jahnavi-chintala/DOWNSTREAM_EVAL)
