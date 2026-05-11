"""
run_eval.py
-----------
D1 Risk Profile Eval Framework – Unified Entry Point (single study).

Auto-detects whether ground truth is available for the study_id and routes to:
  • Scenario 1 (eval_scenario1.py) – if study_id found in risk_profile_ground_truth.csv
  • Scenario 2 (eval_scenario2.py) – if study_id NOT found in ground truth CSV

Writes all output files and prints a summary to stdout.

Usage:
    python3 run_eval.py \\
        --generator_json {study_id}_RiskProfile.json \\
        --ground_truth_risks risk_profile_ground_truth.csv \\
        --ground_truth_factors critical_factors_ground_truth.csv \\
        --study_id {study_id} \\
        --output_dir outputs/ \\
        [--config eval_config/risk_profile_eval_config.yaml] \\
        [--no-yaml] [--no-word]

Output files per study:
    {study_id}_eval_results.json (or {artifact_stem}.json) – Full result dict (includes eval_metadata)
    {artifact_stem}.yaml or eval_report_{study_id}_{date}.yaml – Raw eval config + report (unless --no-yaml)
    Risk_Profile_Eval_Report_{study_id}.docx – Stakeholder Word report (unless --no-word)
    Optional (--no-supplementary skips the next two; data remains inside the main JSON):
    {study_id}_near_misses.csv                – Near miss log (Scenario 1 only)
    {study_id}_hallucination_report.json      – Hallucination findings (both scenarios)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_EVAL_CONFIG = _SCRIPT_DIR.parent / "config" / "risk_profile_eval_config.yaml"

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd

# ── Internal ──────────────────────────────────────────────────────────────────
from core.eval_scenario1 import run_scenario1_eval, classify_failures, VERIFY_STUDIES
from core.eval_scenario2 import run_scenario2_eval
from utils.risk_profile_eval_metadata import build_eval_metadata


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_scenario(study_id: str, ground_truth_risks_csv: str) -> int:
    """
    Determine which evaluation scenario to run for the given study_id.

    Logic:
      - Scenario 1 if the risk GT CSV has rows for this study_id, **or** the study is
        in ``VERIFY_STUDIES``. Missing risk rows for those ids still run Scenario 1 with
        an empty risk GT table (extras scored under M1 / M4).
      - Otherwise → Scenario 2 unless ``run_eval(..., force_scenario1_when_no_gt=True)`` (batch default).

    Args:
        study_id: Study identifier to check
        ground_truth_risks_csv: Path to risk_profile_ground_truth.csv

    Returns:
        Integer 1 (Scenario 1) or 2 (Scenario 2).
    """
    sid = str(study_id).strip()
    verify_ids = {str(x).strip() for x in VERIFY_STUDIES}

    gt_path = Path(ground_truth_risks_csv)
    if gt_path.is_file():
        try:
            df = pd.read_csv(gt_path)
            if sid in set(df["study_id"].astype(str).str.strip().values):
                return 1
        except Exception:
            pass

    if sid in verify_ids:
        return 1

    return 2


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT FILE WRITERS
# ─────────────────────────────────────────────────────────────────────────────

def save_results_json(
    results: Dict,
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: Optional[str] = None,
) -> str:
    """
    Write the full result dict to JSON.

    Default filename: ``{study_id}_eval_results.json``.
    If ``artifact_stem`` is set: ``{artifact_stem}.json`` (e.g. ``risk_profile_eval_<study_id>.json``).

    Args:
        results: Result dict from run_scenario1_eval() or run_scenario2_eval()
        output_dir: Directory to write into
        study_id: Used to name the output file when artifact_stem is omitted
        artifact_stem: Optional basename without extension

    Returns:
        Full path of the written file.
    """
    stem = (artifact_stem or "").strip()
    out_path = (
        Path(output_dir) / f"{stem}.json"
        if stem
        else Path(output_dir) / f"{study_id}_eval_results.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    return str(out_path)


def save_near_misses_csv(
    results: Dict,
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: Optional[str] = None,
) -> Optional[str]:
    """
    Write near-miss records to {study_id}_near_misses.csv (Scenario 1 only).

    Near misses are generated risk names within Levenshtein distance <= 5 of a
    ground truth risk name. They are NOT counted as matches but are logged for the
    generator developer to fix wording in the prompt or YAML library.

    Args:
        results: Scenario 1 result dict (no-op for Scenario 2)
        output_dir: Directory to write into
        study_id: Used to name the output file

    Returns:
        Full path of the written file, or None if no near misses / wrong scenario.
    """
    if results.get("scenario") != 1:
        return None

    near_misses = results.get("near_misses", [])
    stem = (artifact_stem or "").strip()
    out_path = (
        Path(output_dir) / f"{stem}_near_misses.csv"
        if stem
        else Path(output_dir) / f"{study_id}_near_misses.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["study_id", "truth_name", "generated_name", "edit_distance"]
        )
        writer.writeheader()
        for nm in near_misses:
            writer.writerow({
                "study_id": study_id,
                "truth_name": nm["truth_name"],
                "generated_name": nm["generated_name"],
                "edit_distance": nm["edit_distance"],
            })

    return str(out_path)


def save_hallucination_report(
    results: Dict,
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: Optional[str] = None,
) -> str:
    """
    Write the M4 traceability report to {study_id}_hallucination_report.json.

    Written for both scenarios:
      - Scenario 1: from M4 traceability metric
      - Scenario 2: from S1 (hallucination_signals signal)

    An empty flagged_fields list confirms the check ran and found nothing.
    A non-zero count means unresolved provenance/schema traceability and should
    not be treated as a confirmed semantic hallucination without review.

    Args:
        results: Result dict from either scenario
        output_dir: Directory to write into
        study_id: Used to name the output file

    Returns:
        Full path of the written file.
    """
    stem = (artifact_stem or "").strip()
    out_path = (
        Path(output_dir) / f"{stem}_hallucination_report.json"
        if stem
        else Path(output_dir) / f"{study_id}_hallucination_report.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if results.get("scenario") == 1:
        report = results.get("hallucination_report", {
            "study_id": study_id,
            "hallucinations_found": 0,
            "flagged_fields": [],
            "pass": True,
        })
    else:
        s1_signal = results.get("signals", {}).get("S1", {})
        report = {
            "study_id": study_id,
            "hallucinations_found": s1_signal.get("violation_count", 0),
            "flagged_fields": s1_signal.get("violations", []),
            "pass": s1_signal.get("status") == "PASS",
        }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return str(out_path)


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY PRINTER
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: Dict) -> None:
    """
    Print a formatted eval summary to stdout.

    Scenario 1: shows M1–M4 metric scores with PASS/FAIL.
    Scenario 2: shows S1–S7 signal statuses with GREEN/AMBER/RED verdict.

    Args:
        results: Result dict from either scenario
    """
    study_id = results.get("study_id", "?")
    scenario = results.get("scenario", "?")
    verdict = results.get("verdict", "?")
    ta = results.get("ta", "?")
    phase = results.get("phase", "?")

    print(f"\n{'─'*62}")
    print(f"  D1 Risk Profile Eval | {study_id} | Scenario {scenario}")
    print(f"  TA: {ta} | Phase: {phase} | {results.get('timestamp', '')[:10]}")
    print(f"{'─'*62}")

    if scenario == 1:
        metrics = results.get("metrics", {})
        m1 = metrics.get("m1_risk_name_recall", {})
        m2 = metrics.get("m2_rpn_tier_accuracy", {})
        m3 = metrics.get("m3_critical_factor_match", {})
        m4 = metrics.get("m4_hallucination_detection", {})

        def badge(passed, skipped=False):
            if skipped: return "SKIP"
            return "PASS" if passed else "FAIL"

        print(f"  M1 Risk Name Recall:   {m1.get('score', 0):.1%}  ({badge(m1.get('passed'))})")
        m2_sk = bool(m2.get("skipped"))
        if m2_sk:
            print(f"  M2 RPN tier (±1):     N/A  ({badge(m2.get('passed'), True)})")
        else:
            print(
                f"  M2 RPN tier (±1):     {float(m2.get('score') or 0):.1%}  "
                f"({badge(m2.get('passed'))})"
            )
        m3_score = f"{m3.get('score', 0):.1%}" if m3.get("score") is not None else "N/A"
        print(f"  M3 Critical Factors:   {m3_score}  ({badge(m3.get('passed'), m3.get('skipped'))})")
        m4p = m4.get("traceability_flag_count", m4.get("provenance_defect_count", m4.get("hallucinations_found", 0)))
        m4s = m4.get("semantic_hallucination_count", 0)
        print(f"  M4 Traceability flags: {m4p}  ({badge(m4.get('passed'))})  [semantic unmatched: {m4s}]")

        hv = results.get("hierarchy_verification") or {}
        if hv.get("pairs_with_gt_control_count"):
            r = hv.get("control_count_match_rate")
            n = hv.get("pairs_with_gt_control_count")
            r_s = f"{float(r):.1%}" if isinstance(r, (int, float)) else str(r)
            print(f"  Hierarchy (controls):  GT control_count vs gen controls — match rate {r_s} over {n} pair(s)")

        failures = classify_failures(results)
        if failures:
            print(f"\n  Failures ({len(failures)}):")
            for fail in failures:
                print(f"    [{fail['severity']}] {fail['metric']}")
                print(f"      {fail['detail'][:100]}")

        nm = results.get("near_misses", [])
        if nm:
            print(f"\n  Near misses ({len(nm)}) – flag for generator developer:")
            for n in nm:
                print(f"    GT: '{n['truth_name']}' ← Gen: '{n['generated_name']}' (dist={n['edit_distance']})")

    else:
        signals = results.get("signals", {})
        print(f"  {'Signal':<8} {'Name':<36} Status")
        print(f"  {'-'*58}")
        for sid in sorted(signals.keys()):
            sig = signals[sid]
            print(f"  {sid:<8} {sig['name']:<36} {sig['status']}")

        review_count = results.get("review_list_count", 0)
        if review_count:
            print(f"\n  Human review required: {review_count} risk(s)")

    print(f"\n  VERDICT: {verdict}")
    print(f"{'-'*62}\n")


# ─────────────────────────────────────────────────────────────────────────────
# TOP-LEVEL RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def _write_eval_word_report(
    report: Dict,
    output_path: Path,
    *,
    generator_json: Optional[str] = None,
    ground_truth_risks_csv: Optional[str] = None,
    ground_truth_factors_csv: Optional[str] = None,
    usdm_json_path: Optional[str] = None,
) -> str:
    """Write eval Word report; on PermissionError use a timestamped filename. Returns path written.

    Scenario 1 uses the long-standing stakeholder layout. Scenario 2 uses the
    ``rp_scenario2_report`` builder so the docx mirrors
    ``C5091017_rp_eval_report.docx`` (signal scorecard, benchmark slices,
    per-risk USDM traceability, low-confidence review).
    """
    from datetime import datetime as _dt

    scenario = int(report.get("scenario") or 0)
    if scenario == 2 and generator_json:
        from reports.rp_scenario2_report import (
            build_scenario2_payload,
            write_scenario2_report,
        )

        payload = build_scenario2_payload(
            report,
            generator_json,
            ground_truth_risks_csv,
            ground_truth_factors_csv,
            str(report.get("study_id") or ""),
            usdm_json_path=usdm_json_path,
        )
        try:
            path = write_scenario2_report(payload, str(output_path))
            print(f"Word report: {path}")
            return path
        except PermissionError:
            alt = output_path.parent / f"{output_path.stem}_{_dt.now():%Y%m%d_%H%M%S}{output_path.suffix}"
            path = write_scenario2_report(payload, str(alt))
            print(
                f"  [WARN] Could not overwrite locked file: {output_path.name}\n"
                f"  Word report (new file): {path}"
            )
            return path

    from reports.risk_profile_eval_report_docx import write_risk_profile_eval_docx

    try:
        write_risk_profile_eval_docx(report, str(output_path))
        print(f"Word report: {output_path.resolve()}")
        return str(output_path.resolve())
    except PermissionError:
        alt = output_path.parent / f"{output_path.stem}_{_dt.now():%Y%m%d_%H%M%S}{output_path.suffix}"
        write_risk_profile_eval_docx(report, str(alt))
        print(
            f"  [WARN] Could not overwrite locked file: {output_path.name}\n"
            f"  Word report (new file): {alt.resolve()}"
        )
        return str(alt.resolve())


def run_eval(
    generator_json: str,
    ground_truth_risks_csv: str,
    ground_truth_factors_csv: str,
    study_id: str,
    output_dir: str,
    *,
    config_path: Optional[str] = None,
    write_yaml: bool = True,
    write_word: bool = True,
    artifact_stem: Optional[str] = None,
    write_supplementary: bool = True,
    force_scenario1_when_no_gt: bool = False,
    usdm_json_path: Optional[str] = None,
) -> Dict:
    """
    Run the complete evaluation pipeline for one study.

    Auto-detects scenario, delegates to the correct eval module, saves all output
    files, prints summary to stdout, and returns the result dict.

    This function is the single contract for both CLI and API invocations.

    Args:
        generator_json: Path to {study_id}_RiskProfile.json
        ground_truth_risks_csv: Path to risk_profile_ground_truth.csv
        ground_truth_factors_csv: Path to critical_factors_ground_truth.csv
        study_id: Study identifier
        output_dir: Directory for all output files
        config_path: Eval YAML (default: eval_config/risk_profile_eval_config.yaml)
        write_yaml: Emit eval_report_{study}_{date}.yaml (or {artifact_stem}.yaml when set)
        write_word: Emit Risk_Profile_Eval_Report_{study}.docx (or {artifact_stem}.docx when set)
        artifact_stem: Optional basename for JSON/CSV/JSON hall/YAML/DOCX bundle naming
        write_supplementary: If False, skip near_misses.csv and hallucination_report.json (main JSON still includes data)

    Returns:
        Result dict (structure depends on scenario).

    Raises:
        FileNotFoundError: If generator JSON is missing
        ValueError: If study_id has unexpected format or missing data
    """
    # Auto-detect scenario
    scenario = detect_scenario(study_id, ground_truth_risks_csv)
    if scenario == 2 and force_scenario1_when_no_gt:
        scenario = 1

    if scenario == 1:
        results = run_scenario1_eval(
            generator_json_path=generator_json,
            ground_truth_risks_csv=ground_truth_risks_csv,
            ground_truth_factors_csv=ground_truth_factors_csv,
            study_id=study_id,
            # Allow zero risk GT rows: generator-only risks are scored as extras (M1/M4), not a load error.
            allow_empty_risk_gt=True,
            usdm_json_path=usdm_json_path,
        )
    else:
        results = run_scenario2_eval(
            generator_json_path=generator_json,
            study_id=study_id,
        )
        # Compute a weighted "signal health" % so Scenario 2 has a headline
        # number alongside the traffic-light verdict (matches the hero tile
        # the UI expects).
        try:
            from reports.rp_scenario2_report import compute_signal_health

            _health = compute_signal_health(results.get("signals") or {})
            results["signal_health"] = _health
            results["overall_score_percent"] = _health.get("percent")
        except Exception:
            pass

    cfg_path = Path(config_path) if config_path else _DEFAULT_EVAL_CONFIG
    config_dict: Dict = {}
    if cfg_path.is_file():
        try:
            import yaml  # type: ignore

            with open(cfg_path, encoding="utf-8") as yf:
                config_dict = yaml.safe_load(yf) or {}
        except Exception as exc:
            print(f"  [WARN] Could not load eval config {cfg_path}: {exc}")

    artifact_paths = {
        "config": str(cfg_path.resolve()),
        "generator": str(Path(generator_json).resolve()),
        "risks": str(Path(ground_truth_risks_csv).resolve()) if ground_truth_risks_csv else "",
        "factors": str(Path(ground_truth_factors_csv).resolve()) if ground_truth_factors_csv else "",
    }
    results["eval_metadata"] = build_eval_metadata(
        results, config_dict, cfg_path, artifact_paths
    )
    if usdm_json_path and str(usdm_json_path).strip():
        results["eval_metadata"]["usdm_protocol_json_path"] = str(
            Path(usdm_json_path).resolve()
        )

    # Save output files (canonical names: risk_profile_eval_{study_id}.* / Risk_Profile_Eval_Report_{study_id}.docx)
    stem_eff = (artifact_stem or "").strip() or f"risk_profile_eval_{study_id}"
    results_path = save_results_json(
        results, output_dir, study_id, artifact_stem=stem_eff
    )
    near_miss_path = (
        save_near_misses_csv(results, output_dir, study_id, artifact_stem=stem_eff)
        if write_supplementary
        else None
    )
    hallucination_path = (
        save_hallucination_report(results, output_dir, study_id, artifact_stem=stem_eff)
        if write_supplementary
        else None
    )

    extra_outputs: list[str] = []

    if write_yaml:
        try:
            from reports.risk_profile_eval_report_yaml import write_risk_profile_eval_report_yaml

            yp = Path(output_dir) / f"{stem_eff}.yaml"
            write_risk_profile_eval_report_yaml(yp, results, config_path=cfg_path)
            extra_outputs.append(str(yp.resolve()))
            print(f"YAML artifact: {yp.resolve()}")
        except Exception as exc:
            print(f"  [WARN] YAML artifact skipped: {exc}")

    if write_word:
        try:
            wp = Path(output_dir) / f"Risk_Profile_Eval_Report_{study_id}.docx"
            word_path = _write_eval_word_report(
                results,
                wp,
                generator_json=generator_json,
                ground_truth_risks_csv=ground_truth_risks_csv,
                ground_truth_factors_csv=ground_truth_factors_csv,
                usdm_json_path=usdm_json_path,
            )
            extra_outputs.append(word_path)
        except ImportError as e:
            print(f"  [SKIP] Word report needs python-docx: {e}")
        except Exception as exc:
            print(f"  [WARN] Word report skipped: {exc}")

    results["output_files"] = list(
        filter(None, [results_path, near_miss_path, hallucination_path, *extra_outputs])
    )

    # Print summary
    print_summary(results)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="D1 Risk Profile – Unified Eval Runner (auto-detects scenario)"
    )
    parser.add_argument("--generator_json", required=True,
                        help="Path to {study_id}_RiskProfile.json")
    parser.add_argument("--ground_truth_risks", required=True,
                        help="Path to risk_profile_ground_truth.csv")
    parser.add_argument("--ground_truth_factors", required=True,
                        help="Path to critical_factors_ground_truth.csv")
    parser.add_argument("--study_id", required=True,
                        help="Study / protocol identifier (e.g. C5091017)")
    parser.add_argument("--output_dir", default="outputs/",
                        help="Directory for output files (default: outputs/)")
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_EVAL_CONFIG),
        help="Path to risk_profile_eval_config.yaml",
    )
    parser.add_argument("--no-yaml", action="store_true", help="Skip eval_report_{study}_{date}.yaml")
    parser.add_argument("--no-word", action="store_true", help="Skip Risk_Profile_Eval_Report_{study}.docx")
    parser.add_argument(
        "--no-supplementary",
        action="store_true",
        help="Do not write near_misses.csv or hallucination_report.json (only main JSON + optional YAML/Word)",
    )
    parser.add_argument(
        "--artifact-stem",
        default=None,
        help="Basename (no ext) for outputs: {stem}.json, {stem}_near_misses.csv, etc.",
    )
    parser.add_argument(
        "--force-scenario1-when-no-gt",
        action="store_true",
        help="Run Scenario 1 even when study_id has no risk GT rows; treats GT as empty (expected zero risks).",
    )
    parser.add_argument(
        "--usdm-json",
        default=None,
        help="Optional USDM protocol JSON path (recorded in eval_metadata; study id should match generator).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_eval(
        generator_json=args.generator_json,
        ground_truth_risks_csv=args.ground_truth_risks,
        ground_truth_factors_csv=args.ground_truth_factors,
        study_id=args.study_id,
        output_dir=args.output_dir,
        config_path=args.config,
        write_yaml=not args.no_yaml,
        write_word=not args.no_word,
        artifact_stem=args.artifact_stem,
        write_supplementary=not args.no_supplementary,
        force_scenario1_when_no_gt=args.force_scenario1_when_no_gt,
        usdm_json_path=args.usdm_json,
    )
