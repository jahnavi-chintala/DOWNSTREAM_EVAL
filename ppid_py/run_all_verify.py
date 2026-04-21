"""
run_all_verify.py
-----------------
PIPD Eval Framework – Batch Runner for all 8 verify studies.

Finds one generator JSON per verify study in --generator_output_dir,
runs Scenario 1 eval on each, and produces:
  • One per-study results JSON in --output_dir
  • One per-study near_misses CSV
  • One per-study hallucination_report JSON
  • eval_results_verify_set.csv  – one row per study × category
  • eval_summary.json            – aggregate scores, GO / NO-GO verdict

Must complete in < 2 minutes (24-hour test window constraint).

Usage:
    python3 run_all_verify.py \\
        --generator_output_dir ./generator_outputs/ \\
        --ground_truth pipd_ground_truth_clean.csv \\
        --deviation_benchmarks deviation_subcategories_clean.csv \\
        --output_dir ./eval_reports/
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import argparse
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd   # building eval_results_verify_set.csv

# ── Internal modules ──────────────────────────────────────────────────────────
from eval_scenario1 import (
    run_scenario1_eval,
    classify_failures,
    TARGETS,
)
from run_eval import save_near_misses_csv, save_hallucination_report


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VERIFY_STUDIES: List[str] = [
    "B7981027", "C4891023", "C1071003", "C1071005",
    "C3651021", "C4591081", "C3671059", "C5091017",
]


# ─────────────────────────────────────────────────────────────────────────────
# FILE DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def find_generator_json(study_id: str, generator_dir: str) -> Optional[str]:
    """
    Locate the generator output JSON file for a given study in the
    generator output directory.

    Looks for files matching {study_id}_PIPD.json (case-insensitive on
    the directory listing) and returns the first match.

    Args:
        study_id      : Study identifier e.g. 'B7981027'
        generator_dir : Directory containing generator JSON files

    Returns:
        Absolute path string if found, else None
    """
    gen_dir = Path(generator_dir)
    candidates = list(gen_dir.glob(f"{study_id}_PIPD.json")) + \
                 list(gen_dir.glob(f"{study_id}_pipd.json")) + \
                 list(gen_dir.glob(f"{study_id}.json"))
    return str(candidates[0]) if candidates else None


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE RESULT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def build_results_csv(all_results: List[Dict]) -> pd.DataFrame:
    """
    Build the eval_results_verify_set.csv DataFrame.

    One row per study × category (11 rows per study = 88 rows total for
    all 8 verify studies).

    Columns match the spec exactly:
      study_id, ta, phase, category_num,
      m1_recall, m1_matched, m1_gt_total, m1_generated_total, m1_near_misses,
      m2_flag_accuracy, m2_auto_confirmed_accuracy,
      m3_none_identified_correct, m4_hallucinations, overall_pass

    Args:
        all_results : List of results dicts from run_scenario1_eval()

    Returns:
        pandas DataFrame
    """
    rows = []
    for res in all_results:
        study_id = res["study_id"]
        ta       = res.get("ta", "Unknown")
        phase    = res.get("phase", "Unknown")
        overall  = res.get("overall_pass", False)

        for cat_num in range(1, 12):
            cat = res["per_category"].get(cat_num, {})
            m4_count = sum(
                1 for f in res["metrics"]["m4_hallucination_detection"]["flagged_subcategories"]
                if f.get("category_num") == cat_num
            )
            rows.append({
                "study_id":                study_id,
                "ta":                      ta,
                "phase":                   phase,
                "category_num":            cat_num,
                "m1_recall":               round(cat.get("m1_recall", 0.0), 4),
                "m1_matched":              cat.get("m1_matched", 0),
                "m1_gt_total":             cat.get("m1_gt_total", 0),
                "m1_generated_total":      cat.get("m1_generated_total", 0),
                "m1_near_misses":          cat.get("m1_near_misses", 0),
                "m2_flag_accuracy":        round(cat.get("m2_flag_accuracy", 0.0), 4),
                "m2_auto_confirmed_accuracy": round(cat.get("m2_auto_confirmed_accuracy", 0.0), 4),
                "m3_none_identified_correct": cat.get("m3_none_identified_correct", True),
                "m4_hallucinations":       m4_count,
                "overall_pass":            overall,
            })

    return pd.DataFrame(rows)


def build_summary_json(all_results: List[Dict]) -> Dict:
    """
    Build the eval_summary.json aggregate scores.

    Mirrors the exact schema from the design spec:
      {
        run_date, verify_set_count,
        metric_1_subcategory_recall: { score_b7981027, score_aggregate,
                                       target_b7981027, target_aggregate, pass },
        metric_2_flag_accuracy: { score_auto_confirmed, target, pass },
        metric_3_empty_category: { score, target, pass },
        metric_4_hallucinations: { total_flagged, target, pass },
        overall_pass, go_no_go
      }

    Args:
        all_results : List of results dicts from run_scenario1_eval()

    Returns:
        Summary dictionary ready for JSON serialisation
    """
    if not all_results:
        return {"error": "No results to aggregate"}

    # M1 ─────────────────────────────────────────────────────────────────────
    b7_result  = next((r for r in all_results if r["study_id"] == "B7981027"), None)
    m1_b7      = b7_result["metrics"]["m1_subcategory_recall"]["score"] if b7_result else 0.0
    m1_agg     = sum(r["metrics"]["m1_subcategory_recall"]["score"] for r in all_results) / len(all_results)
    m1_pass    = (m1_b7 >= TARGETS["m1_recall_b7981027"]) and (m1_agg >= TARGETS["m1_recall_aggregate"])

    # M2 ─────────────────────────────────────────────────────────────────────
    m2_auto_total   = sum(r["metrics"]["m2_flag_accuracy"]["auto_confirmed_total"] for r in all_results)
    m2_auto_correct = sum(r["metrics"]["m2_flag_accuracy"]["auto_confirmed_correct"] for r in all_results)
    m2_score  = m2_auto_correct / m2_auto_total if m2_auto_total else 1.0
    m2_pass   = m2_score >= TARGETS["m2_auto_confirmed_accuracy"]

    # M3 ─────────────────────────────────────────────────────────────────────
    m3_score  = sum(r["metrics"]["m3_empty_category_accuracy"]["score"] for r in all_results) / len(all_results)
    m3_pass   = m3_score >= TARGETS["m3_empty_category_accuracy"]

    # M4 ─────────────────────────────────────────────────────────────────────
    m4_total  = sum(r["metrics"]["m4_hallucination_detection"]["hallucinations_found"] for r in all_results)
    m4_pass   = m4_total == 0

    all_pass  = m1_pass and m2_pass and m3_pass and m4_pass

    return {
        "run_date":            datetime.now().date().isoformat(),
        "verify_set_count":    len(all_results),
        "studies_evaluated":   [r["study_id"] for r in all_results],
        "metric_1_subcategory_recall": {
            "score_b7981027":    round(m1_b7,  4),
            "score_aggregate":   round(m1_agg, 4),
            "target_b7981027":   TARGETS["m1_recall_b7981027"],
            "target_aggregate":  TARGETS["m1_recall_aggregate"],
            "pass":              m1_pass,
        },
        "metric_2_flag_accuracy": {
            "score_auto_confirmed": round(m2_score, 4),
            "target":               TARGETS["m2_auto_confirmed_accuracy"],
            "pass":                 m2_pass,
        },
        "metric_3_empty_category": {
            "score":  round(m3_score, 4),
            "target": TARGETS["m3_empty_category_accuracy"],
            "pass":   m3_pass,
        },
        "metric_4_hallucinations": {
            "total_flagged": m4_total,
            "target":        0,
            "pass":          m4_pass,
        },
        "overall_pass": all_pass,
        "go_no_go":     "GO" if all_pass else "NO-GO",
    }


def build_failure_digest(all_results: List[Dict]) -> Dict:
    """
    Aggregate and classify all failures across verify studies into a
    concise digest for the generator developer.

    Per the communication pattern in the spec:
      1. Overall scorecard (summary)
      2. Top 3 categories with lowest recall across verify set
      3. Classified failure list by type

    Args:
        all_results : List of results dicts from run_scenario1_eval()

    Returns:
        {
          top_3_low_recall_categories: [ { category_num, avg_recall } ],
          failure_counts_by_type: { type: count },
          all_failures: [ { failure_type, category_num, example, ... } ]
        }
    """
    # Compute average recall per category across all studies
    cat_recall: Dict[int, List[float]] = {n: [] for n in range(1, 12)}
    all_failures = []

    for res in all_results:
        for cat_num, cat in res["per_category"].items():
            cat_recall[cat_num].append(cat.get("m1_recall", 0.0))
        all_failures.extend(classify_failures(res))

    avg_recalls = [
        {"category_num": n, "avg_recall": round(sum(v) / len(v), 4) if v else 0.0}
        for n, v in cat_recall.items()
    ]
    avg_recalls.sort(key=lambda x: x["avg_recall"])
    top3_low = avg_recalls[:3]

    # Count failures by type
    from collections import Counter
    counts = Counter(f["failure_type"] for f in all_failures)

    return {
        "top_3_low_recall_categories": top3_low,
        "failure_counts_by_type":      dict(counts),
        "all_failures":                all_failures,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN BATCH RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_all_verify(
    generator_output_dir: str,
    ground_truth_csv_path: str,
    deviation_benchmarks_path: str,
    output_dir: str,
) -> Dict:
    """
    Run Scenario 1 evaluation across all 8 verify studies and produce
    all aggregate output files.

    Output files created in output_dir:
      • {study_id}_results.json         – per-study results (8 files)
      • near_misses_{study_id}.csv      – near-miss log (8 files)
      • hallucination_report_{id}.json  – M4 report (8 files)
      • eval_results_verify_set.csv     – 88-row results grid
      • eval_summary.json               – GO / NO-GO verdict
      • failure_digest.json             – for generator developer

    Args:
        generator_output_dir      : Dir containing {study_id}_PIPD.json files
        ground_truth_csv_path     : Path to pipd_ground_truth_clean.csv
        deviation_benchmarks_path : Path to deviation_subcategories_clean.csv
                                    (unused in S1 but kept for interface consistency)
        output_dir                : Dir to write all outputs

    Returns:
        eval_summary dict
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results: List[Dict] = []
    failed_studies: List[str] = []
    start_time = time.time()

    print(f"\n{'='*60}")
    print(f"  PIPD Batch Eval │ {len(VERIFY_STUDIES)} verify studies")
    print(f"{'='*60}")

    for study_id in VERIFY_STUDIES:
        print(f"\n  [{study_id}] ", end="", flush=True)

        gen_path = find_generator_json(study_id, generator_output_dir)
        if gen_path is None:
            print(f"SKIPPED – generator JSON not found in {generator_output_dir}")
            failed_studies.append(study_id)
            continue

        try:
            t0 = time.time()
            results = run_scenario1_eval(
                generator_json_path   = gen_path,
                ground_truth_csv_path = ground_truth_csv_path,
                study_id              = study_id,
            )
            elapsed = time.time() - t0

            verdict = "✓ GO" if results["overall_pass"] else "✗ NO-GO"
            m1 = results["metrics"]["m1_subcategory_recall"]["score"]
            print(f"{verdict} │ M1={m1:.1%} │ {elapsed:.1f}s")

            # Save per-study JSON
            study_out = out_dir / f"{study_id}_results.json"
            with open(study_out, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, default=str)

            # Auxiliary outputs
            save_near_misses_csv(results, str(out_dir))
            save_hallucination_report(results, str(out_dir))

            all_results.append(results)

        except Exception as exc:
            print(f"ERROR – {exc}")
            failed_studies.append(study_id)

    total_time = time.time() - start_time
    print(f"\n  Completed {len(all_results)}/{len(VERIFY_STUDIES)} studies in {total_time:.1f}s")

    if not all_results:
        print("[ERROR] No studies evaluated successfully.", file=sys.stderr)
        return {"error": "No successful evaluations"}

    # ── Aggregate outputs ────────────────────────────────────────────────────

    # eval_results_verify_set.csv
    results_df = build_results_csv(all_results)
    csv_path = out_dir / "eval_results_verify_set.csv"
    results_df.to_csv(csv_path, index=False)
    print(f"\n[INFO] Results CSV ({len(results_df)} rows) → {csv_path}")

    # eval_summary.json
    summary = build_summary_json(all_results)
    summary_path = out_dir / "eval_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[INFO] Summary JSON → {summary_path}")

    # failure_digest.json
    digest = build_failure_digest(all_results)
    digest_path = out_dir / "failure_digest.json"
    with open(digest_path, "w", encoding="utf-8") as fh:
        json.dump(digest, fh, indent=2, default=str)
    print(f"[INFO] Failure digest → {digest_path}")

    # ── Final verdict ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  AGGREGATE SCORECARD")
    print(f"{'─'*60}")
    m = summary
    print(f"  M1 Recall (B7981027) : {m['metric_1_subcategory_recall']['score_b7981027']:.2%}"
          f"  ({'PASS' if m['metric_1_subcategory_recall']['pass'] else 'FAIL'})")
    print(f"  M1 Recall (aggregate): {m['metric_1_subcategory_recall']['score_aggregate']:.2%}")
    print(f"  M2 Flag Accuracy     : {m['metric_2_flag_accuracy']['score_auto_confirmed']:.2%}"
          f"  ({'PASS' if m['metric_2_flag_accuracy']['pass'] else 'FAIL'})")
    print(f"  M3 Empty Category    : {m['metric_3_empty_category']['score']:.2%}"
          f"  ({'PASS' if m['metric_3_empty_category']['pass'] else 'FAIL'})")
    print(f"  M4 Hallucinations    : {m['metric_4_hallucinations']['total_flagged']}"
          f"  ({'PASS' if m['metric_4_hallucinations']['pass'] else 'FAIL'})")
    print(f"{'─'*60}")
    print(f"  Overall: {summary['go_no_go']}")
    if failed_studies:
        print(f"  Skipped studies: {failed_studies}")
    print(f"{'='*60}\n")

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PIPD Eval Framework – Batch runner for all 8 verify studies",
    )
    parser.add_argument("--generator_output_dir",  required=True, help="Dir with {study_id}_PIPD.json files")
    parser.add_argument("--ground_truth",          required=True, help="Path to pipd_ground_truth_clean.csv")
    parser.add_argument("--deviation_benchmarks",  required=True, help="Path to deviation_subcategories_clean.csv")
    parser.add_argument("--output_dir",            required=True, help="Output directory for all reports")
    args = parser.parse_args()

    summary = run_all_verify(
        generator_output_dir      = args.generator_output_dir,
        ground_truth_csv_path     = args.ground_truth,
        deviation_benchmarks_path = args.deviation_benchmarks,
        output_dir                = args.output_dir,
    )

    sys.exit(0 if summary.get("go_no_go") == "GO" else 1)
