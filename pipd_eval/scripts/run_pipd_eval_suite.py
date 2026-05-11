#!/usr/bin/env python3
"""
Run **Scenario 1**, **Scenario 2**, and **composite + intelligence** reports for one study.

Scenario 2 needs a deviation benchmark CSV (historical subcategory stats). Default points to
``../pipd_risk/deviation_subcategories.csv`` when present; override with ``--deviation_benchmarks``.

Example::

    python run_pipd_eval_suite.py --study_id B7981027
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from core.eval_scenario1 import classify_failures, run_scenario1_eval
from core.eval_scenario2 import run_scenario2_eval

_PKG = Path(__file__).resolve().parent


def main() -> int:
    p = argparse.ArgumentParser(description="Run PIPD Scenario 1 + 2 + composite/intelligence suite")
    p.add_argument("--study_id", default="B7981027")
    p.add_argument("--generator_json", default=None, help="Default: data/{study_id}_PIPD.json")
    p.add_argument("--ground_truth", default=None, help="Default: data/pipd_ground_truth.csv")
    p.add_argument(
        "--deviation_benchmarks",
        default=None,
        help="Default: ../pipd_risk/deviation_subcategories.csv if it exists",
    )
    p.add_argument("--output_dir", default="eval_outputs")
    p.add_argument("--no-composite", action="store_true", help="Skip pipd_composite_report.py")
    p.add_argument("--no-openai", action="store_true", help="Pass --no-openai to composite report")
    p.add_argument("--no-docx", action="store_true", help="Pass --no-docx to composite report")
    args = p.parse_args()

    sid = args.study_id
    gen = args.generator_json or str(_PKG / "data" / f"{sid}_PIPD.json")
    gt = args.ground_truth or str(_PKG / "data" / "pipd_ground_truth.csv")
    bench = args.deviation_benchmarks
    if not bench:
        cand = _PKG.parent / "pipd_risk" / "deviation_subcategories.csv"
        bench = str(cand) if cand.is_file() else ""

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    if not Path(gen).is_file():
        print(f"[ERROR] Generator JSON not found: {gen}", file=sys.stderr)
        return 2
    if not Path(gt).is_file():
        print(f"[ERROR] Ground truth CSV not found: {gt}", file=sys.stderr)
        return 2

    print("[INFO] Scenario 1 (ground truth)...")
    r1 = run_scenario1_eval(gen, gt, sid)
    r1["classified_failures"] = classify_failures(r1)
    p1 = out / f"{sid}_scenario1_results.json"
    p1.write_text(json.dumps(r1, indent=2, default=str), encoding="utf-8")
    paths["scenario1_json"] = str(p1)
    print(f"       -> {p1} | {r1.get('go_no_go')}")

    if not bench or not Path(bench).is_file():
        print("[WARN] No deviation benchmark CSV — skipping Scenario 2.", file=sys.stderr)
        print("       Set --deviation_benchmarks to deviation_subcategories*.csv", file=sys.stderr)
    else:
        print("[INFO] Scenario 2 (proxy signals + USDM provenance)...")
        r2 = run_scenario2_eval(gen, bench, sid)
        p2 = out / f"{sid}_scenario2_results.json"
        p2.write_text(json.dumps(r2, indent=2, default=str), encoding="utf-8")
        paths["scenario2_json"] = str(p2)
        print(f"       -> {p2} | {r2.get('overall_verdict', {}).get('verdict')}")

    if not args.no_composite:
        print("[INFO] Composite + reference report + intelligence truth...")
        cmd = [
            sys.executable,
            str(_PKG / "pipd_composite_report.py"),
            "--generator_json",
            gen,
            "--ground_truth",
            gt,
            "--study_id",
            sid,
            "--output_dir",
            str(out),
        ]
        if args.no_openai:
            cmd.append("--no-openai")
        if args.no_docx:
            cmd.append("--no-docx")
        subprocess.run(cmd, check=True, cwd=str(_PKG))

    print(json.dumps(paths, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
