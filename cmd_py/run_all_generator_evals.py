#!/usr/bin/env python3
"""
Run the four generator evals (CMP, DMP, PIPD Scenario 1, Risk Profile).

Automation idea
---------------
Each product (CMP, DMP, PIPD, Risk) is a **different codebase** with different ground truth —
that cannot be one script inside one repo. What *can* be automated is **one driver** that:

  • Resolves inputs for a **single study id** (``--study``) using the same conventions
  • Runs all four subprocesses and writes **json + yaml + docx** per product
  • Optionally **checks** that files exist before running (``--check``)

Outputs (default): ``<PFIZER_ROOT>/generator_eval_outputs/{cmp,dmp,pipd,risk}/``

**Protocol bundle folder** (name is ``protocol_bundles``, plural):

  ``<PFIZER_ROOT>/protocol_eval_hub/protocol_bundles/<STUDY_ID>/``

That is where per-protocol generator JSONs live (``*_CMP.json``, ``*_DMP.json``, etc.).

**Bundle mode** (``--bundle C4891002``): reads those four JSONs **only** from that folder and writes to:

  ``protocol_eval_hub/protocol_bundles/<STUDY>/eval_outputs/<STUDY>/{cmp,dmp,pipd,risk}/``

Ground-truth CSVs still come from each repo's ``data/`` (or env overrides).

For manifest discovery / USDM / multi-bundle runs, use the hub driver instead:

  ``cd protocol_eval_hub && python run_protocol_eval_bundle.py --bundle protocol_bundles/<STUDY_ID>``

No supplementary files: Risk uses ``--no-supplementary``; PIPD uses ``--artifacts json_docx``.

Environment overrides (still supported): PFIZER_ROOT, PROTOCOL_BUNDLES_DIR (default
``…/protocol_eval_hub/protocol_bundles``), CMP_INPUT_JSON, DMP_INPUT_JSON,
PIPD_INPUT_JSON, PIPD_GT_CSV, RISK_JSON, RISK_GT_RISKS_CSV, RISK_GT_FACTORS_CSV, EVAL_OUTPUT_ROOT.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PFIZER = Path(os.environ.get("PFIZER_ROOT", str(_HERE.parent))).resolve()


def _protocol_bundles_dir() -> Path:
    """Parent directory: ``.../protocol_eval_hub/protocol_bundles`` (not ``protocol_bundle``)."""
    env = os.environ.get("PROTOCOL_BUNDLES_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return (_PFIZER / "protocol_eval_hub" / "protocol_bundles").resolve()


def _hub_bundle(study: str) -> Path:
    return _protocol_bundles_dir() / study


def _resolve_cmp(study: str) -> Path:
    if os.environ.get("CMP_INPUT_JSON"):
        return Path(os.environ["CMP_INPUT_JSON"])
    hub = _hub_bundle(study) / f"{study}_CMP.json"
    gen = _HERE / "outputs" / "generated" / f"{study}_CMP.json"
    if hub.is_file():
        return hub
    if gen.is_file():
        return gen
    return hub  # preferred default path for messages


def _resolve_dmp(study: str) -> Path:
    if os.environ.get("DMP_INPUT_JSON"):
        return Path(os.environ["DMP_INPUT_JSON"])
    hub = _hub_bundle(study) / f"{study}_DMP.json"
    if hub.is_file():
        return hub
    return hub


def _resolve_pipd(study: str) -> tuple[Path, Path]:
    pj = Path(os.environ["PIPD_INPUT_JSON"]) if os.environ.get("PIPD_INPUT_JSON") else _PFIZER / "ppid_py" / "data" / f"{study}_PIPD.json"
    gt = Path(os.environ["PIPD_GT_CSV"]) if os.environ.get("PIPD_GT_CSV") else _PFIZER / "ppid_py" / "data" / "pipd_ground_truth.csv"
    return pj, gt


def _resolve_risk(study: str) -> tuple[Path, Path, Path]:
    rj = Path(os.environ["RISK_JSON"]) if os.environ.get("RISK_JSON") else _PFIZER / "risk_profile_eval" / "data" / f"{study}_RiskProfile.json"
    risks = (
        Path(os.environ["RISK_GT_RISKS_CSV"])
        if os.environ.get("RISK_GT_RISKS_CSV")
        else _PFIZER / "risk_profile_eval" / "data" / "risk_profile_ground_truth.csv"
    )
    fac = (
        Path(os.environ["RISK_GT_FACTORS_CSV"])
        if os.environ.get("RISK_GT_FACTORS_CSV")
        else _PFIZER / "risk_profile_eval" / "data" / "critical_factors_ground_truth.csv"
    )
    return rj, risks, fac


def _run(name: str, cwd: Path, args: list[str]) -> int:
    cmd = [sys.executable, *args]
    print(f"\n{'='*60}\n{name}\n$ cd {cwd} && ", " ".join(args), "\n", sep="", flush=True)
    return subprocess.call(cmd, cwd=str(cwd))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run CMP, DMP, PIPD, and Risk evals for one study (automated path resolution)."
    )
    ap.add_argument(
        "--study",
        default="B7981027",
        help="Study id for DMP / PIPD / Risk default paths (e.g. B7981027_DMP.json). Default: B7981027",
    )
    ap.add_argument(
        "--study-cmp",
        default=None,
        metavar="ID",
        help="Optional study id for CMP only (e.g. C4891002 when CMP lives in protocol_hub but other artifacts use --study)",
    )
    ap.add_argument(
        "--output-root",
        default=os.environ.get("EVAL_OUTPUT_ROOT", str(_PFIZER / "generator_eval_outputs")),
        help="Root folder for cmp/, dmp/, pipd/, risk/ subfolders",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Only print resolved paths and exit 0 if all four CMP/DMP/PIPD/Risk inputs exist (else exit 1)",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the resolved-inputs table before running (subprocess logs still appear)",
    )
    ap.add_argument(
        "--bundle",
        default=None,
        metavar="STUDY_ID",
        help="Single protocol bundle under protocol_hub/protocol_bundles/{ID}/ — all four generator JSONs from that folder; outputs go to bundle/eval_outputs/",
    )
    ap.add_argument(
        "--clean",
        action="store_true",
        help="Remove the output root (bundle/.../eval_outputs or --output-root) before running",
    )
    args = ap.parse_args()

    if args.bundle:
        study = str(args.bundle).strip().upper()
        cmp_study = study
        bd = _hub_bundle(study)
        # Match protocol_eval_hub/run_protocol_eval_bundle.py: …/protocol_bundles/<STUDY>/eval_outputs/<STUDY>/
        out = (bd / "eval_outputs" / study).resolve()
        cmp_json = Path(os.environ["CMP_INPUT_JSON"]) if os.environ.get("CMP_INPUT_JSON") else bd / f"{study}_CMP.json"
        dmp_json = Path(os.environ["DMP_INPUT_JSON"]) if os.environ.get("DMP_INPUT_JSON") else bd / f"{study}_DMP.json"
        pipd_json = Path(os.environ["PIPD_INPUT_JSON"]) if os.environ.get("PIPD_INPUT_JSON") else bd / f"{study}_PIPD.json"
        _, pipd_gt = _resolve_pipd(study)
        risk_json = Path(os.environ["RISK_JSON"]) if os.environ.get("RISK_JSON") else bd / f"{study}_RiskProfile.json"
        _, risk_risks, risk_fac = _resolve_risk(study)
    else:
        study = str(args.study).strip().upper()
        cmp_study = str(args.study_cmp).strip().upper() if args.study_cmp else study
        out = Path(args.output_root).resolve()
        cmp_json = _resolve_cmp(cmp_study)
        dmp_json = _resolve_dmp(study)
        pipd_json, pipd_gt = _resolve_pipd(study)
        risk_json, risk_risks, risk_fac = _resolve_risk(study)

    if args.clean and out.exists():
        try:
            shutil.rmtree(out)
        except PermissionError:
            # Windows: Word/Explorer may lock .docx under eval_outputs
            stale = out.parent / f"{out.name}_stale_{int(time.time())}"
            try:
                out.rename(stale)
                print(
                    f"[WARN] Output dir was in use; renamed old folder to:\n  {stale}\n"
                    "  Close open documents and delete the stale folder when convenient.",
                    file=sys.stderr,
                    flush=True,
                )
            except OSError as exc:
                print(
                    f"[WARN] Could not clear output dir (files in use): {out}\n  ({exc})\n"
                    "  Continuing — existing files may be overwritten.",
                    file=sys.stderr,
                    flush=True,
                )
    out.mkdir(parents=True, exist_ok=True)

    if not args.quiet or args.check:
        print("Resolved inputs (override with env vars if needed):")
        print(f"  study:     {study}" + (f"  (CMP uses {cmp_study})" if cmp_study != study else ""))
        print(f"  CMP:       {cmp_json} {'✓' if cmp_json.is_file() else 'MISSING'}")
        print(f"  DMP:       {dmp_json} {'✓' if dmp_json.is_file() else 'MISSING'}")
        print(f"  PIPD:      {pipd_json} {'✓' if pipd_json.is_file() else 'MISSING'}")
        print(f"  PIPD GT:   {pipd_gt} {'✓' if pipd_gt.is_file() else 'MISSING'}")
        print(f"  Risk:      {risk_json} {'✓' if risk_json.is_file() else 'MISSING'}")
        print(f"  Risk GT:   {risk_risks} {'✓' if risk_risks.is_file() else 'MISSING'}")
        print(f"  Factors GT:{risk_fac} {'✓' if risk_fac.is_file() else 'MISSING'}")
        print(f"  output:    {out}")

    ok_cmp = cmp_json.is_file()
    ok_dmp = dmp_json.is_file()
    ok_pipd = pipd_json.is_file() and pipd_gt.is_file()
    ok_risk = risk_json.is_file() and risk_risks.is_file() and risk_fac.is_file()
    all_ok = ok_cmp and ok_dmp and ok_pipd and ok_risk

    if args.check:
        return 0 if all_ok else 1

    rc = 0

    cmp_dir = out / "cmp"
    cmp_dir.mkdir(parents=True, exist_ok=True)
    if ok_cmp:
        rc |= _run(
            "CMP",
            _HERE,
            [
                "eval_d3_cmp.py",
                "--input",
                str(cmp_json),
                "--output-dir",
                str(cmp_dir),
                "--no-print",
            ],
        )
    else:
        print(f"[SKIP] CMP — missing: {cmp_json}")

    dmp_dir = out / "dmp"
    dmp_dir.mkdir(parents=True, exist_ok=True)
    if ok_dmp:
        rc |= _run(
            "DMP",
            _PFIZER / "DMP_py",
            [
                "eval_d4_dmp.py",
                "--input",
                str(dmp_json),
                "--output-dir",
                str(dmp_dir),
            ],
        )
    else:
        print(f"[SKIP] DMP — missing: {dmp_json}")

    pipd_dir = out / "pipd"
    pipd_dir.mkdir(parents=True, exist_ok=True)
    if ok_pipd:
        rc |= _run(
            "PIPD",
            _PFIZER / "ppid_py",
            [
                "pipd_scenario1_report.py",
                "--generator_json",
                str(pipd_json),
                "--ground_truth",
                str(pipd_gt),
                "--study_id",
                study,
                "--output_dir",
                str(pipd_dir),
                "--artifacts",
                "json_docx",
            ],
        )
    else:
        print(f"[SKIP] PIPD — missing pipd or ground-truth CSV")

    risk_dir = out / "risk"
    risk_dir.mkdir(parents=True, exist_ok=True)
    # run_eval.py auto-detects Scenario 1 (study in GT) or Scenario 2 (no GT).

    if ok_risk:
        stem = f"risk_profile_eval_{study}"
        rc |= _run(
            "Risk Profile",
            _PFIZER / "risk_profile_eval",
            [
                "run_eval.py",
                "--generator_json",
                str(risk_json),
                "--ground_truth_risks",
                str(risk_risks),
                "--ground_truth_factors",
                str(risk_fac),
                "--study_id",
                study,
                "--output_dir",
                str(risk_dir),
                "--artifact-stem",
                stem,
                "--no-supplementary",
                "--force-scenario1-when-no-gt",
            ],
        )
    else:
        print(f"[SKIP] Risk — missing risk JSON or ground-truth CSVs")

    print(f"\nDone. Outputs under: {out.resolve()}")
    if rc:
        print(
            f"[INFO] Combined subprocess exit code {min(rc, 255)} "
            "(often 1 when a document score fails thresholds; outputs may still be written)."
        )
    return min(rc, 255)


if __name__ == "__main__":
    raise SystemExit(main())
