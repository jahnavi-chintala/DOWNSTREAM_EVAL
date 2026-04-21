"""
run_all_verify.py
-----------------
D1 Risk Profile Eval Framework – Batch Runner (all 8 verify studies).

Iterates all 8 verify studies, runs Scenario 1 evaluation on each (ground truth
is available for all verify studies), and produces aggregated outputs:

  eval_results_verify_set.csv  – one row per study, all 4 metric scores
  eval_summary.json            – aggregate scores + overall GO/NO-GO verdict
  near_misses.csv              – all near misses across all verify studies
  hallucination_report_{study_id}.json – per-study hallucination report

Per-study eval reports are also written: {study_id}_eval_results.json.

Usage:
    python3 run_all_verify.py \\
        --generator_output_dir ./generator_outputs/ \\
        --ground_truth_risks risk_profile_ground_truth.csv \\
        --ground_truth_factors critical_factors_ground_truth.csv \\
        --output_dir ./eval_reports/

Produces: one eval result per study + aggregate_scorecard files.
Runtime: < 60 seconds for all 8 studies (pure Python, no API calls).
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import csv
import argparse
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd

# ── Internal ──────────────────────────────────────────────────────────────────
from eval_scenario1 import (
    run_scenario1_eval, classify_failures,
    TARGETS, VERIFY_STUDIES,
)
from run_eval import save_results_json, save_near_misses_csv, save_hallucination_report


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Studies NOT in risk_profile_ground_truth.csv (verify set check)
# These will be skipped for M1/M2 but may be scored for M3 if in CF ground truth
RISK_GT_MISSING: List[str] = ["C4891023", "C1071005", "C3671059"]


# ─────────────────────────────────────────────────────────────────────────────
# FILE DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

def _json_looks_like_risk_profile(path: Path) -> bool:
    """True if root object has typical Risk Profile generator keys (not raw USDM-only payloads)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    keys = {str(k).lower() for k in data.keys()}
    markers = (
        "risks",
        "critical_factors",
        "study_overview",
        "vendor_risks",
        "study_site_risks",
        "risks_monitored",
        "other_domain_risks",
    )
    return bool(keys & set(markers))


def _study_ids_declared_in_risk_profile(path: Path) -> Set[str]:
    """Study / protocol identifiers embedded in JSON (filename may omit study id)."""
    found: Set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return found
    if not isinstance(data, dict):
        return found

    def add_val(v: object) -> None:
        if v is None:
            return
        s = str(v).strip()
        if s:
            found.add(s.lower())

    for key in ("study_id", "protocol_id", "protocol_number"):
        add_val(data.get(key))
    so = data.get("study_overview")
    if isinstance(so, dict):
        for key in ("study_id", "protocol_number", "study_identifier", "nct_id"):
            add_val(so.get(key))
    md = data.get("metadata")
    if isinstance(md, dict):
        add_val(md.get("study_id"))

    # Embedded in titles / compound lines (filename may be generic)
    chunks: List[str] = []
    if isinstance(so, dict):
        chunks.append(json.dumps(so, ensure_ascii=False, default=str)[:12000])
    if isinstance(md, dict):
        chunks.append(json.dumps(md, ensure_ascii=False, default=str)[:8000])
    blob = " ".join(chunks).lower()
    for m in re.finditer(r"\b([a-z]\d{6,})\b", blob):
        found.add(m.group(1))

    return found


def _risk_profile_path_rank(path: Path) -> tuple:
    """
    Higher = better candidate. Prefer RiskProfile filenames and schema-shaped JSON;
    deprioritize USDM-only style names when study id matches several files.
    """
    name = path.name.lower()
    score = 0
    if any(h in name for h in ("riskprofile", "risk_profile", "risk-profile")):
        score += 40
    if _json_looks_like_risk_profile(path):
        score += 30
    # Filename looks USDM export but not risk profile
    if "usdm" in name and "risk" not in name and "riskprofile" not in name:
        score -= 25
    return (-score, str(path))


PROTOCOL_ID_IN_STEM = re.compile(r"\b([A-Za-z]\d{6,})\b")


def _normalize_protocol_id(token: str) -> Optional[str]:
    t = str(token).strip()
    if not t:
        return None
    m = re.fullmatch(r"([A-Za-z])(\d{6,})", t, re.I)
    if not m:
        return None
    return m.group(1).upper() + m.group(2)


def _is_auxiliary_or_non_risk_profile_path(path: Path) -> bool:
    """Skip eval artifacts, PIPD/CMP exports, and USDM-only files that are not Risk Profile JSON."""
    low = path.name.lower()
    if "eval" in {p.lower() for p in path.parts}:
        return True
    for needle in ("_comparison.json", "_extracted.json", "pipd_comparison", "cmp_comparison"):
        if needle in low:
            return True
    if low.endswith("_pipd.json") or low.endswith("_cmp.json"):
        return True
    if "usdm" in low and not any(
        h in low for h in ("riskprofile", "risk_profile", "risk-profile")
    ):
        if not _json_looks_like_risk_profile(path):
            return True
    return False


def _iter_risk_profile_json_candidates(generator_dir: Path) -> List[Path]:
    out: List[Path] = []
    for f in generator_dir.rglob("*.json"):
        if _is_auxiliary_or_non_risk_profile_path(f):
            continue
        low = f.name.lower()
        name_hint = any(
            h in low for h in ("riskprofile", "risk_profile", "risk-profile")
        )
        if name_hint or _json_looks_like_risk_profile(f):
            out.append(f)
    return out


def _resolve_study_id_for_risk_profile_file(f: Path) -> Optional[str]:
    candidates: Set[str] = set()
    for m in PROTOCOL_ID_IN_STEM.finditer(f.stem):
        nid = _normalize_protocol_id(m.group(0))
        if nid:
            candidates.add(nid)
    for raw in _study_ids_declared_in_risk_profile(f):
        nid = _normalize_protocol_id(raw)
        if nid:
            candidates.add(nid)
    if not candidates:
        return None
    parent_id = _normalize_protocol_id(f.parent.name)
    if parent_id and parent_id in candidates:
        return parent_id
    return sorted(candidates)[0]


def discover_risk_profile_generators(generator_dir: str) -> List[Tuple[str, str]]:
    """
    Scan ``generator_dir`` recursively for Risk Profile generator JSON files.

    Returns:
        Sorted list of ``(study_id, json_path_str)`` with one best file per study
        (same ranking as ``find_generator_json``).
    """
    root = Path(generator_dir)
    if not root.is_dir():
        return []
    best: Dict[str, Path] = {}
    for f in _iter_risk_profile_json_candidates(root):
        sid = _resolve_study_id_for_risk_profile_file(f)
        if not sid:
            continue
        prev = best.get(sid)
        if prev is None or _risk_profile_path_rank(f) < _risk_profile_path_rank(prev):
            best[sid] = f
    return sorted((sid, str(path)) for sid, path in best.items())


def find_generator_json(study_id: str, generator_dir: str) -> Optional[str]:
    """
    Find a **Risk Profile** generator JSON for ``study_id`` under ``generator_dir``.

    Searches **recursively** (nested folders, mixed USDM + Risk Profile drops).
    When several ``*.json`` files match the study id, picks the best Risk Profile
    candidate via filename hints and a light JSON shape check.
    """
    dir_path = Path(generator_dir)
    if not dir_path.exists():
        print(f"  [WARN] Generator output directory not found: {generator_dir}")
        return None

    study_lower = study_id.lower()
    candidates: List[Path] = []
    for f in dir_path.rglob("*.json"):
        if study_lower in f.name.lower():
            candidates.append(f)

    if not candidates:
        for f in dir_path.rglob("*.json"):
            if not _json_looks_like_risk_profile(f):
                continue
            if study_lower in _study_ids_declared_in_risk_profile(f):
                candidates.append(f)

    if not candidates:
        print(f"  [WARN] No Risk Profile JSON found for study '{study_id}' in {generator_dir}")
        return None

    best = sorted(candidates, key=_risk_profile_path_rank)[0]
    return str(best)


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def build_results_csv(all_results: List[Dict], output_dir: str) -> str:
    """
    Build eval_results_verify_set.csv from all per-study Scenario 1 result dicts.

    One row per study. Columns match the design spec §9.1 output specification.
    Includes all 4 metric scores and an overall_pass column for easy filtering.

    Args:
        all_results: List of result dicts, one per study
        output_dir: Directory to write into

    Returns:
        Full path of the written CSV.
    """
    out_path = Path(output_dir) / "eval_results_verify_set.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "study_id", "ta", "phase",
        "m1_risk_name_score", "m1_matched_risks", "m1_ground_truth_risks",
        "m2_rpn_tier_score", "m2_matched_rpn", "m2_total_matched_risks",
        "m3_critical_factor_score", "m3_matched_factors", "m3_ground_truth_factors",
        "m3_skipped",
        "m4_hallucinations", "m4_pass",
        "near_miss_count",
        "overall_pass", "verdict",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in all_results:
            if r.get("skipped"):
                writer.writerow({
                    "study_id": r["study_id"],
                    "ta": r.get("ta", ""),
                    "phase": r.get("phase", ""),
                    "m1_risk_name_score": "SKIPPED",
                    "m1_matched_risks": "",
                    "m1_ground_truth_risks": "",
                    "m2_rpn_tier_score": "SKIPPED",
                    "m2_matched_rpn": "",
                    "m2_total_matched_risks": "",
                    "m3_critical_factor_score": "SKIPPED",
                    "m3_matched_factors": "",
                    "m3_ground_truth_factors": "",
                    "m3_skipped": True,
                    "m4_hallucinations": "SKIPPED",
                    "m4_pass": "",
                    "near_miss_count": "",
                    "overall_pass": "SKIPPED",
                    "verdict": "SKIPPED",
                })
                continue

            metrics = r.get("metrics", {})
            m1 = metrics.get("m1_risk_name_recall", {})
            m2 = metrics.get("m2_rpn_tier_accuracy", {})
            m3 = metrics.get("m3_critical_factor_match", {})
            m4 = metrics.get("m4_hallucination_detection", {})

            writer.writerow({
                "study_id": r["study_id"],
                "ta": r.get("ta", ""),
                "phase": r.get("phase", ""),
                "m1_risk_name_score": m1.get("score", ""),
                "m1_matched_risks": m1.get("matched", ""),
                "m1_ground_truth_risks": m1.get("ground_truth_total", ""),
                "m2_rpn_tier_score": m2.get("score", ""),
                "m2_matched_rpn": m2.get("matched_rpn", ""),
                "m2_total_matched_risks": m2.get("total_matched_risks", ""),
                "m3_critical_factor_score": m3.get("score", ""),
                "m3_matched_factors": m3.get("matched_factors", ""),
                "m3_ground_truth_factors": m3.get("ground_truth_total", ""),
                "m3_skipped": m3.get("skipped", False),
                "m4_hallucinations": m4.get("hallucinations_found", ""),
                "m4_pass": m4.get("passed", ""),
                "near_miss_count": len(r.get("near_misses", [])),
                "overall_pass": r["verdict"] == "GO",
                "verdict": r["verdict"],
            })

    print(f"  Written: {out_path}")
    return str(out_path)


def build_summary_json(all_results: List[Dict], output_dir: str) -> str:
    """
    Build eval_summary.json aggregating scores across all verify studies.

    Matches design spec §9.2 output format. Computes aggregate metric scores
    (averages across all studies with available ground truth) and overall GO/NO-GO.

    Overall GO requires ALL studies to pass ALL metrics simultaneously. A single
    NO-GO study = overall NO-GO.

    Args:
        all_results: List of result dicts from batch run (includes skipped studies)
        output_dir: Directory to write into

    Returns:
        Full path of the written JSON.
    """
    # Filter to studies that were actually evaluated (not skipped)
    evaluated = [r for r in all_results if not r.get("skipped")]

    def _avg(values):
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    m1_scores = [r["metrics"]["m1_risk_name_recall"]["score"] for r in evaluated
                 if r.get("metrics", {}).get("m1_risk_name_recall", {}).get("score") is not None]
    m2_scores = [r["metrics"]["m2_rpn_tier_accuracy"]["score"] for r in evaluated
                 if r.get("metrics", {}).get("m2_rpn_tier_accuracy", {}).get("score") is not None]
    m3_scores = [r["metrics"]["m3_critical_factor_match"]["score"] for r in evaluated
                 if r.get("metrics", {}).get("m3_critical_factor_match", {}).get("score") is not None]
    m4_totals = [r["metrics"]["m4_hallucination_detection"]["hallucinations_found"]
                 for r in evaluated
                 if r.get("metrics", {}).get("m4_hallucination_detection") is not None]

    m1_avg = _avg(m1_scores)
    m2_avg = _avg(m2_scores)
    m3_avg = _avg(m3_scores)
    m4_total = sum(m4_totals) if m4_totals else 0

    m1_pass = m1_avg is not None and m1_avg >= TARGETS["m1_risk_name_recall"]
    m2_pass = m2_avg is not None and m2_avg >= TARGETS["m2_rpn_tier_accuracy"]
    m3_pass = m3_avg is not None and m3_avg >= TARGETS["m3_critical_factor_match"]
    m4_pass = m4_total == 0

    # Overall: all evaluated studies must have GO verdict
    go_count = sum(1 for r in evaluated if r.get("verdict") == "GO")
    no_go_count = len(evaluated) - go_count
    overall_pass = no_go_count == 0 and len(evaluated) > 0

    per_study = [
        {
            "study_id": r["study_id"],
            "ta": r.get("ta", ""),
            "phase": r.get("phase", ""),
            "verdict": r.get("verdict", "SKIPPED"),
            "m1_score": r.get("metrics", {}).get("m1_risk_name_recall", {}).get("score"),
            "m2_score": r.get("metrics", {}).get("m2_rpn_tier_accuracy", {}).get("score"),
            "m3_score": r.get("metrics", {}).get("m3_critical_factor_match", {}).get("score"),
            "m4_hallucinations": r.get("metrics", {}).get("m4_hallucination_detection", {}).get("hallucinations_found"),
        }
        for r in all_results
    ]

    summary = {
        "run_date": datetime.utcnow().date().isoformat(),
        "run_timestamp": datetime.utcnow().isoformat() + "Z",
        "verify_set_count": len(VERIFY_STUDIES),
        "evaluated_count": len(evaluated),
        "skipped_count": len(all_results) - len(evaluated),
        "metric_1_risk_name": {
            "score": m1_avg,
            "target": TARGETS["m1_risk_name_recall"],
            "pass": m1_pass,
            "studies_scored": len(m1_scores),
        },
        "metric_2_rpn_tier": {
            "score": m2_avg,
            "target": TARGETS["m2_rpn_tier_accuracy"],
            "pass": m2_pass,
            "studies_scored": len(m2_scores),
        },
        "metric_3_critical_factors": {
            "score": m3_avg,
            "target": TARGETS["m3_critical_factor_match"],
            "pass": m3_pass,
            "studies_scored": len(m3_scores),
        },
        "metric_4_hallucinations": {
            "total_flagged": m4_total,
            "target": 0,
            "pass": m4_pass,
            "studies_scored": len(m4_totals),
        },
        "overall_pass": overall_pass,
        "go_no_go": "GO" if overall_pass else "NO-GO",
        "go_count": go_count,
        "no_go_count": no_go_count,
        "per_study": per_study,
    }

    out_path = Path(output_dir) / "eval_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"  Written: {out_path}")
    return str(out_path)


def build_combined_near_misses_csv(all_results: List[Dict], output_dir: str) -> str:
    """
    Build a combined near_misses.csv across all verify studies.

    One row per near miss. Useful for the generator developer to see patterns
    across all studies — e.g. systematic wording differences in the prompt or
    YAML risk name library.

    Args:
        all_results: List of result dicts from batch run
        output_dir: Directory to write into

    Returns:
        Full path of the written CSV.
    """
    out_path = Path(output_dir) / "near_misses.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["study_id", "truth_name", "generated_name", "edit_distance"]
        )
        writer.writeheader()
        for r in all_results:
            if r.get("skipped"):
                continue
            for nm in r.get("near_misses", []):
                writer.writerow({
                    "study_id": r["study_id"],
                    "truth_name": nm["truth_name"],
                    "generated_name": nm["generated_name"],
                    "edit_distance": nm["edit_distance"],
                })

    print(f"  Written: {out_path}")
    return str(out_path)


def build_failure_digest(all_results: List[Dict], output_dir: str) -> str:
    """
    Build failure_digest.json: a triage-ready summary of all failures across the batch.

    Reports:
      - Failure counts by metric type (M1/M2/M3/M4)
      - Which studies failed each metric
      - Top 3 most missed risk names (GT risks the generator most commonly misses)
      - All hallucinated field paths for generator developer fix

    Args:
        all_results: List of result dicts from batch run
        output_dir: Directory to write into

    Returns:
        Full path of the written JSON.
    """
    evaluated = [r for r in all_results if not r.get("skipped")]

    # Count failures by metric
    failure_counts: Dict[str, int] = {"M1": 0, "M2": 0, "M3": 0, "M4": 0}
    failed_studies: Dict[str, List[str]] = {"M1": [], "M2": [], "M3": [], "M4": []}
    all_missed_risks: List[str] = []
    all_hallucinations: List[Dict] = []

    for r in evaluated:
        metrics = r.get("metrics", {})
        study_id = r["study_id"]

        m1 = metrics.get("m1_risk_name_recall", {})
        if not m1.get("passed"):
            failure_counts["M1"] += 1
            failed_studies["M1"].append(study_id)
        all_missed_risks.extend(m1.get("missed_names", []))

        m2 = metrics.get("m2_rpn_tier_accuracy", {})
        if not m2.get("passed"):
            failure_counts["M2"] += 1
            failed_studies["M2"].append(study_id)

        m3 = metrics.get("m3_critical_factor_match", {})
        if not m3.get("skipped") and not m3.get("passed"):
            failure_counts["M3"] += 1
            failed_studies["M3"].append(study_id)

        m4 = metrics.get("m4_hallucination_detection", {})
        if not m4.get("passed"):
            failure_counts["M4"] += 1
            failed_studies["M4"].append(study_id)
            for field in m4.get("flagged_fields", []):
                all_hallucinations.append({"study_id": study_id, **field})

    # Top 3 most commonly missed risk names
    from collections import Counter
    missed_counter = Counter(all_missed_risks)
    top_missed = [
        {"risk_name": name, "miss_count": count}
        for name, count in missed_counter.most_common(3)
    ]

    digest = {
        "run_date": datetime.utcnow().date().isoformat(),
        "evaluated_studies": len(evaluated),
        "failure_counts_by_metric": failure_counts,
        "failed_studies_by_metric": failed_studies,
        "top_3_missed_risks": top_missed,
        "all_missed_risk_names": all_missed_risks,
        "total_hallucinations": len(all_hallucinations),
        "hallucination_details": all_hallucinations,
    }

    out_path = Path(output_dir) / "failure_digest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(digest, f, indent=2, ensure_ascii=False)

    print(f"  Written: {out_path}")
    return str(out_path)


# ─────────────────────────────────────────────────────────────────────────────
# BATCH RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_all_verify(
    generator_output_dir: str,
    ground_truth_risks_csv: str,
    ground_truth_factors_csv: str,
    output_dir: str,
) -> List[Dict]:
    """
    Run Scenario 1 evaluation across all 8 verify studies and produce aggregated outputs.

    Iterates VERIFY_STUDIES in order. For each study:
      1. Finds the generator JSON in generator_output_dir
      2. Runs run_scenario1_eval() — or marks as skipped if JSON or GT missing
      3. Saves per-study output files (results JSON, near-misses, hallucination report)

    After all studies: builds eval_results_verify_set.csv, eval_summary.json,
    combined near_misses.csv, and failure_digest.json.

    Designed to complete in < 60 seconds (target from design spec).
    Skips gracefully if a generator JSON is missing rather than crashing.

    Args:
        generator_output_dir: Directory containing {study_id}_RiskProfile.json files
        ground_truth_risks_csv: Path to risk_profile_ground_truth.csv
        ground_truth_factors_csv: Path to critical_factors_ground_truth.csv
        output_dir: Directory for all output files

    Returns:
        List of result dicts (one per verify study, including skipped entries).
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    start_time = datetime.utcnow()

    print(f"\n{'='*62}")
    print(f"  D1 Risk Profile Eval – Batch Verify Set")
    print(f"  Studies: {len(VERIFY_STUDIES)} | Output: {output_dir}")
    print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*62}\n")

    all_results: List[Dict] = []

    for study_id in VERIFY_STUDIES:
        print(f"  [{VERIFY_STUDIES.index(study_id)+1}/{len(VERIFY_STUDIES)}] {study_id}")

        # Find generator JSON
        json_path = find_generator_json(study_id, generator_output_dir)
        if json_path is None:
            print(f"         SKIPPED (no generator JSON)")
            all_results.append({
                "study_id": study_id,
                "ta": "UNKNOWN",
                "phase": "UNKNOWN",
                "scenario": 1,
                "skipped": True,
                "skip_reason": "Generator JSON not found in output directory",
            })
            continue

        try:
            result = run_scenario1_eval(
                generator_json_path=json_path,
                ground_truth_risks_csv=ground_truth_risks_csv,
                ground_truth_factors_csv=ground_truth_factors_csv,
                study_id=study_id,
                allow_empty_risk_gt=True,
            )

            # Save per-study files
            save_results_json(result, output_dir, study_id)
            save_near_misses_csv(result, output_dir, study_id)
            save_hallucination_report(result, output_dir, study_id)

            metrics = result.get("metrics", {})
            m1 = metrics.get("m1_risk_name_recall", {})
            m2 = metrics.get("m2_rpn_tier_accuracy", {})
            m3 = metrics.get("m3_critical_factor_match", {})
            m2_str = "SKIP" if m2.get("skipped") else f"{m2.get('score', 0):.0%}"
            m3_str = "SKIP" if m3.get("skipped") else f"{m3.get('score', 0):.0%}"
            print(
                f"         {result['verdict']}  "
                f"M1={m1.get('score', 0):.0%}  "
                f"M2={m2_str}  "
                f"M3={m3_str}  "
                f"M4={'PASS' if metrics.get('m4_hallucination_detection', {}).get('passed') else 'FAIL'}"
            )

            all_results.append(result)

        except Exception as e:
            print(f"         ERROR: {e}")
            all_results.append({
                "study_id": study_id,
                "ta": "UNKNOWN",
                "phase": "UNKNOWN",
                "scenario": 1,
                "skipped": True,
                "skip_reason": f"Eval error: {e}",
            })

    # Build aggregate outputs
    print(f"\n  Building aggregate outputs...")
    build_results_csv(all_results, output_dir)
    build_summary_json(all_results, output_dir)
    build_combined_near_misses_csv(all_results, output_dir)
    build_failure_digest(all_results, output_dir)

    elapsed = (datetime.utcnow() - start_time).total_seconds()
    evaluated_count = sum(1 for r in all_results if not r.get("skipped"))
    go_count = sum(1 for r in all_results if r.get("verdict") == "GO")
    print(f"\n  Completed in {elapsed:.1f}s | "
          f"Evaluated: {evaluated_count}/{len(VERIFY_STUDIES)} | "
          f"GO: {go_count}/{evaluated_count}")
    print(f"{'='*62}\n")

    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="D1 Risk Profile – Batch Eval Runner (all 8 verify studies)"
    )
    parser.add_argument("--generator_output_dir", required=True,
                        help="Directory containing {study_id}_RiskProfile.json files")
    parser.add_argument("--ground_truth_risks", required=True,
                        help="Path to risk_profile_ground_truth.csv")
    parser.add_argument("--ground_truth_factors", required=True,
                        help="Path to critical_factors_ground_truth.csv")
    parser.add_argument("--output_dir", default="eval_reports/",
                        help="Output directory (default: eval_reports/)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_all_verify(
        generator_output_dir=args.generator_output_dir,
        ground_truth_risks_csv=args.ground_truth_risks,
        ground_truth_factors_csv=args.ground_truth_factors,
        output_dir=args.output_dir,
    )
