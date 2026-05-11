"""Backward-compatible entrypoint. Prefer ``python tools/run_scenario2_all.py``."""

from tools.run_scenario2_all import main

if __name__ == "__main__":
    raise SystemExit(main())
