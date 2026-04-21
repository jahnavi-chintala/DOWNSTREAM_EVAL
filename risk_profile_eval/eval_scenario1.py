"""
eval_scenario1.py
-----------------
D1 Risk Profile Eval Framework – Scenario 1: Ground truth AVAILABLE (verify studies).

Computes 4 metrics per the D1 Risk Profile Eval Framework Design:

  M1 – Risk Name Selection Recall   (CRITICAL  – target >= 85%; exact risk_name; all GT rows for study)
  M2 – RPN Tier Accuracy            (CRITICAL  – target >= 90%; ±1 tier on M1-matched risks)
  M3 – Critical Factor Match        (HIGH      – target >= 80%; recall on factor names per TDD §7)
  M4 – Hallucination Detection      (CRITICAL  – target = 0 hallucinated fields)

Ground truth sources:
  • risk_profile_ground_truth.csv   (risk names, RPNs, components)
  • critical_factors_ground_truth.csv (critical factor names per study)

Usage (standalone):
    python3 eval_scenario1.py \\
        --generator_json path/to/<study>_RiskProfile.json \\
        --ground_truth_risks risk_profile_ground_truth.csv \\
        --ground_truth_factors critical_factors_ground_truth.csv \\
        --study_id <study_id> \\
        --output_json eval_s1_results.json
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd
from Levenshtein import distance as lev

from risk_generator_risks import (
    get_all_risk_dicts,
    infer_generated_domain,
    iter_risk_dicts_with_keys,
    normalize_domain_label,
    normalize_risk_name_for_match,
)
from risk_usdm_tracing import (
    UsdmIndex,
    build_usdm_index,
    collect_factor_trace_issues,
    collect_risk_trace_issues,
    load_usdm,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VERIFY_STUDIES: List[str] = [
    "B7981027", "C4891023", "C1071003", "C1071005",
    "C3651021", "C4591081", "C3671059", "C5091017",
]

NEAR_MISS_THRESHOLD: int = 5        # Levenshtein distance <= 5 → near miss (NOT a match)

# C1071003 has blank Critical Data / Process – score factor names only (no content violations)
FACTOR_NAME_ONLY_STUDIES: List[str] = ["C1071003"]

# RPN → Tier mapping (5 discrete tiers)
RPN_TIER_MAP: Dict[int, int] = {112: 1, 160: 2, 196: 3, 280: 4, 400: 5}
RPN_TIERS_SORTED: List[int] = sorted(RPN_TIER_MAP.keys())

# Metric pass/fail targets
TARGETS: Dict[str, float] = {
    "m1_risk_name_recall":      0.85,
    "m2_rpn_tier_accuracy":     0.90,
    "m3_critical_factor_match": 0.80,
    "m4_hallucinations":        0.00,   # zero tolerance
}

# Fields that must have provenance – used by M4 hallucination detection
# Each entry: (path_description, how_to_check)
# Checked in compute_hallucination_detection()
PROVENANCE_RULES = [
    "risk.intelligence.benchmark_source",
    "risk.usdm_drivers",
    "risk.associated_causes[*].usdm_trigger",
    "critical_factor.usdm_sources.critical_data",
]


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_risk_ground_truth(csv_path: str, study_id: str, *, allow_empty: bool = False) -> pd.DataFrame:
    """
    Load risk ground truth rows for a single study from risk_profile_ground_truth.csv.

    Filters to the specified study_id. Returns all rows for that study (no RPN cap).

    Args:
        csv_path: Path to risk_profile_ground_truth.csv
        study_id: Study / protocol identifier

    Returns:
        DataFrame with columns: study_id, ta, phase, risk_name, rpn, impact,
        likelihood, detectability, etc.

    Raises:
        FileNotFoundError: if the CSV does not exist
        ValueError: if study_id has zero rows in the CSV (unless allow_empty=True)
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Risk ground truth CSV not found: {csv_path}")

    df = pd.read_csv(path)
    sid = str(study_id).strip()
    study_df = df[df["study_id"].astype(str).str.strip() == sid].copy()

    if study_df.empty and not allow_empty:
        raise ValueError(
            f"Study '{study_id}' has no rows in {csv_path}. "
            "Ensure study_id is correct and study is in the verify set."
        )
    if study_df.empty and allow_empty:
        return df.head(0).copy()

    return study_df.reset_index(drop=True)


def load_factor_ground_truth(csv_path: str, study_id: str) -> pd.DataFrame:
    """
    Load critical factor ground truth rows for a single study from
    critical_factors_ground_truth.csv.

    Args:
        csv_path: Path to critical_factors_ground_truth.csv
        study_id: Study identifier

    Returns:
        DataFrame with columns: study_id, critical_factor_name, critical_data,
        critical_process. Empty DataFrame if study not present.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Critical factors ground truth CSV not found: {csv_path}")

    df = pd.read_csv(path)
    sid = str(study_id).strip()
    return df[df["study_id"].astype(str).str.strip() == sid].copy().reset_index(drop=True)


def load_generator_json(json_path: str) -> Dict[str, Any]:
    """
    Load and validate the D1 generator output JSON for a single study.

    Verifies that the required top-level keys are present (risks, critical_factors,
    metadata). Does not deep-validate every field – metric functions handle missing
    fields gracefully.

    Args:
        json_path: Path to {study_id}_RiskProfile.json

    Returns:
        Parsed JSON as Dict.

    Raises:
        FileNotFoundError: if JSON file does not exist
        json.JSONDecodeError: if JSON is malformed
        KeyError: if required top-level keys are absent
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"Generator JSON not found: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for required_key in ["risks", "critical_factors", "metadata"]:
        if required_key not in data:
            raise KeyError(
                f"Generator JSON missing required key '{required_key}'. "
                f"File: {json_path}"
            )

    return data


# ─────────────────────────────────────────────────────────────────────────────
# RPN TIER UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def rpn_to_tier(rpn: int) -> Optional[int]:
    """
    Map an RPN value to its tier number (1–5).

    Returns None if the RPN value is not one of the 5 canonical values.
    Used for **tier_distance** in M2; M2 pass/fail is **tier distance ≤ 1** (TDD §6).

    Args:
        rpn: RPN integer value

    Returns:
        Tier integer 1–5 or None if unrecognised.
    """
    return RPN_TIER_MAP.get(int(rpn), None)


def tier_distance(rpn_generated: int, rpn_truth: int) -> Optional[int]:
    """
    Compute the tier distance between two RPN values.

    Tier distance = |tier(generated) - tier(truth)|.
    Returns None if either RPN is not a recognised canonical value.
    M2 treats **tier_distance ≤ 1** as pass (TDD §6); ``None`` → fail that pair.

    Args:
        rpn_generated: RPN value from generator output
        rpn_truth: RPN value from ground truth CSV

    Returns:
        Non-negative integer tier distance, or None if tier lookup fails.
    """
    t_gen = rpn_to_tier(rpn_generated)
    t_truth = rpn_to_tier(rpn_truth)
    if t_gen is None or t_truth is None:
        return None
    return abs(t_gen - t_truth)


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 1 – RISK NAME SELECTION RECALL
# ─────────────────────────────────────────────────────────────────────────────

def find_near_misses(
    unmatched_gen: List[Dict[str, Any]],
    unmatched_gt: List[Dict[str, Any]],
) -> List[Dict]:
    """
    Near-miss pairs among **unmatched** risks (TDD §5.2). Levenshtein distance on
    stripped risk_name strings; not counted as matches for scoring.
    """
    near_misses = []
    for gt in unmatched_gt:
        tn = str(gt["risk_name"]).strip()
        best_dist = NEAR_MISS_THRESHOLD + 1
        best_gen = None
        for gr in unmatched_gen:
            gn = str(gr["risk_name"]).strip()
            d = lev(gn, tn)
            if d <= NEAR_MISS_THRESHOLD and d < best_dist:
                best_dist = d
                best_gen = gr["risk_name"]
        if best_gen is not None:
            near_misses.append({
                "truth_name": gt["risk_name"],
                "generated_name": best_gen,
                "edit_distance": best_dist,
            })
    return sorted(near_misses, key=lambda x: x["edit_distance"])


def compute_risk_name_recall(
    generator_json: Dict,
    truth_df: pd.DataFrame,
    study_id: str,
) -> Dict:
    """
    M1 – Risk name recall. **Exact** match on stripped ``risk_name`` strings; one-to-one
    greedy matching. **No** domain filter. Denominator = **every** non-blank GT risk row
    for the study (dynamic per protocol — no fixed cap).

    If there are **no** risk rows in ground truth, recall is 1.0 only when the generator
    emits no risks.
    """
    has_domain = "risk_domain" in truth_df.columns

    gt_rows: List[Dict[str, Any]] = []
    for _, row in truth_df.iterrows():
        rn = str(row.get("risk_name", "") or "").strip()
        if not rn:
            continue
        dom_raw = str(row.get("risk_domain", "") or "").strip() if has_domain else ""
        rp = row.get("rpn")
        gt_rows.append({
            "risk_name": rn,
            "domain_norm": normalize_domain_label(dom_raw) if has_domain else "",
            "risk_id": str(row.get("risk_id", "") or ""),
            "rpn": int(rp) if pd.notna(rp) else None,
        })

    gen_rows: List[Dict[str, Any]] = []
    for json_key, j, risk in iter_risk_dicts_with_keys(generator_json):
        rn = str(risk.get("risk_name", "") or "").strip()
        if not rn:
            continue
        dom = infer_generated_domain(json_key, risk)
        gen_rows.append({
            "json_key": json_key,
            "idx": j,
            "risk": risk,
            "risk_name": rn,
            "domain_norm": normalize_domain_label(dom),
        })

    total_gt_full = len(gt_rows)
    if total_gt_full == 0:
        score = 1.0 if len(gen_rows) == 0 else 0.0
        return {
            "study_id": study_id,
            "score": round(score, 4),
            "matched": 0,
            "ground_truth_total": 0,
            "ground_truth_risk_rows_full": 0,
            "m1_scored_gt_indices": [],
            "no_risk_gt_rows": True,
            "benchmark_expects_zero_risks": True,
            "matched_names": [],
            "matched_pairs": [],
            "missed_names": [],
            "hallucination_candidates": [g["risk_name"] for g in gen_rows],
            "extra_generated_risks": [g["risk_name"] for g in gen_rows],
            "near_misses": [],
            "passed": score >= TARGETS["m1_risk_name_recall"],
        }

    eval_indices = list(range(total_gt_full))

    pair_candidates: List[Tuple[int, int]] = []
    for gi in eval_indices:
        gt = gt_rows[gi]
        for gj, gr in enumerate(gen_rows):
            if gt["risk_name"] != gr["risk_name"]:
                continue
            pair_candidates.append((gi, gj))

    pair_candidates.sort(key=lambda x: (x[0], x[1]))
    used_gi: set = set()
    used_gj: set = set()
    matched_pairs: List[Dict[str, Any]] = []
    for gi, gj in pair_candidates:
        if gi in used_gi or gj in used_gj:
            continue
        used_gi.add(gi)
        used_gj.add(gj)
        gt = gt_rows[gi]
        gr = gen_rows[gj]
        gen_risk = gr["risk"]
        gen_rpn_raw = gen_risk.get("rpn")
        try:
            gen_rpn = int(gen_rpn_raw) if gen_rpn_raw is not None and str(gen_rpn_raw).strip() != "" else None
        except (TypeError, ValueError):
            gen_rpn = None
        matched_pairs.append({
            "gt_risk_name": gt["risk_name"],
            "gt_risk_id": gt["risk_id"],
            "gt_rpn": gt["rpn"],
            "gt_domain": gt["domain_norm"],
            "generated_risk_name": gr["risk_name"],
            "generated_rpn": gen_rpn,
            "generated_json_key": gr["json_key"],
            "generated_domain": gr["domain_norm"],
        })

    total_gt = total_gt_full
    matched_names = [p["gt_risk_name"] for p in matched_pairs]
    missed_names = [gt_rows[i]["risk_name"] for i in eval_indices if i not in used_gi]

    unmatched_gt = [gt_rows[i] for i in eval_indices if i not in used_gi]
    unmatched_gen = [gen_rows[j] for j in range(len(gen_rows)) if j not in used_gj]
    hallucination_candidates = [g["risk_name"] for g in unmatched_gen]

    near_misses = find_near_misses(unmatched_gen, unmatched_gt)

    score = len(matched_pairs) / total_gt if total_gt > 0 else 0.0

    return {
        "study_id": study_id,
        "score": round(score, 4),
        "matched": len(matched_pairs),
        "ground_truth_total": total_gt,
        "ground_truth_risk_rows_full": total_gt_full,
        "m1_scored_gt_indices": list(eval_indices),
        "no_risk_gt_rows": False,
        "benchmark_expects_zero_risks": False,
        "matched_names": matched_names,
        "matched_pairs": matched_pairs,
        "missed_names": missed_names,
        "hallucination_candidates": hallucination_candidates,
        "extra_generated_risks": hallucination_candidates,
        "near_misses": near_misses,
        "passed": score >= TARGETS["m1_risk_name_recall"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 2 – RPN TIER ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def _nonblank_risk_row_count(truth_df: pd.DataFrame) -> int:
    """GT rows with non-empty risk_name (same notion as M1 ``gt_rows``)."""
    if truth_df.empty or "risk_name" not in truth_df.columns:
        return 0
    n = 0
    for _, row in truth_df.iterrows():
        if str(row.get("risk_name", "") or "").strip():
            n += 1
    return n


def compute_rpn_tier_accuracy(
    generator_json: Dict,
    truth_df: pd.DataFrame,
    matched_pairs: List[Dict[str, Any]],
) -> Dict:
    """
    M2 – RPN **tier** accuracy on **M1-matched pairs** only (TDD §6).

    Pass for a pair = tier distance **≤ 1** (±1 tier). Unknown/canonical RPN → fail.

    If there are **no M1 pairs**:
      • Benchmark has **no** risk rows → M2 is **skipped** (N/A), not a vacuous 100%.
      • Benchmark **has** risk rows but none matched → score **0**, fail.
    """
    gt_n = _nonblank_risk_row_count(truth_df)
    per_risk: List[Dict[str, Any]] = []
    passed_count = 0

    for p in matched_pairs:
        gt_rpn = p.get("gt_rpn")
        gen_rpn = p.get("generated_rpn")
        risk_label = p.get("gt_risk_name", "")

        if gen_rpn is None or gt_rpn is None:
            per_risk.append({
                "risk_name": risk_label,
                "gt_risk_id": p.get("gt_risk_id"),
                "generated_rpn": gen_rpn,
                "truth_rpn": gt_rpn,
                "tier_distance": None,
                "passed": False,
                "note": "RPN value missing from generator or ground truth",
            })
            continue

        dist = tier_distance(int(gen_rpn), int(gt_rpn))
        tier_ok = dist is not None and dist <= 1
        if tier_ok:
            passed_count += 1

        per_risk.append({
            "risk_name": risk_label,
            "gt_risk_id": p.get("gt_risk_id"),
            "generated_rpn": gen_rpn,
            "generated_tier": rpn_to_tier(int(gen_rpn)),
            "truth_rpn": gt_rpn,
            "truth_tier": rpn_to_tier(int(gt_rpn)),
            "tier_distance": dist,
            "passed": tier_ok,
            "match_mode": "tier_at_most_1",
        })

    total = len(matched_pairs)
    if total == 0:
        if gt_n == 0:
            return {
                "score": None,
                "matched_rpn": 0,
                "total_matched_risks": 0,
                "ground_truth_risk_rows": gt_n,
                "per_risk": per_risk,
                "passed": False,
                "skipped": True,
                "skip_reason": "no_benchmark_risk_rows",
            }
        return {
            "score": 0.0,
            "matched_rpn": 0,
            "total_matched_risks": 0,
            "ground_truth_risk_rows": gt_n,
            "per_risk": per_risk,
            "passed": False,
            "skipped": False,
        }

    score = passed_count / total
    return {
        "score": round(score, 4),
        "matched_rpn": passed_count,
        "total_matched_risks": total,
        "ground_truth_risk_rows": gt_n,
        "per_risk": per_risk,
        "passed": score >= TARGETS["m2_rpn_tier_accuracy"],
        "skipped": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 3 – CRITICAL FACTOR SELECTION MATCH
# ─────────────────────────────────────────────────────────────────────────────

def _gt_cf_field_blank(val: Any) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    s = str(val).strip().lower()
    return s in ("", "--", "nan", "none", "n/a", "na")


def _gen_cf_payload_nonempty(val: Any) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, str):
        t = val.strip()
        return bool(t) and t.lower() not in ("", "--", "none")
    return True


def _gen_critical_data_field(gen_factor: Dict[str, Any]) -> Any:
    u = gen_factor.get("usdm_sources") or {}
    if isinstance(u, dict) and "critical_data" in u:
        return u.get("critical_data")
    return gen_factor.get("critical_data")


def _gen_critical_process_field(gen_factor: Dict[str, Any]) -> Any:
    u = gen_factor.get("usdm_sources") or {}
    if isinstance(u, dict) and "critical_process" in u:
        return u.get("critical_process")
    return gen_factor.get("critical_process")


def compute_critical_factor_match(
    generator_json: Dict,
    factor_truth_df: pd.DataFrame,
    study_id: str,
) -> Dict:
    """
    M3 – Critical factor **name recall** (TDD §7): ``|generated ∩ GT| / |GT|``.
    Extra factor names do not reduce the score. Studies in ``FACTOR_NAME_ONLY_STUDIES``
    skip content checks when GT marks data/process blank (TDD §7.2).

    If there are **no** critical-factor rows in GT, score 1.0 only when the generator
    emits none.
    """
    if factor_truth_df.empty:
        gen_factors = generator_json.get("critical_factors", []) or []
        gen_names: List[str] = [
            str(f.get("factor_name", "") or "").strip()
            for f in gen_factors
            if isinstance(f, dict)
        ]
        gen_names = [n for n in gen_names if n]
        n_gen = len(gen_names)
        if n_gen == 0:
            return {
                "score": 1.0,
                "f1": 1.0,
                "precision": 1.0,
                "recall": 1.0,
                "matched_factors": 0,
                "ground_truth_total": 0,
                "generated_factor_count": 0,
                "matched_names": [],
                "missing_names": [],
                "extra_names": [],
                "content_violations": [],
                "factor_name_only_mode": False,
                "passed": True,
                "skipped": False,
                "benchmark_expects_zero_factors": True,
                "factor_hallucination_candidates": [],
            }
        return {
            "score": 0.0,
            "f1": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "matched_factors": 0,
            "ground_truth_total": 0,
            "generated_factor_count": n_gen,
            "matched_names": [],
            "missing_names": [],
            "extra_names": list(gen_names),
            "content_violations": [],
            "factor_name_only_mode": False,
            "passed": False,
            "skipped": False,
            "benchmark_expects_zero_factors": True,
            "factor_hallucination_candidates": list(gen_names),
        }

    factor_truth_df = factor_truth_df.drop_duplicates(subset=["critical_factor_name"], keep="first")

    gen_factors = generator_json.get("critical_factors", []) or []
    gen_names: List[str] = [
        str(f.get("factor_name", "") or "").strip()
        for f in gen_factors
        if isinstance(f, dict)
    ]
    gen_names = [n for n in gen_names if n]
    truth_names: List[str] = [
        str(x).strip() for x in factor_truth_df["critical_factor_name"].tolist()
    ]

    matched_names = [t for t in truth_names if t in gen_names]
    missing_names = [t for t in truth_names if t not in gen_names]
    extra_names = [g for g in gen_names if g not in truth_names]

    tp = len(matched_names)
    fp = len(extra_names)
    fn = len(missing_names)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    factor_name_only = study_id in FACTOR_NAME_ONLY_STUDIES
    content_violations: List[Dict[str, str]] = []
    if not factor_name_only:
        truth_by_name = factor_truth_df.set_index("critical_factor_name", drop=False)
        for gen_f in gen_factors:
            if not isinstance(gen_f, dict):
                continue
            name = str(gen_f.get("factor_name", "") or "")
            if not name or name not in truth_by_name.index:
                continue
            gt_row = truth_by_name.loc[name]
            if isinstance(gt_row, pd.DataFrame):
                gt_row = gt_row.iloc[0]
            cd_gt = gt_row.get("critical_data")
            cp_gt = gt_row.get("critical_process")
            cd_gen = _gen_critical_data_field(gen_f)
            cp_gen = _gen_critical_process_field(gen_f)

            if _gt_cf_field_blank(cd_gt) and _gen_cf_payload_nonempty(cd_gen):
                content_violations.append({
                    "factor_name": name,
                    "field": "critical_data",
                    "detail": "Ground truth marks critical_data as empty; generator supplied content.",
                })
            if _gt_cf_field_blank(cp_gt) and _gen_cf_payload_nonempty(cp_gen):
                content_violations.append({
                    "factor_name": name,
                    "field": "critical_process",
                    "detail": "Ground truth marks critical_process as empty; generator supplied content.",
                })

    score = round(recall, 4)

    return {
        "score": score,
        "f1": round(f1, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "matched_factors": tp,
        "ground_truth_total": len(truth_names),
        "generated_factor_count": len(gen_names),
        "matched_names": matched_names,
        "missing_names": missing_names,
        "extra_names": extra_names,
        "content_violations": content_violations,
        "factor_name_only_mode": factor_name_only,
        "passed": score >= TARGETS["m3_critical_factor_match"],
        "skipped": False,
        "benchmark_expects_zero_factors": False,
        "factor_hallucination_candidates": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# METADATA CONSISTENCY
# ─────────────────────────────────────────────────────────────────────────────

def validate_risk_metadata(generator_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compare summary fields to actual risk arrays (counts, high-RPN counts).

    Declared totals may appear under ``metadata`` and/or ``risk_profile_summary``
    (generator variants differ); any declared count that disagrees with the union
    of risk list keys is flagged.
    """
    md = generator_json.get("metadata") or {}
    rps = generator_json.get("risk_profile_summary") or {}
    if not isinstance(rps, dict):
        rps = {}
    risks = get_all_risk_dicts(generator_json)
    n_actual = len(risks)
    issues: List[Dict[str, Any]] = []

    for src, declared in (
        ("metadata.total_risks", md.get("total_risks")),
        ("risk_profile_summary.total_risks", rps.get("total_risks")),
    ):
        if declared is None:
            continue
        try:
            if int(declared) != n_actual:
                issues.append({
                    "code": "total_risks_mismatch",
                    "message": (
                        f"{src} is {declared} but the union of risk arrays "
                        f"contains {n_actual} risk objects."
                    ),
                })
        except (TypeError, ValueError):
            issues.append({
                "code": "total_risks_unparseable",
                "message": f"{src}={declared!r} is not an integer.",
            })

    count_high = 0
    for r in risks:
        try:
            rp = int(r.get("rpn", 0) or 0)
            if rp > 196:
                count_high += 1
        except (TypeError, ValueError):
            pass

    for key in ("risks_above_196", "risks_above_196_count"):
        for src_prefix, block in (("metadata", md), ("risk_profile_summary", rps)):
            if key not in block:
                continue
            try:
                if int(block[key]) != count_high:
                    issues.append({
                        "code": "risks_above_196_mismatch",
                        "message": (
                            f"{src_prefix}.{key} is {block[key]} but {count_high} risks "
                            f"have RPN > 196."
                        ),
                    })
            except (TypeError, ValueError):
                issues.append({
                    "code": "risks_above_196_unparseable",
                    "message": f"{src_prefix}.{key}={block[key]!r} is not an integer.",
                })

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "actual_total_risks": n_actual,
        "actual_risks_above_196": count_high,
    }


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 4 – HALLUCINATION DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def compute_hallucination_detection(
    generator_json: Dict,
    *,
    gt_risk_exists: bool = True,
    gt_cf_exists: bool = True,
    study_id: str = "",
    semantic_hallucination_count: int = 0,
    usdm_index: Optional[UsdmIndex] = None,
) -> Dict:
    """
    Compute M4 – Hallucination Detection.

    Flags two classes of defect:

    1. **Shape defects** — ``usdm_drivers`` / ``usdm_sources.critical_data``
       missing or empty, ``intelligence.benchmark_source`` missing,
       ``usdm_trigger`` object missing entity/signal. These are purely
       generator-side checks and don't need the protocol USDM.

    2. **Trace defects** — every reference that *is* present must actually
       resolve to a node in the uploaded USDM JSON (by id, or by
       ``(instanceType, name)`` match). References that don't resolve are
       reported as hallucinations. See ``risk_usdm_tracing.trace_reference``.

    When ``usdm_index`` is ``None`` (no USDM uploaded), the trace defects
    sub-check is skipped — only the shape checks run, and each trace entry
    is reported as ``trace_status = 'no_usdm'`` rather than hallucinated.

    Args:
        generator_json: Parsed generator output dict
        usdm_index: Optional pre-built USDM index from ``build_usdm_index``

    Returns:
        Dict with ``flagged_fields`` now carrying per-reference tracing
        detail: ``field_path``, ``entity``, ``signal``, ``usdm_id``,
        ``trace_status``, ``matched_node_id``, ``candidates``, ``reason``.
    """
    flagged: List[Dict] = []
    usdm_available = usdm_index is not None and not usdm_index.empty
    generated_risks = get_all_risk_dicts(generator_json)
    gen_factors_list = [
        f for f in (generator_json.get("critical_factors") or []) if isinstance(f, dict)
    ]

    # Rule 0 (existence gate): if GT has no risk profile rows for this study,
    # generator must output zero risks. Any generated risk is a hallucination.
    if not gt_risk_exists and len(generated_risks) > 0:
        for key, i, risk in iter_risk_dicts_with_keys(generator_json):
            flagged.append(
                {
                    "field_path": f"{key}[{i}]",
                    "value": str(risk.get("risk_name") or f"Risk[{i}]"),
                    "rule": (
                        f"Risk existence mismatch: study '{study_id}' has no risk profile in GT; "
                        f"expected 0 generated risks."
                    ),
                }
            )

    # Rule 0b: no critical-factor benchmark rows → expect zero generated critical factors.
    if not gt_cf_exists and len(gen_factors_list) > 0:
        for i, factor in enumerate(gen_factors_list):
            flagged.append(
                {
                    "field_path": f"critical_factors[{i}]",
                    "value": str(factor.get("factor_name") or f"Factor[{i}]"),
                    "rule": (
                        f"Critical factor existence mismatch: study '{study_id}' has no rows in "
                        f"critical_factors_ground_truth.csv; expected 0 generated critical factors."
                    ),
                }
            )

    # ── Check risks (all list locations) ───────────────────────────────────────
    for key, i, risk in iter_risk_dicts_with_keys(generator_json):
        risk_name = risk.get("risk_name", f"Risk[{i}]")
        prefix = f"{key}[{i}] ({risk_name!r})"

        # Rule 1: intelligence.benchmark_source must be non-null and non-empty
        intelligence = risk.get("intelligence", {})
        benchmark_source = intelligence.get("benchmark_source")
        if not benchmark_source or str(benchmark_source).strip() in ("", "null", "None"):
            flagged.append({
                "field_path": f"{prefix}.intelligence.benchmark_source",
                "value": benchmark_source,
                "rule": "intelligence.benchmark_source must be non-null and non-empty",
            })

        # Rule 2a (shape): usdm_drivers must be a non-empty list.
        usdm_drivers = risk.get("usdm_drivers")
        if not usdm_drivers or not isinstance(usdm_drivers, list) or len(usdm_drivers) == 0:
            flagged.append({
                "field_path": f"{prefix}.usdm_drivers",
                "value": usdm_drivers,
                "rule": "usdm_drivers must be a non-empty list",
            })

        # Rule 3a (shape): every associated_cause must have usdm_trigger with
        # entity + signal.
        for j, cause in enumerate(risk.get("associated_causes", [])):
            cause_text = cause.get("cause", f"cause[{j}]")
            trigger = cause.get("usdm_trigger")
            if not trigger:
                flagged.append({
                    "field_path": f"{prefix}.associated_causes[{j}] ({cause_text!r}).usdm_trigger",
                    "value": None,
                    "rule": "usdm_trigger is required for every associated_cause",
                })
            else:
                if not trigger.get("entity"):
                    flagged.append({
                        "field_path": f"{prefix}.associated_causes[{j}].usdm_trigger.entity",
                        "value": trigger.get("entity"),
                        "rule": "usdm_trigger.entity must be non-empty",
                    })
                if not trigger.get("signal"):
                    flagged.append({
                        "field_path": f"{prefix}.associated_causes[{j}].usdm_trigger.signal",
                        "value": trigger.get("signal"),
                        "rule": "usdm_trigger.signal must be non-empty",
                    })

        # Rules 2b/3b (trace): every usdm_drivers entry and usdm_trigger must
        # resolve to a real USDM node. Skip if USDM not uploaded.
        if usdm_available:
            for issue in collect_risk_trace_issues(risk, usdm_index):
                flagged.append({
                    "field_path": f"{prefix}.{issue['field_path']}",
                    "value": {
                        "entity": issue["entity"],
                        "signal": issue["signal"],
                        "usdm_id": issue["usdm_id"],
                    },
                    "rule": (
                        "Reference must resolve to a USDM node by id or "
                        "(instanceType, name) match"
                    ),
                    "trace_status": issue["status"],
                    "matched_node_id": issue.get("matched_node_id"),
                    "candidates": issue.get("candidates") or [],
                    "reason": issue.get("reason", ""),
                })

    # ── Check critical factors ─────────────────────────────────────────────────
    for i, factor in enumerate(generator_json.get("critical_factors", [])):
        factor_name = factor.get("factor_name", f"Factor[{i}]")
        prefix = f"critical_factors[{i}] ({factor_name!r})"

        # Rule 4a (shape): usdm_sources.critical_data must be a non-empty list.
        usdm_sources = factor.get("usdm_sources", {})
        critical_data_sources = usdm_sources.get("critical_data") if usdm_sources else None
        if (
            not critical_data_sources
            or not isinstance(critical_data_sources, list)
            or len(critical_data_sources) == 0
        ):
            # critical_data can legitimately be a dict in older generator
            # outputs; still require at least one reference dict.
            if not (isinstance(critical_data_sources, dict) and critical_data_sources):
                flagged.append({
                    "field_path": f"{prefix}.usdm_sources.critical_data",
                    "value": critical_data_sources,
                    "rule": "usdm_sources.critical_data must be a non-empty list",
                })

        # Rule 4b (trace): every critical_data / critical_process reference
        # must resolve in the USDM index.
        if usdm_available:
            for issue in collect_factor_trace_issues(factor, usdm_index):
                flagged.append({
                    "field_path": f"{prefix}.{issue['field_path']}",
                    "value": {
                        "entity": issue["entity"],
                        "signal": issue["signal"],
                        "usdm_id": issue["usdm_id"],
                    },
                    "rule": (
                        "Reference must resolve to a USDM node by id or "
                        "(instanceType, name) match"
                    ),
                    "trace_status": issue["status"],
                    "matched_node_id": issue.get("matched_node_id"),
                    "candidates": issue.get("candidates") or [],
                    "reason": issue.get("reason", ""),
                })

    provenance_count = len(flagged)
    return {
        "provenance_defect_count": provenance_count,
        "semantic_hallucination_count": semantic_hallucination_count,
        "hallucinations_found": provenance_count,
        "flagged_fields": flagged,
        "passed": provenance_count == 0,
        "risk_existence_check": {
            "gt_risk_exists": bool(gt_risk_exists),
            "generated_risk_count": len(generated_risks),
            "passed": not (not gt_risk_exists and len(generated_risks) > 0),
        },
        "cf_existence_check": {
            "gt_cf_exists": bool(gt_cf_exists),
            "generated_cf_count": len(gen_factors_list),
            "passed": not (not gt_cf_exists and len(gen_factors_list) > 0),
        },
        "note": (
            "hallucinations_found counts **provenance / schema defect checks** per risk field, "
            "not semantic hallucinations. Use semantic_hallucination_count (from M1) for "
            "generated risks with no GT match."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario1_eval(
    generator_json_path: str,
    ground_truth_risks_csv: str,
    ground_truth_factors_csv: str,
    study_id: str,
    *,
    allow_empty_risk_gt: bool = False,
    usdm_json_path: Optional[str] = None,
) -> Dict:
    """
    Orchestrate the full Scenario 1 evaluation for one study.

    Runs all 4 metrics in sequence, assembles results into a standardised result dict,
    determines the overall GO/NO-GO verdict (all metrics must pass simultaneously),
    and adds timestamp + metadata. This is the primary entry point called by
    run_eval.py and api.py.

    The verdict is GO only if M1, M2, M3 (where applicable), and M4 all pass.
    M3 with no CF benchmark rows expects zero generated critical factors (same as risks).

    Args:
        generator_json_path: Path to {study_id}_RiskProfile.json
        ground_truth_risks_csv: Path to risk_profile_ground_truth.csv
        ground_truth_factors_csv: Path to critical_factors_ground_truth.csv
        study_id: Study identifier for filtering and labelling
        allow_empty_risk_gt: If True, a study with zero risk GT rows loads an empty table;
            any generated risks are scored as extras (no GT counterpart), not a load error.

    Returns:
        Fully populated result dict including all metric results and overall verdict.
    """
    timestamp = datetime.utcnow().isoformat() + "Z"

    # ── Load data ──────────────────────────────────────────────────────────────
    generator_json = load_generator_json(generator_json_path)
    risk_truth_df = load_risk_ground_truth(
        ground_truth_risks_csv, study_id, allow_empty=allow_empty_risk_gt
    )

    # Critical factors may not exist for all verify studies
    try:
        factor_truth_df = load_factor_ground_truth(ground_truth_factors_csv, study_id)
    except (FileNotFoundError, ValueError):
        factor_truth_df = pd.DataFrame()

    # ── M1 ─────────────────────────────────────────────────────────────────────
    m1 = compute_risk_name_recall(generator_json, risk_truth_df, study_id)

    # ── M2 (only on M1 domain-matched pairs) ───────────────────────────────────
    m2 = compute_rpn_tier_accuracy(
        generator_json, risk_truth_df, m1.get("matched_pairs", [])
    )

    # ── M3 ─────────────────────────────────────────────────────────────────────
    m3 = compute_critical_factor_match(generator_json, factor_truth_df, study_id)

    # ── M4 ─────────────────────────────────────────────────────────────────────
    gt_risk_exists = not risk_truth_df.empty
    gt_cf_exists = not factor_truth_df.empty
    sem_hall = len(m1.get("hallucination_candidates", []) or [])
    usdm_root = load_usdm(usdm_json_path) if usdm_json_path else None
    usdm_idx = build_usdm_index(usdm_root) if usdm_root is not None else None
    m4 = compute_hallucination_detection(
        generator_json,
        gt_risk_exists=gt_risk_exists,
        gt_cf_exists=gt_cf_exists,
        study_id=study_id,
        semantic_hallucination_count=sem_hall,
        usdm_index=usdm_idx,
    )

    metadata_validation = validate_risk_metadata(generator_json)

    # ── Overall verdict (TDD §10 — four metrics only) ───────────────────────────
    # M2 skipped (no benchmark risk rows) does not block GO by itself; M3 is never skipped now
    m2_verdict = m2["passed"] if not m2.get("skipped") else True
    m3_verdict = bool(m3.get("passed"))
    all_passed = m1["passed"] and m2_verdict and m3_verdict and m4["passed"]

    verdict = "GO" if all_passed else "NO-GO"

    # ── Build result dict ──────────────────────────────────────────────────────
    metadata = generator_json.get("metadata", {})
    return {
        "study_id": study_id,
        "scenario": 1,
        "timestamp": timestamp,
        "verdict": verdict,
        "generator_version": metadata.get("generator_version", "unknown"),
        "ta": (
            risk_truth_df["ta"].iloc[0]
            if not risk_truth_df.empty
            else (
                factor_truth_df["ta"].iloc[0]
                if not factor_truth_df.empty and "ta" in factor_truth_df.columns
                else "unknown"
            )
        ),
        "phase": (
            risk_truth_df["phase"].iloc[0]
            if not risk_truth_df.empty
            else (
                factor_truth_df["phase"].iloc[0]
                if not factor_truth_df.empty and "phase" in factor_truth_df.columns
                else "unknown"
            )
        ),
        "metrics": {
            "m1_risk_name_recall": m1,
            "m2_rpn_tier_accuracy": m2,
            "m3_critical_factor_match": m3,
            "m4_hallucination_detection": m4,
        },
        "targets": TARGETS,
        "near_misses": m1["near_misses"],
        "metadata_validation": metadata_validation,
        "hallucination_report": {
            "study_id": study_id,
            "provenance_defect_count": m4.get("provenance_defect_count", m4.get("hallucinations_found")),
            "semantic_hallucination_count": m4.get("semantic_hallucination_count", sem_hall),
            "hallucinations_found": m4["hallucinations_found"],
            "flagged_fields": m4["flagged_fields"],
            "pass": m4["passed"],
        },
        "gt_risk_exists": gt_risk_exists,
        "gt_cf_exists": gt_cf_exists,
        "usdm_trace": {
            "usdm_loaded": usdm_idx is not None and not usdm_idx.empty,
            "usdm_path": str(Path(usdm_json_path).resolve()) if usdm_json_path else None,
            "usdm_node_count": len(usdm_idx.by_id) if usdm_idx else 0,
            "usdm_instance_types": sorted(usdm_idx.by_type.keys())[:40] if usdm_idx else [],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# FAILURE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def classify_failures(results: Dict) -> List[Dict]:
    """
    Post-process a Scenario 1 result dict into a human-readable, prioritised failure list.

    For each failing metric, generates a structured entry with: metric name, severity,
    actual value, target value, root cause hint, and recommended generator fix.

    Useful for reporting to the generator developer after each eval run.
    Sorted CRITICAL first, HIGH second, then alphabetically within each severity.

    Args:
        results: Output dict from run_scenario1_eval()

    Returns:
        List of failure dicts. Empty list = all metrics passed (GO verdict).
    """
    failures = []
    metrics = results.get("metrics", {})
    study_id = results.get("study_id", "")

    # M1
    m1 = metrics.get("m1_risk_name_recall", {})
    if m1 and not m1.get("passed"):
        gt_total = int(m1.get("ground_truth_total") or 0)
        gen_hall = list(m1.get("hallucination_candidates") or [])
        if gt_total == 0:
            detail = (
                f"risk_profile_ground_truth.csv defines no risks for this protocol: the correct output is "
                f"zero risk objects. Generator emitted {len(gen_hall)} risk(s) with no benchmark counterpart "
                f"(each counts as hallucination / extra): {gen_hall}."
            )
            root_cause = (
                "Benchmark has no risk rows for this study_id; any non-empty risk output violates the benchmark."
            )
            generator_fix = (
                "Emit empty risk lists for this protocol until benchmark rows exist, or add the protocol to "
                "risk_profile_ground_truth.csv with the authorized risk set."
            )
        else:
            detail = (
                f"Matched {m1.get('matched')}/{m1.get('ground_truth_total')} ground truth risks. "
                f"Missed: {m1.get('missed_names')}. "
                f"Near misses: {[nm['generated_name'] for nm in m1.get('near_misses', [])]}."
            )
            root_cause = "YAML occurrence_rate below selection_threshold OR wrong TA/Phase bucket"
            generator_fix = "Lower selection_threshold or add missing risks to TA/Phase bucket YAML"
        failures.append({
            "study_id": study_id,
            "metric": "M1 – Risk Name Recall",
            "severity": "CRITICAL",
            "actual": m1.get("score"),
            "target": TARGETS["m1_risk_name_recall"],
            "detail": detail,
            "root_cause": root_cause,
            "generator_fix": generator_fix,
        })

    # M2
    m2 = metrics.get("m2_rpn_tier_accuracy", {})
    if m2 and not m2.get("skipped") and not m2.get("passed"):
        failed_risks = [r["risk_name"] for r in m2.get("per_risk", []) if not r.get("passed")]
        gt_n = int(m2.get("ground_truth_risk_rows") or 0)
        if int(m2.get("total_matched_risks") or 0) == 0 and gt_n > 0:
            detail = (
                f"No M1 pairs ({gt_n} benchmark risk row(s) did not match any generated risk). "
                f"RPN tier cannot be scored until M1 recall improves."
            )
        else:
            detail = (
                f"RPN within ±1 tier on M1 pairs: {m2.get('matched_rpn')}/{m2.get('total_matched_risks')}. "
                f"Failed risks: {failed_risks}."
            )
        failures.append({
            "study_id": study_id,
            "metric": "M2 – RPN Tier Accuracy",
            "severity": "CRITICAL",
            "actual": m2.get("score"),
            "target": TARGETS["m2_rpn_tier_accuracy"],
            "detail": detail,
            "root_cause": "YAML typical_impact or typical_likelihood wrong for this TA/Phase bucket",
            "generator_fix": "Recalculate typical RPN values from train set. Check USDM signal adjustments.",
        })

    # M3
    m3 = metrics.get("m3_critical_factor_match", {})
    if m3 and not m3.get("skipped") and not m3.get("passed"):
        cv = m3.get("content_violations") or []
        if m3.get("benchmark_expects_zero_factors") and int(m3.get("ground_truth_total") or 0) == 0:
            extras = m3.get("extra_names") or m3.get("factor_hallucination_candidates") or []
            failures.append({
                "study_id": study_id,
                "metric": "M3 – Critical Factor Selection",
                "severity": "HIGH",
                "actual": m3.get("score"),
                "target": TARGETS["m3_critical_factor_match"],
                "detail": (
                    f"critical_factors_ground_truth.csv defines no factors for this protocol; "
                    f"correct output is zero critical factor objects. Generator emitted {len(extras)}: {extras}."
                ),
                "root_cause": (
                    "Benchmark has no critical-factor rows for this study_id; any non-empty critical_factors "
                    "output violates the benchmark."
                ),
                "generator_fix": (
                    "Emit an empty critical_factors array until benchmark rows exist, or add this protocol to "
                    "critical_factors_ground_truth.csv."
                ),
            })
        else:
            failures.append({
                "study_id": study_id,
                "metric": "M3 – Critical Factor Selection",
                "severity": "HIGH",
                "actual": m3.get("score"),
                "target": TARGETS["m3_critical_factor_match"],
                "detail": (
                    f"Name recall (TDD §7.1)={m3.get('score')}, matched names={m3.get('matched_factors')}/"
                    f"{m3.get('ground_truth_total')}, missing={m3.get('missing_names')}, "
                    f"extras (not scored)={m3.get('extra_names')}. "
                    f"Informational: precision={m3.get('precision')}, F1={m3.get('f1')}; "
                    f"content notes={cv}."
                ),
                "root_cause": "factor_selection bucket incomplete for this TA/Phase",
                "generator_fix": "Update critical_data_benchmarks.yaml factor_selection section",
            })

    # M4
    m4 = metrics.get("m4_hallucination_detection", {})
    if m4 and not m4.get("passed"):
        flagged = [f["field_path"] for f in m4.get("flagged_fields", [])]
        existence = m4.get("risk_existence_check") or {}
        prov = m4.get("provenance_defect_count", m4.get("hallucinations_found"))
        sem = m4.get("semantic_hallucination_count", 0)
        ff = m4.get("flagged_fields") or []
        existence_mismatch = any("Risk existence mismatch" in str(f.get("rule", "")) for f in ff)
        cf_existence_mismatch = any(
            "Critical factor existence mismatch" in str(f.get("rule", "")) for f in ff
        )
        if existence_mismatch or cf_existence_mismatch:
            detail_parts: List[str] = []
            if existence_mismatch:
                n_ex = len([f for f in ff if "Risk existence mismatch" in str(f.get("rule", ""))])
                detail_parts.append(
                    f"Risks: benchmark (risk_profile_ground_truth.csv) specifies zero risks; "
                    f"generator emitted {n_ex} risk object(s) (hallucination / extra)."
                )
            if cf_existence_mismatch:
                n_cf = len([f for f in ff if "Critical factor existence mismatch" in str(f.get("rule", ""))])
                detail_parts.append(
                    f"Critical factors: benchmark (critical_factors_ground_truth.csv) specifies zero factors; "
                    f"generator emitted {n_cf} critical factor object(s) (hallucination / extra)."
                )
            m4_detail = " ".join(detail_parts)
            if existence_mismatch and cf_existence_mismatch:
                m4_root = "Generator must emit no risks and no critical factors when both benchmarks are empty for this protocol."
                m4_fix = (
                    "Gate on study_id: empty risk arrays when no risk GT rows; empty critical_factors when no CF GT rows."
                )
            elif existence_mismatch:
                m4_root = (
                    "Generator output must be empty for all risk arrays when the protocol has no risk benchmark rows."
                )
                m4_fix = (
                    "Gate generation: if study_id has no rows in risk_profile_ground_truth.csv, emit no risks."
                )
            else:
                m4_root = (
                    "Generator output must have no critical_factors when the protocol has no CF benchmark rows."
                )
                m4_fix = (
                    "Gate generation: if study_id has no rows in critical_factors_ground_truth.csv, "
                    "emit critical_factors=[]."
                )
        else:
            m4_detail = (
                f"Provenance/schema defects: {prov} (target 0). "
                f"Semantic unmatched generated risks (M1): {sem}. "
                f"Flagged fields ({len(flagged)}): {flagged}"
            )
            m4_root = "Generator producing content without usdm_trigger or benchmark_source provenance"
            m4_fix = "Add usdm_trigger to every associated_cause; ensure benchmark_source is always set"
        failures.append({
            "study_id": study_id,
            "metric": "M4 – Hallucination Detection",
            "severity": "CRITICAL",
            "actual": prov,
            "target": 0,
            "detail": m4_detail,
            "root_cause": m4_root,
            "generator_fix": m4_fix,
        })

    # Sort: CRITICAL before HIGH
    severity_order = {"CRITICAL": 0, "HIGH": 1}
    failures.sort(key=lambda f: severity_order.get(f["severity"], 99))
    return failures


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="D1 Risk Profile – Scenario 1 Eval (ground truth available)"
    )
    parser.add_argument("--generator_json", required=True,
                        help="Path to {study_id}_RiskProfile.json")
    parser.add_argument("--ground_truth_risks", required=True,
                        help="Path to risk_profile_ground_truth.csv")
    parser.add_argument("--ground_truth_factors", required=True,
                        help="Path to critical_factors_ground_truth.csv")
    parser.add_argument("--study_id", required=True,
                        help="Study / protocol identifier")
    parser.add_argument("--output_json", default=None,
                        help="Optional path to write JSON result (default: stdout only)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    print(f"\n{'='*60}")
    print(f"D1 Risk Profile Eval – Scenario 1")
    print(f"Study: {args.study_id}")
    print(f"{'='*60}\n")

    results = run_scenario1_eval(
        generator_json_path=args.generator_json,
        ground_truth_risks_csv=args.ground_truth_risks,
        ground_truth_factors_csv=args.ground_truth_factors,
        study_id=args.study_id,
    )

    # Print scorecard
    metrics = results["metrics"]
    print(f"VERDICT: {results['verdict']}\n")
    print(f"M1 Risk Name Recall:    {metrics['m1_risk_name_recall']['score']:.1%}  "
          f"(target {TARGETS['m1_risk_name_recall']:.0%})  "
          f"{'PASS' if metrics['m1_risk_name_recall']['passed'] else 'FAIL'}")
    m2m = metrics["m2_rpn_tier_accuracy"]
    if m2m.get("skipped"):
        m2_line = (
            f"M2 RPN tier (±1):      N/A  "
            f"(target {TARGETS['m2_rpn_tier_accuracy']:.0%})  SKIP (no benchmark risk rows)"
        )
    else:
        m2_line = (
            f"M2 RPN tier (±1):      {m2m['score']:.1%}  "
            f"(target {TARGETS['m2_rpn_tier_accuracy']:.0%})  "
            f"{'PASS' if m2m['passed'] else 'FAIL'}"
        )
    print(m2_line)
    m3 = metrics["m3_critical_factor_match"]
    m3_str = f"{m3['score']:.1%}" if m3["score"] is not None else "SKIPPED"
    print(f"M3 Critical Factors:    {m3_str}  "
          f"(target {TARGETS['m3_critical_factor_match']:.0%})  "
          f"{'PASS' if m3.get('passed') else ('SKIP' if m3.get('skipped') else 'FAIL')}")
    m4d = metrics["m4_hallucination_detection"]
    m4p = m4d.get("provenance_defect_count", m4d.get("hallucinations_found"))
    m4s = m4d.get("semantic_hallucination_count", 0)
    print(f"M4 Provenance defects:  {m4p}  (target 0)  "
          f"{'PASS' if metrics['m4_hallucination_detection']['passed'] else 'FAIL'}")
    print(f"M4 Semantic (M1 extra): {m4s}  (informational; see M1 recall)")

    if results.get("near_misses"):
        print(f"\nNear misses ({len(results['near_misses'])}):")
        for nm in results["near_misses"]:
            print(f"  GT: '{nm['truth_name']}' ← Gen: '{nm['generated_name']}' (dist={nm['edit_distance']})")

    failures = classify_failures(results)
    if failures:
        print(f"\nFailures ({len(failures)}):")
        for f in failures:
            print(f"  [{f['severity']}] {f['metric']}: {f['detail']}")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        print(f"\nResults written to: {out_path}")
