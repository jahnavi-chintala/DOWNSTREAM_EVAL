"""
eval_scenario2.py
-----------------
PIPD Eval Framework – Scenario 2: NO ground truth (test studies, May 18 live window).

Cannot measure accuracy (no answer key).  Instead measures 7 proxy quality
signals that evaluate internal consistency and plausibility of the generator output.

When ground truth and a USDM protocol JSON *are* available (e.g. verifying an
intelligence-style PIPD output against Actual PIPD and the protocol), use
``pipd_composite_report.py`` or ``pipd_intelligence_truth_report.py`` instead;
see ``reference_spec/PIPD_Eval_USDM_and_Intelligence_Reference.md``.

A high Scenario 2 score means "the generator behaved correctly given what it knows"
– NOT "the generator got it right".  Human review of flagged items is mandatory.

Proxy signals:
  S1 – Hallucination check          (auto_confirmed must have valid usdm_entity + rate > 0)
  S2 – Confidence distribution      (no category > 50% low_confidence)
  S3 – Category completeness        (all 11 categories present)
  S4 – Protocol specificity Cat 10  (Cat 10 never auto_confirmed)
  S5 – Benchmark alignment          (auto_confirmed rate >= 0.70)
  S6 – Subcategory count sanity     (count within 3x of segment average)
  S7 – None-identified plausibility (none_identified=True where rate > 80% unusual)

Usage (standalone):
    python3 eval_scenario2.py \\
        --generator_json B9999999_PIPD.json \\
        --deviation_benchmarks deviation_subcategories_clean.csv \\
        --study_id B9999999 \\
        --output_json B9999999_s2_results.json
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd    # benchmark CSV loading, groupby for segment averages

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

NUM_CATEGORIES: int = 11

BENCHMARK_RATE_THRESHOLD: float = 0.70   # auto_confirmed subcats must have rate >= this
LOW_CONF_THRESHOLD: float       = 0.50   # > 50% low_confidence in a category → WARN
COUNT_SANITY_MULTIPLIER: int    = 3      # count > 3x average → hallucination explosion risk
NONE_IDENT_FLAG_RATE: float     = 0.80   # none_identified=True unusual if segment rate > this

# Signal severity ratings
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Confidence values that are valid (anything else is a schema problem)
VALID_CONFIDENCE_VALUES = {"auto_confirmed", "review", "low_confidence", "category_10"}
NULL_PLACEHOLDERS = {"", "null", "none", "n/a", "placeholder", "tbd"}


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_generator_json(json_path: str) -> Dict[str, Any]:
    """
    Load and parse the generator output JSON for a test study.

    Args:
        json_path : Path to {study_id}_PIPD.json

    Returns:
        Parsed JSON dictionary
    """
    with open(json_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_benchmark_data(csv_path: str) -> pd.DataFrame:
    """
    Load deviation_subcategories_clean.csv which holds historical benchmark
    rates for subcategory occurrence by TA / phase / segment.

    Expected columns include: category_num, ta, phase, subcategory_text,
    segment_rate (or similar – the script adapts to available columns).

    Args:
        csv_path : Path to deviation_subcategories_clean.csv

    Returns:
        pandas DataFrame with benchmark data
    """
    df = pd.read_csv(csv_path, dtype=str)
    # Normalise numeric columns
    for col in ["segment_rate", "rate", "frequency"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "category_num" in df.columns:
        df["category_num"] = pd.to_numeric(df["category_num"], errors="coerce")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# DATA EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def extract_all_subcategories(generator_json: Dict) -> List[Dict]:
    """
    Flatten all subcategories from all categories in the generator JSON
    into a single list.  Each item is annotated with _category_num for
    downstream signal checks.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        List of subcategory dicts, each with added '_category_num' key
    """
    result: List[Dict] = []
    for cat in generator_json.get("categories", []):
        cat_num = cat.get("category_num")
        for sub in cat.get("subcategories", []):
            annotated = dict(sub)
            annotated["_category_num"] = cat_num
            result.append(annotated)
    return result


def get_categories_present(generator_json: Dict) -> List[int]:
    """
    Return the list of category_num values present in the generator JSON.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        Sorted list of category numbers found
    """
    return sorted([int(cat["category_num"]) for cat in generator_json.get("categories", [])
                   if "category_num" in cat])


def get_none_identified_map(generator_json: Dict) -> Dict[int, bool]:
    """
    Extract the none_identified flag per category.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        { category_num: none_identified_bool }
    """
    result: Dict[int, bool] = {}
    for cat in generator_json.get("categories", []):
        num = cat.get("category_num")
        if num is not None:
            result[int(num)] = bool(cat.get("none_identified", False))
    return result


def get_benchmark_rate(sub: Dict) -> Optional[float]:
    """
    Safely extract the benchmark segment rate from a subcategory dict.

    The nested path is: benchmark.segment.rate

    Args:
        sub : Single subcategory dict from generator JSON

    Returns:
        Float rate if present and valid, else None
    """
    try:
        return float(sub["benchmark"]["segment"]["rate"])
    except (KeyError, TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1 – HALLUCINATION CHECK
# ─────────────────────────────────────────────────────────────────────────────

def check_hallucination_signals(all_subcats: List[Dict]) -> Dict:
    """
    S1 – Hallucination check.

    For every auto_confirmed subcategory, verify that:
      (a) usdm_entity is non-null, AND
      (b) benchmark.segment.rate > 0  OR  confidence is review/low_confidence

    An auto_confirmed subcat without either provenance tag OR a plausible
    benchmark rate is likely hallucinated.

    Args:
        all_subcats : Flattened list of all subcategory dicts

    Returns:
        {
          status: PASS|FAIL,
          flagged: [ { category_num, subcategory_text, usdm_entity, rate, issue } ],
          message: str
        }
    """
    flagged: List[Dict] = []

    for sub in all_subcats:
        confidence = sub.get("confidence", "")
        if confidence != "auto_confirmed":
            continue

        entity    = sub.get("usdm_entity")
        entity_id = sub.get("usdm_entity_id")
        rate      = get_benchmark_rate(sub)

        issues: List[str] = []

        if entity is None or str(entity).strip().lower() in NULL_PLACEHOLDERS:
            issues.append("null usdm_entity – likely hallucinated")
        if entity_id is None or str(entity_id).strip().lower() in NULL_PLACEHOLDERS:
            issues.append("null usdm_entity_id – no protocol traceability")
        if rate is None or rate <= 0:
            issues.append(f"benchmark rate={rate} – must be > 0 for auto_confirmed")

        if issues:
            flagged.append({
                "category_num":     sub.get("_category_num"),
                "subcategory_text": sub.get("subcategory_text"),
                "usdm_entity":      entity,
                "usdm_entity_id":   entity_id,
                "benchmark_rate":   rate,
                "issues":           issues,
            })

    status = FAIL if flagged else PASS
    return {
        "signal":   "S1_HALLUCINATION_CHECK",
        "status":   status,
        "flagged":  flagged,
        "message":  (f"{len(flagged)} auto_confirmed subcat(s) with missing provenance or zero rate"
                     if flagged else "All auto_confirmed subcats have valid provenance and rate > 0"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2 – CONFIDENCE DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def check_confidence_distribution(generator_json: Dict) -> Dict:
    """
    S2 – Confidence distribution.

    Within each non-empty category, compute the fraction of subcategories
    at each confidence level.  Flag if more than 50% are 'low_confidence'
    (indicates the generator is struggling with this TA/phase combination).

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        {
          status: PASS|WARN|FAIL,
          per_category: { cat_num: { auto_confirmed%, review%, low_confidence%, total } },
          warnings: [ { category_num, low_confidence_fraction, message } ],
          message: str
        }
    """
    per_category: Dict[int, Dict] = {}
    warnings: List[Dict] = []

    for cat in generator_json.get("categories", []):
        cat_num  = int(cat.get("category_num", 0))
        subcats  = cat.get("subcategories", [])
        if not subcats:
            continue

        counts: Dict[str, int] = {lvl: 0 for lvl in VALID_CONFIDENCE_VALUES}
        for sub in subcats:
            lvl = sub.get("confidence", "")
            if lvl in counts:
                counts[lvl] += 1

        total       = len(subcats)
        low_frac    = counts["low_confidence"] / total
        auto_frac   = counts["auto_confirmed"] / total
        review_frac = counts["review"]          / total
        cat10_frac  = counts["category_10"]     / total

        per_category[cat_num] = {
            "total":             total,
            "auto_confirmed_pct": round(auto_frac   * 100, 1),
            "review_pct":         round(review_frac * 100, 1),
            "low_confidence_pct": round(low_frac    * 100, 1),
            "category_10_pct":    round(cat10_frac  * 100, 1),
        }

        if low_frac > LOW_CONF_THRESHOLD:
            warnings.append({
                "category_num":        cat_num,
                "low_confidence_frac": round(low_frac, 3),
                "message": f"{low_frac:.0%} low_confidence in category {cat_num} – generator struggling",
            })

    status = WARN if warnings else PASS
    return {
        "signal":       "S2_CONFIDENCE_DISTRIBUTION",
        "status":       status,
        "per_category": per_category,
        "warnings":     warnings,
        "message":      (f"{len(warnings)} category/ies exceed 50% low_confidence"
                         if warnings else "Confidence distribution is healthy across all categories"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 3 – CATEGORY COMPLETENESS
# ─────────────────────────────────────────────────────────────────────────────

def check_category_completeness(generator_json: Dict) -> Dict:
    """
    S3 – Category completeness.

    All 11 deviation categories must be present in the generator output JSON.
    A missing category means the generator failed to process that section of
    the USDM JSON.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        {
          status: PASS|FAIL,
          categories_present: [int],
          categories_missing: [int],
          message: str
        }
    """
    present  = set(get_categories_present(generator_json))
    expected = set(range(1, NUM_CATEGORIES + 1))
    missing  = sorted(expected - present)

    status = FAIL if missing else PASS
    return {
        "signal":               "S3_CATEGORY_COMPLETENESS",
        "status":               status,
        "categories_present":   sorted(present),
        "categories_missing":   missing,
        "message":              (f"Missing categories: {missing}" if missing
                                 else "All 11 categories present"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 4 – PROTOCOL SPECIFICITY (CATEGORY 10)
# ─────────────────────────────────────────────────────────────────────────────

def check_protocol_specificity_cat10(generator_json: Dict) -> Dict:
    """
    S4 – Protocol specificity check for Category 10 (Other).

    Category 10 subcategories are fully protocol-specific (WISC-V,
    suicidal ideation questionnaire, etc.) and must never be auto_confirmed.
    If any Cat 10 subcat has confidence='auto_confirmed', the generator is
    using generic patterns instead of protocol-specific content.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        {
          status: PASS|FAIL,
          violations: [ { subcategory_text, confidence } ],
          message: str
        }
    """
    violations: List[Dict] = []

    for cat in generator_json.get("categories", []):
        if int(cat.get("category_num", 0)) != 10:
            continue
        for sub in cat.get("subcategories", []):
            if sub.get("confidence") == "auto_confirmed":
                violations.append({
                    "subcategory_text": sub.get("subcategory_text"),
                    "confidence":       sub.get("confidence"),
                })

    status = FAIL if violations else PASS
    return {
        "signal":     "S4_PROTOCOL_SPECIFICITY_CAT10",
        "status":     status,
        "violations": violations,
        "message":    (f"{len(violations)} Cat 10 subcat(s) incorrectly auto_confirmed"
                       if violations
                       else "All Category 10 subcats correctly flagged as review/low_confidence"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 5 – BENCHMARK ALIGNMENT
# ─────────────────────────────────────────────────────────────────────────────

def check_benchmark_alignment(all_subcats: List[Dict]) -> Dict:
    """
    S5 – Benchmark alignment.

    For every auto_confirmed subcategory, the benchmark.segment.rate must be
    >= 0.70.  auto_confirmed subcategories below this rate suggest the
    confidence-assignment threshold logic is broken.

    Args:
        all_subcats : Flattened list of all subcategory dicts

    Returns:
        {
          status: PASS|FAIL,
          violations: [ { category_num, subcategory_text, rate } ],
          message: str
        }
    """
    violations: List[Dict] = []

    for sub in all_subcats:
        if sub.get("confidence") != "auto_confirmed":
            continue
        rate = get_benchmark_rate(sub)
        if rate is None or rate < BENCHMARK_RATE_THRESHOLD:
            violations.append({
                "category_num":     sub.get("_category_num"),
                "subcategory_text": sub.get("subcategory_text"),
                "rate":             rate,
            })

    status = FAIL if violations else PASS
    return {
        "signal":     "S5_BENCHMARK_ALIGNMENT",
        "status":     status,
        "violations": violations,
        "message":    (f"{len(violations)} auto_confirmed subcat(s) below rate threshold {BENCHMARK_RATE_THRESHOLD}"
                       if violations
                       else f"All auto_confirmed subcats have rate >= {BENCHMARK_RATE_THRESHOLD}"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 6 – SUBCATEGORY COUNT SANITY
# ─────────────────────────────────────────────────────────────────────────────

def check_subcat_count_sanity(
    generator_json: Dict,
    benchmark_df: pd.DataFrame,
    study_id: str,
) -> Dict:
    """
    S6 – Subcategory count sanity.

    For each category, compare the generated subcat count to the average
    count seen in training-set studies for the same TA/phase segment.
    If generated count > 3x the segment average, flag as possible hallucination
    explosion.

    If benchmark data is insufficient to compute segment averages, fall back
    to a global average from the benchmark CSV.

    Args:
        generator_json : Parsed generator output JSON
        benchmark_df   : Loaded deviation_subcategories_clean.csv
        study_id       : Study identifier

    Returns:
        {
          status: PASS|WARN,
          per_category: { cat_num: { generated_count, expected_avg, ratio, flagged } },
          warnings: [ { category_num, generated_count, expected_avg, ratio } ],
          message: str
        }
    """
    per_cat: Dict[int, Dict] = {}
    warnings: List[Dict] = []

    # Try to get segment average from benchmark data by category
    avg_by_cat: Dict[int, float] = {}
    if not benchmark_df.empty and "category_num" in benchmark_df.columns:
        # If benchmark has a count column, use it; otherwise count rows per category
        if "subcat_count" in benchmark_df.columns:
            grp = benchmark_df.groupby("category_num")["subcat_count"].mean()
        else:
            grp = benchmark_df.groupby("category_num").size().astype(float)
        for cat_num, avg in grp.items():
            avg_by_cat[int(cat_num)] = float(avg)

    global_avg = (benchmark_df.groupby("category_num").size().mean()
                  if not benchmark_df.empty and "category_num" in benchmark_df.columns
                  else 5.0)

    for cat in generator_json.get("categories", []):
        cat_num = int(cat.get("category_num", 0))
        count   = len(cat.get("subcategories", []))
        avg     = avg_by_cat.get(cat_num, global_avg)
        ratio   = count / avg if avg > 0 else 0.0
        flagged = ratio > COUNT_SANITY_MULTIPLIER and count > 0

        per_cat[cat_num] = {
            "generated_count": count,
            "expected_avg":    round(avg, 1),
            "ratio":           round(ratio, 2),
            "flagged":         flagged,
        }

        if flagged:
            warnings.append({
                "category_num":    cat_num,
                "generated_count": count,
                "expected_avg":    round(avg, 1),
                "ratio":           round(ratio, 2),
                "message":         f"Category {cat_num}: {count} subcats vs avg {avg:.1f} (ratio {ratio:.1f}x)",
            })

    status = WARN if warnings else PASS
    return {
        "signal":       "S6_SUBCAT_COUNT_SANITY",
        "status":       status,
        "per_category": per_cat,
        "warnings":     warnings,
        "message":      (f"{len(warnings)} category/ies exceed {COUNT_SANITY_MULTIPLIER}x average count"
                         if warnings else "Subcategory counts are within expected ranges"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 7 – NONE-IDENTIFIED PLAUSIBILITY
# ─────────────────────────────────────────────────────────────────────────────

def check_none_identified_plausibility(
    generator_json: Dict,
    benchmark_df: pd.DataFrame,
) -> Dict:
    """
    S7 – None-identified plausibility.

    If the generator marks a category as none_identified=True, check whether
    the benchmark data supports that (i.e. historical rate of deviation in
    that category is low).

    Flag for human review if none_identified=True but the historical rate
    for that segment is > 80% (common category marked as empty is suspicious).

    Args:
        generator_json : Parsed generator output JSON
        benchmark_df   : Loaded deviation_subcategories_clean.csv

    Returns:
        {
          status: PASS|WARN,
          flags: [ { category_num, benchmark_rate, message } ],
          message: str
        }
    """
    none_ident_map = get_none_identified_map(generator_json)
    flags: List[Dict] = []

    # Try to get category occurrence rate from benchmark
    cat_rate: Dict[int, float] = {}
    rate_col = next((c for c in ["segment_rate", "rate", "frequency"] if c in benchmark_df.columns), None)
    if rate_col and "category_num" in benchmark_df.columns:
        grp = benchmark_df.groupby("category_num")[rate_col].mean()
        for cat_num, rate in grp.items():
            cat_rate[int(cat_num)] = float(rate)

    for cat_num, is_empty in none_ident_map.items():
        if not is_empty:
            continue    # generator has subcats → not flagging

        bm_rate = cat_rate.get(cat_num)
        if bm_rate is not None and bm_rate > NONE_IDENT_FLAG_RATE:
            flags.append({
                "category_num":   cat_num,
                "benchmark_rate": round(bm_rate, 3),
                "message": (f"Category {cat_num}: generator says empty but historical rate "
                            f"is {bm_rate:.0%} – flag for human review"),
            })

    status = WARN if flags else PASS
    return {
        "signal":  "S7_NONE_IDENTIFIED_PLAUSIBILITY",
        "status":  status,
        "flags":   flags,
        "message": (f"{len(flags)} category/ies flagged as empty despite high historical rate"
                    if flags else "None-identified flags are plausible against benchmark rates"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN REVIEW LIST
# ─────────────────────────────────────────────────────────────────────────────

def build_human_review_list(generator_json: Dict) -> List[Dict]:
    """
    Compile the list of subcategories requiring human review before the
    PIPD form can be finalised.

    Includes all subcategories with confidence in {review, low_confidence}
    AND all Category 10 subcategories regardless of confidence.

    Args:
        generator_json : Parsed generator output JSON

    Returns:
        List of { category_num, subcategory_text, confidence, reason }
    """
    review_list: List[Dict] = []

    for cat in generator_json.get("categories", []):
        cat_num = int(cat.get("category_num", 0))
        for sub in cat.get("subcategories", []):
            confidence = sub.get("confidence", "")
            reason = None

            if cat_num == 10:
                reason = "Category 10 – always requires human confirmation (protocol-specific)"
            elif confidence in {"review", "low_confidence"}:
                reason = f"Confidence level '{confidence}' – generator uncertain"

            if reason:
                review_list.append({
                    "category_num":     cat_num,
                    "subcategory_text": sub.get("subcategory_text"),
                    "confidence":       confidence,
                    "reason":           reason,
                })

    return review_list


# ─────────────────────────────────────────────────────────────────────────────
# OVERALL QUALITY VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def compute_overall_verdict(signals: List[Dict]) -> Dict:
    """
    Compute an overall quality verdict from the 7 proxy signal results.

    Verdict rules (from spec):
      GREEN  – all signals PASS
      AMBER  – 1 or 2 signals WARN, none FAIL
      RED    – any signal FAIL

    Args:
        signals : List of signal result dicts from the 7 check functions

    Returns:
        { verdict: GREEN|AMBER|RED, fail_count, warn_count, pass_count,
          remediation_notes: [str] }
    """
    fails = [s for s in signals if s["status"] == FAIL]
    warns = [s for s in signals if s["status"] == WARN]

    if fails:
        verdict = "RED"
    elif len(warns) <= 2:
        verdict = "AMBER" if warns else "GREEN"
    else:
        verdict = "AMBER"

    notes: List[str] = []
    for s in fails:
        notes.append(f"[FAIL] {s['signal']}: {s['message']}")
    for s in warns:
        notes.append(f"[WARN] {s['signal']}: {s['message']}")

    return {
        "verdict":            verdict,
        "fail_count":         len(fails),
        "warn_count":         len(warns),
        "pass_count":         len(signals) - len(fails) - len(warns),
        "remediation_notes":  notes,
    }


# ─────────────────────────────────────────────────────────────────────────────
# USDM provenance (Scenario 2 + intelligence-style output)
# ─────────────────────────────────────────────────────────────────────────────

def _build_scenario2_usdm_provenance(
    all_subcats: List[Dict],
    study_id: str,
    usdm_json_path: Optional[str],
) -> Dict[str, Any]:
    """
    For each subcategory, resolve a human-readable line in the USDM protocol JSON
    (eligibility text, id, inclusion vs exclusion, etc.). See ``pipd_usdm_provenance``.
    """
    pkg_data = Path(__file__).resolve().parent / "data"
    u_path: Optional[Path] = None
    if usdm_json_path and str(usdm_json_path).strip():
        cand = Path(usdm_json_path.strip())
        if cand.is_file():
            u_path = cand
    if u_path is None:
        from core.pipd_usdm_support import resolve_usdm_protocol_path

        r = resolve_usdm_protocol_path(study_id, pkg_data)
        if r is not None and r.is_file():
            u_path = r
    if u_path is None or not u_path.is_file():
        return {
            "loaded": False,
            "message": "Optional: pass usdm_json_path or set PIPD_USDM_JSON / place protocol JSON under data/.",
        }
    from core.pipd_usdm_provenance import (
        index_usdm_nodes_by_id,
        index_usdm_nodes_by_instance_type,
        resolve_usdm_source_for_subcategory,
    )

    try:
        with open(u_path, encoding="utf-8") as fh:
            root = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"loaded": False, "path": str(u_path), "error": str(exc)}

    by_id = index_usdm_nodes_by_id(root)
    ti = index_usdm_nodes_by_instance_type(root)
    rows: List[Dict[str, Any]] = []
    for s in all_subcats:
        txt = str(s.get("subcategory_text") or "")
        line, meth, eval_eid, eval_etype = resolve_usdm_source_for_subcategory(s, txt, by_id, ti)
        rows.append(
            {
                "category_num":       s.get("_category_num"),
                "subcategory_text":   s.get("subcategory_text"),
                # Generator-provided fields (usdm_entity_id always null for PIPD)
                "gen_usdm_entity_id": s.get("usdm_entity_id"),
                "gen_usdm_entity":    s.get("usdm_entity"),
                # Evaluator-found fields (independent USDM lookup)
                "eval_usdm_entity_id":   eval_eid,
                "eval_usdm_entity_type": eval_etype,
                "confidence":            s.get("confidence"),
                "usdm_protocol_source":  line,
                "usdm_source_method":    meth,
            }
        )
    return {
        "loaded":            True,
        "path":              str(u_path.resolve()),
        "subcategory_count": len(rows),
        "subcategories":     rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EVAL RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario2_eval(
    generator_json_path: str,
    benchmark_csv_path: str,
    study_id: str,
    usdm_json_path: Optional[str] = None,
) -> Dict:
    """
    Orchestrate the full Scenario 2 evaluation for one test study.

    Runs all 7 proxy quality signals and assembles the full results dict.

    Args:
        generator_json_path : Path to {study_id}_PIPD.json
        benchmark_csv_path  : Path to deviation_subcategories_clean.csv
        study_id            : Study identifier
        usdm_json_path      : Optional USDM protocol JSON; if omitted, uses ``PIPD_USDM_JSON`` or auto-discovery under ``data/``

    Returns:
        Comprehensive results dict with signal results, review list, and verdict
    """
    generator_json = load_generator_json(generator_json_path)
    benchmark_df   = load_benchmark_data(benchmark_csv_path)
    all_subcats    = extract_all_subcategories(generator_json)

    # Infer study TA / phase from generator JSON if available
    ta    = generator_json.get("ta", "Unknown")
    phase = generator_json.get("phase", "Unknown")

    # Run 7 proxy signals
    s1 = check_hallucination_signals(all_subcats)
    s2 = check_confidence_distribution(generator_json)
    s3 = check_category_completeness(generator_json)
    s4 = check_protocol_specificity_cat10(generator_json)
    s5 = check_benchmark_alignment(all_subcats)
    s6 = check_subcat_count_sanity(generator_json, benchmark_df, study_id)
    s7 = check_none_identified_plausibility(generator_json, benchmark_df)

    signals = [s1, s2, s3, s4, s5, s6, s7]
    verdict = compute_overall_verdict(signals)
    review_list = build_human_review_list(generator_json)
    usdm_prov = _build_scenario2_usdm_provenance(all_subcats, study_id, usdm_json_path)

    return {
        "study_id":         study_id,
        "ta":               ta,
        "phase":            phase,
        "eval_date":        datetime.now().isoformat(),
        "scenario":         2,
        "signals":          { s["signal"]: s for s in signals },
        "overall_verdict":  verdict,
        "human_review_list": review_list,
        "human_review_count": len(review_list),
        "overall_pass":     verdict["verdict"] in ("GREEN", "AMBER"),
        "go_no_go":         "GO"    if verdict["verdict"] in ("GREEN", "AMBER") else "NO-GO",
        "usdm_provenance":  usdm_prov,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PIPD Eval Framework – Scenario 2 (no ground truth)")
    parser.add_argument("--generator_json",       required=True, help="Path to {study_id}_PIPD.json")
    parser.add_argument("--deviation_benchmarks", required=True, help="Path to deviation_subcategories_clean.csv")
    parser.add_argument("--study_id",             required=True, help="Study identifier")
    parser.add_argument("--output_json",          default=None,  help="Optional path to save results JSON")
    parser.add_argument("--usdm_json",            default=None,  help="Optional USDM protocol JSON path")
    args = parser.parse_args()

    results = run_scenario2_eval(
        args.generator_json,
        args.deviation_benchmarks,
        args.study_id,
        usdm_json_path=args.usdm_json,
    )

    v = results["overall_verdict"]
    colour_map = {"GREEN": "✓ GREEN", "AMBER": "⚠ AMBER", "RED": "✗ RED"}
    print(f"\n{'='*55}")
    print(f"  Scenario 2 Eval │ {args.study_id}")
    print(f"{'='*55}")
    for sig_key, sig in results["signals"].items():
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[sig["status"]]
        print(f"  {icon} {sig_key:<35} {sig['status']}")
    print(f"{'─'*55}")
    print(f"  Overall Verdict : {colour_map.get(v['verdict'], v['verdict'])}")
    print(f"  Human review items: {results['human_review_count']}")
    print(f"{'='*55}\n")

    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"Results saved → {args.output_json}")
