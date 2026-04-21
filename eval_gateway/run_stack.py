"""
Start PIPD API (8001), Risk Profile API (8002), and gateway (9000) in one command.

Usage (from eval_gateway directory):
    python run_stack.py

Requires dependencies installed in the same environment as ppid_py and risk_profile_eval
(pandas, Levenshtein, fastapi, etc.) — install both projects' requirements.txt first.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PFIZER = ROOT.parent
PPID = PFIZER / "ppid_py"
RISK = PFIZER / "risk_profile_eval"


def main() -> None:
    if not PPID.is_dir() or not RISK.is_dir():
        print("Expected folders:", PPID, RISK, file=sys.stderr)
        sys.exit(1)

    py = sys.executable
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")

    procs = [
        subprocess.Popen(
            [py, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8001"],
            cwd=str(PPID),
            env=env,
        ),
        subprocess.Popen(
            [py, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8002"],
            cwd=str(RISK),
            env=env,
        ),
    ]

    time.sleep(2)

    gw = subprocess.Popen(
        [py, "-m", "uvicorn", "proxy_app:app", "--host", "0.0.0.0", "--port", "9000"],
        cwd=str(ROOT),
        env=env,
    )
    procs.append(gw)

    print("PIPD backend:      http://127.0.0.1:8001/docs")
    print("Risk backend:      http://127.0.0.1:8002/docs")
    print("Gateway (use me): http://127.0.0.1:9000/health")
    print("  PIPD via gateway:        http://127.0.0.1:9000/pipd/docs")
    print("  Risk Profile via gateway: http://127.0.0.1:9000/risk-profile/docs")
    print("Ctrl+C stops all.\n")

    try:
        gw.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                p.kill()


if __name__ == "__main__":
    main()
