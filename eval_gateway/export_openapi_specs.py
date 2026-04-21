"""
Write OpenAPI 3 YAML for each backend with gateway `servers` URLs (Postman-friendly).

Run from eval_gateway after installing ppid_py + risk_profile_eval dependencies:
    pip install -r ../ppid_py/requirements.txt -r ../risk_profile_eval/requirements.txt -r requirements.txt
    python export_openapi_specs.py

Output:
    openapi/pipd.yaml
    openapi/risk_profile.yaml
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PFIZER = ROOT.parent
OUT = ROOT / "openapi"


def _load_openapi(cwd: Path) -> dict:
    code = r"""
import json, sys
sys.path.insert(0, ".")
from api import app
json.dump(app.openapi(), sys.stdout)
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise RuntimeError(f"openapi export failed in {cwd}")
    return json.loads(r.stdout)


def _dump(name: str, spec: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(
            spec,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )
    print("Wrote", path)


def main() -> None:
    pipd_dir = PFIZER / "ppid_py"
    risk_dir = PFIZER / "risk_profile_eval"
    if not pipd_dir.is_dir() or not risk_dir.is_dir():
        print("Missing project folders.", file=sys.stderr)
        sys.exit(1)

    pipd = _load_openapi(pipd_dir)
    pipd["servers"] = [{"url": "http://127.0.0.1:9000/pipd", "description": "Gateway (recommended)"}]
    pipd.setdefault("tags", [])
    _dump("pipd.yaml", pipd)

    risk = _load_openapi(risk_dir)
    risk["servers"] = [
        {"url": "http://127.0.0.1:9000/risk-profile", "description": "Gateway (recommended)"},
    ]
    _dump("risk_profile.yaml", risk)


if __name__ == "__main__":
    main()
