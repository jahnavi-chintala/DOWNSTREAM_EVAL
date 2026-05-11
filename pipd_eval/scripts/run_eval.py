"""
run_eval.py
-----------
PIPD Eval Framework – Unified Entry Point.

Auto-detects which scenario to run based on whether the study has
ground-truth rows in pipd_ground_truth_clean.csv (split=verify):

  • Study found in CSV (split=verify) → Scenario 1 (ground truth eval)
  • Study NOT found                   → Scenario 2 (proxy signal eval)

Output: JSON (+ Scenario 1 near-misses / hallucination JSON).  Scenario 1 also
emits combined eval YAML and reference Word unless ``--no-yaml`` / ``--no-word``.
Use ``--scenario1-only`` to skip studies that are not in verify ground truth.

Usage:
    python3 run_eval.py \\
        --generator_json B7981027_PIPD.json \\
        --ground_truth pipd_ground_truth_clean.csv \\
        --deviation_benchmarks deviation_subcategories_clean.csv \\
        --study_id B7981027 \\
        --output B7981027_eval_report.json
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import argparse
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd   # used only for scenario detection (CSV read)

# ── Internal modules ──────────────────────────────────────────────────────────
from core.eval_scenario1 import run_scenario1_eval, classify_failures, filter_verify_split_rows
from core.eval_scenario2 import run_scenario2_eval
from core.pipd_semantic_review import (
    apply_semantic_review_to_m1,
    run_semantic_review_for_results,
)


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_scenario(study_id: str, ground_truth_csv_path: str) -> int:
    """
    Determine which evaluation scenario applies to this study.

    Logic:
      1. Load pipd_ground_truth_clean.csv
      2. Filter to split='verify'
      3. If study_id appears in filtered rows → Scenario 1
      4. Otherwise → Scenario 2

    Args:
        study_id              : Study identifier e.g. 'B7981027'
        ground_truth_csv_path : Path to pipd_ground_truth_clean.csv

    Returns:
        1 (Scenario 1 – ground truth available) or
        2 (Scenario 2 – no ground truth, use proxy signals)
    """
    gt_path = Path(ground_truth_csv_path)
    if not gt_path.exists():
        print(f"[WARN] Ground truth CSV not found at '{gt_path}'. Defaulting to Scenario 2.")
        return 2

    df = pd.read_csv(gt_path, dtype=str)
    if "study_folder" not in df.columns:
        print(f"[WARN] Ground truth CSV has no 'study_folder' column. Defaulting to Scenario 2.")
        return 2

    df = filter_verify_split_rows(df)
    verify_ids = set(df["study_folder"].str.strip())

    if study_id in verify_ids:
        return 1
    return 2


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_results_json(results: Dict, output_path: str) -> None:
    """
    Persist the evaluation results dictionary as a formatted JSON file.

    Args:
        results     : Results dict from run_scenario1_eval() or run_scenario2_eval()
        output_path : Filesystem path for the output file
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"[INFO] Results JSON -> {out}")


def _save_semantic_review_json(
    results: Dict, output_dir: str, *, artifact_stem: Optional[str] = None
) -> None:
    block = results.get("semantic_review")
    if not block:
        return
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = results["study_id"]
    stem = (artifact_stem or "").strip()
    path = (
        out_dir / f"{stem}_semantic_review.json"
        if stem
        else out_dir / f"semantic_review_{sid}.json"
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(block, fh, indent=2, ensure_ascii=False, default=str)
    print(f"[INFO] Semantic review JSON -> {path}")


def save_near_misses_csv(
    results: Dict, output_dir: str, *, artifact_stem: Optional[str] = None
) -> None:
    """Write Scenario 1 near_misses to CSV (generated vs GT pair only)."""
    near_misses = results.get("near_misses", [])
    if not near_misses:
        return

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = results["study_id"]
    stem = (artifact_stem or "").strip()
    csv_path = (
        out_dir / f"{stem}_near_misses.csv"
        if stem
        else out_dir / f"near_misses_{sid}.csv"
    )

    rows = []
    for nm in near_misses:
        rows.append({
            "study_id":       results["study_id"],
            "category_num":   nm.get("category_num"),
            "tier":           nm.get("tier"),
            "root_cause":     nm.get("root_cause"),
            "credit":         nm.get("credit"),
            "generated_text": nm.get("generated_text"),
            "gt_text":        nm.get("gt_text"),
        })

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"[INFO] Near misses CSV -> {csv_path}")


def save_hallucination_report(
    results: Dict, output_dir: str, *, artifact_stem: Optional[str] = None
) -> None:
    """
    Write the M4 traceability report as a per-study JSON file.

    Keeps the legacy ``hallucinations_found`` field for compatibility, but the
    count represents missing or unresolved USDM provenance.

    Args:
        results    : Results dict (Scenario 1 or 2)
        output_dir : Directory where hallucination_report_{study_id}.json is written
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    study_id = results["study_id"]
    scenario = results.get("scenario", 1)

    if scenario == 1:
        m4 = results["metrics"]["m4_hallucination_detection"]
        report = {
            "study_id":              study_id,
            "scenario":              1,
            "traceability_flag_count": m4.get("traceability_flag_count", m4["hallucinations_found"]),
            "hallucinations_found":  m4["hallucinations_found"],
            "flagged_subcategories": m4["flagged_subcategories"],
            "traceability_flags":    m4.get("traceability_flags", m4["flagged_subcategories"]),
            "pass":                  m4["pass"],
            "note":                  m4.get("note"),
        }
    else:
        s1 = results["signals"].get("S1_HALLUCINATION_CHECK", {})
        report = {
            "study_id":              study_id,
            "scenario":              2,
            "hallucinations_found":  len(s1.get("flagged", [])),
            "flagged_subcategories": s1.get("flagged", []),
            "pass":                  s1.get("status") == "PASS",
        }

    stem = (artifact_stem or "").strip()
    report_path = (
        out_dir / f"{stem}_hallucination_report.json"
        if stem
        else out_dir / f"hallucination_report_{study_id}.json"
    )
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"[INFO] Traceability report -> {report_path}")


def print_summary(results: Dict) -> None:
    """
    Print a concise terminal summary of the evaluation results.

    Args:
        results : Results dict from either scenario eval
    """
    study_id = results["study_id"]
    scenario = results.get("scenario", 1)
    verdict  = results.get("go_no_go", "UNKNOWN")

    print(f"\n{'='*60}")
    print(f"  PIPD Eval Summary | {study_id} | Scenario {scenario}")
    print(f"{'='*60}")

    if scenario == 1:
        m = results["metrics"]
        rows = [
            ("M1 Subcategory Recall",   f"{m['m1_subcategory_recall']['score']:.2%}",   m['m1_subcategory_recall']['pass']),
            ("M2 Flag Accuracy",         f"{m['m2_flag_accuracy']['auto_confirmed_accuracy']:.2%}", m['m2_flag_accuracy']['pass']),
            ("M3 Empty Category",        f"{m['m3_empty_category_accuracy']['score']:.2%}", m['m3_empty_category_accuracy']['pass']),
            (
                "M4 Traceability Flags",
                str(m['m4_hallucination_detection'].get(
                    'traceability_flag_count',
                    m['m4_hallucination_detection']['hallucinations_found'],
                )),
                m['m4_hallucination_detection']['pass'],
            ),
        ]
        for name, value, passed in rows:
            icon = "[OK]" if passed else "[X]"
            print(f"  {icon} {name:<30} {value}")
    else:
        v = results["overall_verdict"]
        print(f"  Proxy signals: {v['pass_count']} PASS | {v['warn_count']} WARN | {v['fail_count']} FAIL")
        print(f"  Human review items: {results['human_review_count']}")
        for note in v.get("remediation_notes", []):
            print(f"  {note}")

    print(f"{'-'*60}")
    print(f"  Verdict: {verdict}")
    print(f"{'='*60}\n")


def _emit_scenario2_yaml_word(
    results: Dict[str, Any],
    generator_json_path: str,
    deviation_benchmarks_path: str,
    study_id: str,
    aux_dir: str,
    *,
    usdm_json_path: Optional[str],
    artifact_stem: Optional[str],
    write_yaml: bool,
    write_word: bool,
) -> None:
    if not write_yaml and not write_word:
        return
    try:
        from reports.pipd_scenario2_report import (
            build_scenario2_report_payload,
            write_scenario2_yaml_and_word,
        )

        payload = build_scenario2_report_payload(
            results,
            generator_json_path,
            deviation_benchmarks_path,
            study_id,
            usdm_json_path=usdm_json_path,
        )
        extra = write_scenario2_yaml_and_word(
            payload,
            aux_dir,
            study_id,
            artifact_stem=artifact_stem,
            write_yaml=write_yaml,
            write_docx=write_word,
        )
        for k, v in extra.items():
            if k.endswith("_error"):
                print(f"  [WARN] Scenario 2 report {k}: {v}")
            elif k == "docx_note":
                print(f"  [INFO] {v}")
            else:
                print(f"[INFO] Scenario 2 report {k} -> {v}")
    except Exception as exc:
        print(f"  [WARN] Scenario 2 YAML/Word skipped: {exc}")


def _emit_scenario1_yaml_word(
    results: Dict[str, Any],
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    aux_dir: str,
    *,
    usdm_json_path: Optional[str],
    artifact_stem: Optional[str],
    write_yaml: bool,
    write_word: bool,
) -> None:
    if not write_yaml and not write_word:
        return
    try:
        from reports.pipd_scenario1_report import (
            build_scenario1_report_payload,
            write_scenario1_yaml_and_word,
        )

        payload = build_scenario1_report_payload(
            results,
            generator_json_path,
            ground_truth_csv_path,
            study_id,
            usdm_json_path=usdm_json_path,
            include_structure_check=True,
        )
        extra = write_scenario1_yaml_and_word(
            payload,
            aux_dir,
            study_id,
            artifact_stem=artifact_stem,
            write_yaml=write_yaml,
            write_docx=write_word,
        )
        for k, v in extra.items():
            if k.endswith("_error"):
                print(f"  [WARN] Scenario 1 report {k}: {v}")
            elif k == "docx_note":
                print(f"  [INFO] {v}")
            else:
                print(f"[INFO] Scenario 1 report {k} -> {v}")
    except Exception as exc:
        print(f"  [WARN] Scenario 1 YAML/Word skipped: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_eval(
    generator_json_path: str,
    ground_truth_csv_path: str,
    deviation_benchmarks_path: str,
    study_id: str,
    output_path: str,
    output_dir: Optional[str] = None,
    usdm_json_path: Optional[str] = None,
    artifact_stem: Optional[str] = None,
    *,
    write_report_yaml: bool = True,
    write_report_docx: bool = True,
    scenario1_only: bool = False,
    with_semantic_review: bool = False,
    semantic_review_affects_m1: bool = False,
) -> Dict:
    """
    Unified evaluation runner.  Auto-detects scenario and delegates to the
    appropriate eval function.

    Args:
        generator_json_path       : Path to {study_id}_PIPD.json
        ground_truth_csv_path     : Path to pipd_ground_truth_clean.csv
        deviation_benchmarks_path : Path to deviation_subcategories_clean.csv
        study_id                  : Study identifier e.g. 'B7981027'
        output_path               : Path for main results JSON output
        output_dir                : Optional directory for auxiliary outputs
                                    (near_misses, hallucination_report)
        usdm_json_path            : Optional protocol USDM JSON (Scenario 1 M4 id-in-graph check)
        artifact_stem             : Optional basename (no extension) for aux files:
                                    ``{stem}_near_misses.csv``, ``{stem}_hallucination_report.json``
        write_report_yaml         : Scenario 1: emit ``{stem}.yaml`` (combined config + report)
        write_report_docx         : Scenario 1: emit ``{stem}.docx`` (reference layout)
        scenario1_only            : If True, skip run when study is not Scenario 1 (no files written)
        with_semantic_review      : If True (Scenario 1), call OpenAI to pair missed GT with extra
                                    generated lines; writes structured JSON (does not change M1 unless
                                    ``semantic_review_affects_m1`` is True).
        semantic_review_affects_m1: If True, merge semantic-review credits into M1 recall / lists.

    Returns:
        Results dict (Scenario 1 or 2 format), or ``{'skipped': True, ...}`` when scenario1_only skips
    """
    scenario = detect_scenario(study_id, ground_truth_csv_path)
    print(f"[INFO] {study_id} -> Scenario {scenario} "
          f"({'ground truth available' if scenario == 1 else 'no ground truth - proxy signals'})")

    if scenario1_only and scenario != 1:
        print(
            f"[INFO] Skipping {study_id}: not in verify ground truth (--scenario1-only).",
        )
        return {
            "skipped": True,
            "study_id": study_id,
            "scenario_would_be": scenario,
        }

    if scenario == 1:
        results = run_scenario1_eval(
            generator_json_path=generator_json_path,
            ground_truth_csv_path=ground_truth_csv_path,
            study_id=study_id,
            usdm_json_path=usdm_json_path,
        )
        # Annotate classified failures for generator developer
        results["classified_failures"] = classify_failures(results)

        if with_semantic_review:
            sr = run_semantic_review_for_results(
                results,
                usdm_json_path=usdm_json_path,
            )
            sr["applied_to_m1"] = False
            results["semantic_review"] = sr
            if semantic_review_affects_m1:
                apply_semantic_review_to_m1(results, sr)
                results["classified_failures"] = classify_failures(results)
        elif semantic_review_affects_m1:
            print(
                "[WARN] --semantic-review-affects-m1 ignored without --with-semantic-review",
                file=sys.stderr,
            )
    else:
        results = run_scenario2_eval(
            generator_json_path=generator_json_path,
            benchmark_csv_path=deviation_benchmarks_path,
            study_id=study_id,
            usdm_json_path=usdm_json_path,
        )
        # Expose a weighted "signal health" % so scenario 2 has a headline
        # number alongside the traffic-light verdict (matches the hero tile
        # expected by the UI).
        try:
            from reports.pipd_scenario2_report import compute_signal_health

            _health = compute_signal_health(results.get("signals") or {})
            results["signal_health"] = _health
            results["overall_score_percent"] = _health.get("percent")
        except Exception:
            pass

    # Always save main JSON
    save_results_json(results, output_path)

    # Auxiliary outputs
    aux_dir = output_dir or str(Path(output_path).parent)
    save_hallucination_report(results, aux_dir, artifact_stem=artifact_stem)
    if scenario == 1:
        save_near_misses_csv(results, aux_dir, artifact_stem=artifact_stem)
        if results.get("semantic_review"):
            _save_semantic_review_json(results, aux_dir, artifact_stem=artifact_stem)
        _emit_scenario1_yaml_word(
            results,
            generator_json_path,
            ground_truth_csv_path,
            study_id,
            aux_dir,
            usdm_json_path=usdm_json_path,
            artifact_stem=artifact_stem,
            write_yaml=write_report_yaml,
            write_word=write_report_docx,
        )
    else:
        _emit_scenario2_yaml_word(
            results,
            generator_json_path,
            deviation_benchmarks_path,
            study_id,
            aux_dir,
            usdm_json_path=usdm_json_path,
            artifact_stem=artifact_stem,
            write_yaml=write_report_yaml,
            write_word=write_report_docx,
        )

    print_summary(results)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PIPD Eval Framework – Unified Runner (auto-detects scenario)",
    )
    parser.add_argument("--generator_json",       required=True, help="Path to {study_id}_PIPD.json")
    parser.add_argument("--ground_truth",         required=True, help="Path to pipd_ground_truth_clean.csv")
    parser.add_argument("--deviation_benchmarks", required=True, help="Path to deviation_subcategories_clean.csv")
    parser.add_argument("--study_id",             required=True, help="Study identifier e.g. B7981027")
    parser.add_argument("--output",               required=True, help="Output JSON path")
    parser.add_argument("--output_dir",           default=None,  help="Optional: dir for auxiliary outputs")
    parser.add_argument(
        "--usdm_json",
        default=None,
        help="Optional protocol USDM JSON for Scenario 1 M4 (ids must exist in graph when load succeeds)",
    )
    parser.add_argument(
        "--artifact_stem",
        default=None,
        help="Optional basename for near_misses / hallucination_report files (no extension)",
    )
    parser.add_argument(
        "--no-yaml",
        action="store_true",
        help="Scenario 1: skip combined eval YAML (pip install pyyaml)",
    )
    parser.add_argument(
        "--no-word",
        action="store_true",
        help="Scenario 1: skip Word report (python-docx)",
    )
    parser.add_argument(
        "--scenario1-only",
        action="store_true",
        help="Skip entirely if study is not in verify ground truth (no Scenario 2)",
    )
    parser.add_argument(
        "--with-semantic-review",
        action="store_true",
        dest="with_semantic_review",
        help="Scenario 1: call OpenAI to pair missed GT with extra generated lines (USDM excerpt + JSON output). "
        "Requires OPENAI_API_KEY. Does not change M1 unless --semantic-review-affects-m1.",
    )
    parser.add_argument(
        "--semantic-review-affects-m1",
        action="store_true",
        dest="semantic_review_affects_m1",
        help="Scenario 1: apply semantic-review credits into M1 recall (requires --with-semantic-review).",
    )
    args = parser.parse_args()

    try:
        results = run_eval(
            generator_json_path       = args.generator_json,
            ground_truth_csv_path     = args.ground_truth,
            deviation_benchmarks_path = args.deviation_benchmarks,
            study_id                  = args.study_id,
            output_path               = args.output,
            output_dir                = args.output_dir,
            usdm_json_path            = args.usdm_json,
            artifact_stem             = args.artifact_stem,
            write_report_yaml         = not args.no_yaml,
            write_report_docx         = not args.no_word,
            scenario1_only            = args.scenario1_only,
            with_semantic_review      = args.with_semantic_review,
            semantic_review_affects_m1 = args.semantic_review_affects_m1,
        )
        if results.get("skipped"):
            sys.exit(0)
        sys.exit(0 if results.get("overall_pass") else 1)
    except Exception as exc:
        print(f"[ERROR] Eval failed: {exc}", file=sys.stderr)
        raise
