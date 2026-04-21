"""
eval_scenario2.py
-----------------
D1 Risk Profile Eval Framework – Scenario 2: NO ground truth available (test studies).

Used on May 18-19 for Pfizer's 3 live test protocols. No ground truth exists.
7 proxy quality signals measure internal consistency and plausibility of the
generated Risk Profile JSON.

Signals:
  S1 – Hallucination check          (FAIL: any risk with empty usdm_drivers or null benchmark_source)
  S2 – RPN confidence distribution  (WARN: > 40% LOW confidence)
  S3 – Risk count sanity            (FAIL: 0 risks or > 6 risks)
  S4 – USDM traceability            (FAIL: any risk with PARTIAL or NONE traceability)
  S5 – Critical factor completeness (WARN: fewer than expected factors for TA/Phase)
  S6 – Placeholder ID presence      (FAIL: any non SR-PLACEHOLDER-XXX risk_id)
  S7 – RPN formula integrity        (FAIL: impact × likelihood × detectability ≠ rpn)

Overall verdict: GREEN (all pass) / AMBER (1-2 warns, zero fails) / RED (any fail)

Usage (standalone):
    python3 eval_scenario2.py \\
        --generator_json {study_id}_RiskProfile.json \\
        --study_id {study_id} \\
        --output_json {study_id}_s2_results.json
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Signal thresholds from design spec §4.1
RISK_COUNT_FLOOR: int = 3
RISK_COUNT_CEILING: int = 6
LOW_CONFIDENCE_THRESHOLD: float = 0.40   # > 40% LOW confidence = WARN
PLACEHOLDER_ID_PATTERN = re.compile(r"^SR-PLACEHOLDER-\d{3}$")

# Minimum expected critical factors by TA (heuristic, based on verify set averages)
# Used by S5. If the TA is not in this map, minimum defaults to 3.
MIN_EXPECTED_FACTORS_BY_TA: Dict[str, int] = {
    "IMMUNOLOGY":       5,
    "ONCOLOGY":         4,
    "VACCINES":         4,
    "INFECTIOUS":       4,
}
MIN_EXPECTED_FACTORS_DEFAULT: int = 3


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_generator_json(json_path: str) -> Dict[str, Any]:
    """
    Load and validate the D1 generator output JSON for a single study.

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
                f"Generator JSON missing required key '{required_key}'. File: {json_path}"
            )

    return data


def get_all_risks(generator_json: Dict) -> List[Dict]:
    """
    Extract the flat list of study risks from the generator JSON.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        List of risk dicts. Empty list if 'risks' key is absent or empty.
    """
    return generator_json.get("risks", [])


def get_ta(generator_json: Dict) -> str:
    """
    Extract the therapeutic area from the generator JSON study_overview block.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Therapeutic area string (upper case), or 'UNKNOWN' if not present.
    """
    return generator_json.get("study_overview", {}).get("therapeutic_area", "UNKNOWN").upper()


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S1 – HALLUCINATION CHECK
# ─────────────────────────────────────────────────────────────────────────────

def check_hallucination_signals(generator_json: Dict) -> Dict:
    """
    Signal S1 – Hallucination Check.

    Every risk must have:
      (a) usdm_drivers: non-empty list
      (b) intelligence.benchmark_source: non-null, non-empty string

    Every associated_cause must have:
      (c) usdm_trigger with entity + signal (both non-empty)

    Any violation → status = FAIL. Zero violations → status = PASS.
    S1 FAIL alone triggers RED overall verdict.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|FAIL), violation_count, violations}
    """
    violations = []
    risks = get_all_risks(generator_json)

    for i, risk in enumerate(risks):
        risk_name = risk.get("risk_name", f"Risk[{i}]")
        prefix = f"risks[{i}] ({risk_name!r})"

        # (a) usdm_drivers must be a non-empty list
        usdm_drivers = risk.get("usdm_drivers")
        if not usdm_drivers or not isinstance(usdm_drivers, list) or len(usdm_drivers) == 0:
            violations.append({
                "field_path": f"{prefix}.usdm_drivers",
                "value": usdm_drivers,
                "reason": "usdm_drivers is empty or missing — no protocol traceability",
            })

        # (b) intelligence.benchmark_source must be non-null and non-empty
        intelligence = risk.get("intelligence", {}) or {}
        benchmark_source = intelligence.get("benchmark_source")
        if not benchmark_source or str(benchmark_source).strip() in ("", "null", "None", "--"):
            violations.append({
                "field_path": f"{prefix}.intelligence.benchmark_source",
                "value": benchmark_source,
                "reason": "benchmark_source is null or empty — risk not traceable to YAML benchmark",
            })

        # (c) every associated_cause must have usdm_trigger.entity and .signal
        for j, cause in enumerate(risk.get("associated_causes", [])):
            cause_text = cause.get("cause", f"cause[{j}]")
            trigger = cause.get("usdm_trigger")
            if not trigger:
                violations.append({
                    "field_path": f"{prefix}.associated_causes[{j}] ({cause_text!r}).usdm_trigger",
                    "value": None,
                    "reason": "usdm_trigger missing — cause has no protocol traceability",
                })
            else:
                for field in ("entity", "signal"):
                    if not trigger.get(field):
                        violations.append({
                            "field_path": f"{prefix}.associated_causes[{j}].usdm_trigger.{field}",
                            "value": trigger.get(field),
                            "reason": f"usdm_trigger.{field} must be non-empty",
                        })

    return {
        "signal_id": "S1",
        "name": "Hallucination Check",
        "status": "FAIL" if violations else "PASS",
        "violation_count": len(violations),
        "violations": violations,
        "description": "Every risk must have non-empty usdm_drivers and a valid benchmark_source. Every cause must have a usdm_trigger.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S2 – RPN CONFIDENCE DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def check_confidence_distribution(generator_json: Dict) -> Dict:
    """
    Signal S2 – RPN Confidence Distribution.

    Counts HIGH / MEDIUM / LOW confidence across all risks.
    If more than 40% of risks have LOW confidence → WARN. The generator is uncertain
    about this TA, and human review is essential before submission.
    Status is WARN (not FAIL) because a high LOW rate is a quality signal, not a
    disqualifying error.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|WARN), counts, percentages}
    """
    risks = get_all_risks(generator_json)
    if not risks:
        return {
            "signal_id": "S2",
            "name": "RPN Confidence Distribution",
            "status": "WARN",
            "counts": {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0},
            "percentages": {},
            "total_risks": 0,
            "low_confidence_rate": None,
            "description": "No risks found — cannot evaluate confidence distribution",
        }

    counts: Dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    low_confidence_risks = []

    for risk in risks:
        intelligence = risk.get("intelligence", {}) or {}
        confidence = str(intelligence.get("rpn_confidence", "UNKNOWN")).upper()
        if confidence not in counts:
            confidence = "UNKNOWN"
        counts[confidence] += 1
        if confidence == "LOW":
            low_confidence_risks.append(risk.get("risk_name", "?"))

    total = len(risks)
    low_rate = counts["LOW"] / total
    percentages = {k: round(v / total, 4) for k, v in counts.items()}

    return {
        "signal_id": "S2",
        "name": "RPN Confidence Distribution",
        "status": "WARN" if low_rate > LOW_CONFIDENCE_THRESHOLD else "PASS",
        "counts": counts,
        "percentages": percentages,
        "total_risks": total,
        "low_confidence_rate": round(low_rate, 4),
        "low_confidence_risks": low_confidence_risks,
        "threshold": LOW_CONFIDENCE_THRESHOLD,
        "description": (
            f"LOW confidence rate: {low_rate:.1%} "
            f"({'exceeds' if low_rate > LOW_CONFIDENCE_THRESHOLD else 'within'} "
            f"{LOW_CONFIDENCE_THRESHOLD:.0%} threshold)."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S3 – RISK COUNT SANITY
# ─────────────────────────────────────────────────────────────────────────────

def check_risk_count_sanity(generator_json: Dict) -> Dict:
    """
    Signal S3 – Risk Count Sanity.

    The generator must produce between 3 and 6 study risks (inclusive).
    0 risks = generator completely failed. > 6 risks = generator violated its ceiling.
    Both → FAIL.

    Also checks vendor_risks, study_site_risks, and other_domain_risks — these
    should be empty arrays for most studies. Non-empty → WARN.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|WARN|FAIL), risk_count}
    """
    risks = get_all_risks(generator_json)
    risk_count = len(risks)

    # Core risk count check
    if risk_count < RISK_COUNT_FLOOR or risk_count > RISK_COUNT_CEILING:
        status = "FAIL"
        description = (
            f"Generated {risk_count} study risks. "
            f"Expected {RISK_COUNT_FLOOR}–{RISK_COUNT_CEILING}. "
            f"{'Generator ceiling violated.' if risk_count > RISK_COUNT_CEILING else 'Generator produced no risks.'}"
        )
    else:
        status = "PASS"
        description = f"Generated {risk_count} study risks. Within expected range {RISK_COUNT_FLOOR}–{RISK_COUNT_CEILING}."

    # Additional domain checks
    additional_warnings = []
    for domain_key in ("vendor_risks", "study_site_risks", "other_domain_risks"):
        domain_risks = generator_json.get(domain_key, [])
        if domain_risks:
            additional_warnings.append(
                f"{domain_key}: {len(domain_risks)} unexpected risks found"
            )
            if status == "PASS":
                status = "WARN"

    return {
        "signal_id": "S3",
        "name": "Risk Count Sanity",
        "status": status,
        "study_risk_count": risk_count,
        "floor": RISK_COUNT_FLOOR,
        "ceiling": RISK_COUNT_CEILING,
        "additional_domain_warnings": additional_warnings,
        "description": description,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S4 – USDM TRACEABILITY
# ─────────────────────────────────────────────────────────────────────────────

def check_usdm_traceability(generator_json: Dict) -> Dict:
    """
    Signal S4 – USDM Traceability.

    Every risk must have intelligence.usdm_traceability = 'FULL'. Any risk with
    PARTIAL or NONE traceability means the generator could not trace its output
    back to the protocol USDM — a disqualifying deficiency.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|FAIL), violations}
    """
    risks = get_all_risks(generator_json)
    violations = []

    for i, risk in enumerate(risks):
        risk_name = risk.get("risk_name", f"Risk[{i}]")
        intelligence = risk.get("intelligence", {}) or {}
        traceability = str(intelligence.get("usdm_traceability", "NONE")).upper()
        if traceability != "FULL":
            violations.append({
                "risk_name": risk_name,
                "usdm_traceability": traceability,
                "reason": f"Expected FULL, got {traceability!r}",
            })

    return {
        "signal_id": "S4",
        "name": "USDM Traceability",
        "status": "FAIL" if violations else "PASS",
        "total_risks": len(risks),
        "violations": violations,
        "description": (
            f"{len(violations)} risk(s) with non-FULL traceability."
            if violations else
            "All risks have FULL USDM traceability."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S5 – CRITICAL FACTOR COMPLETENESS
# ─────────────────────────────────────────────────────────────────────────────

def check_critical_factor_completeness(generator_json: Dict) -> Dict:
    """
    Signal S5 – Critical Factor Completeness.

    Without ground truth, checks that the number of generated critical factors
    meets the minimum expected for the study's TA. If fewer than expected → WARN.
    This detects incomplete factor_selection buckets in the YAML.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|WARN), factor_count, min_expected}
    """
    ta = get_ta(generator_json)
    min_expected = MIN_EXPECTED_FACTORS_BY_TA.get(ta, MIN_EXPECTED_FACTORS_DEFAULT)

    cf = generator_json.get("critical_factors", [])
    factor_count = len(cf)
    factor_names = [f.get("factor_name", "?") for f in cf]

    # Check each factor has usdm_sources
    missing_sources = [
        f.get("factor_name", f"Factor[{i}]")
        for i, f in enumerate(cf)
        if not f.get("usdm_sources") or (
            not f.get("usdm_sources", {}).get("critical_data")
            and not f.get("usdm_sources", {}).get("critical_process")
        )
    ]

    status = "PASS"
    warnings = []

    if factor_count < min_expected:
        status = "WARN"
        warnings.append(
            f"Only {factor_count} critical factors generated; expected >= {min_expected} for {ta}."
        )

    if missing_sources:
        if status == "PASS":
            status = "WARN"
        warnings.append(
            f"Factors missing usdm_sources: {missing_sources}"
        )

    return {
        "signal_id": "S5",
        "name": "Critical Factor Completeness",
        "status": status,
        "factor_count": factor_count,
        "min_expected": min_expected,
        "ta": ta,
        "factor_names": factor_names,
        "factors_missing_usdm_sources": missing_sources,
        "warnings": warnings,
        "description": (
            "; ".join(warnings) if warnings else
            f"{factor_count} critical factors generated for {ta} (min expected: {min_expected})."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S6 – PLACEHOLDER ID PRESENCE
# ─────────────────────────────────────────────────────────────────────────────

def check_placeholder_ids(generator_json: Dict) -> Dict:
    """
    Signal S6 – Placeholder ID Presence.

    All risk_ids must match the SR-PLACEHOLDER-XXX pattern (3 digits). This confirms
    the generator has NOT fabricated real IRMS IDs. A real SR-XXXXX ID appearing in
    the output means the generator is inventing IDs that conflict with the IRMS —
    a disqualifying submission error.

    Also checks control_ids must be SCT-PLACEHOLDER-XXX format.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|FAIL), violations}
    """
    SCT_PATTERN = re.compile(r"^SCT-PLACEHOLDER-\d{3}$")
    violations = []
    risks = get_all_risks(generator_json)

    for i, risk in enumerate(risks):
        risk_name = risk.get("risk_name", f"Risk[{i}]")
        risk_id = risk.get("risk_id", "")

        if not PLACEHOLDER_ID_PATTERN.match(str(risk_id)):
            violations.append({
                "risk_name": risk_name,
                "field": "risk_id",
                "value": risk_id,
                "reason": f"Expected SR-PLACEHOLDER-XXX format, got '{risk_id}'",
            })

        # Check control IDs
        for j, control in enumerate(risk.get("controls", [])):
            control_id = control.get("control_id", "")
            if not SCT_PATTERN.match(str(control_id)):
                violations.append({
                    "risk_name": risk_name,
                    "field": f"controls[{j}].control_id",
                    "value": control_id,
                    "reason": f"Expected SCT-PLACEHOLDER-XXX format, got '{control_id}'",
                })

    return {
        "signal_id": "S6",
        "name": "Placeholder ID Presence",
        "status": "FAIL" if violations else "PASS",
        "total_risks": len(risks),
        "violations": violations,
        "description": (
            f"{len(violations)} non-placeholder ID(s) found — potential fabricated IRMS IDs."
            if violations else
            "All risk_ids and control_ids use correct SR-PLACEHOLDER / SCT-PLACEHOLDER format."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL S7 – RPN FORMULA INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────

def check_rpn_formula_integrity(generator_json: Dict) -> Dict:
    """
    Signal S7 – RPN Formula Integrity.

    For every risk: impact × likelihood × detectability must equal rpn exactly.
    Arithmetic mismatches mean the generator has a calculation error. This is a
    disqualifying failure — incorrect RPNs directly affect risk prioritisation.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        Signal dict: {signal_id, name, status (PASS|FAIL), mismatches}
    """
    risks = get_all_risks(generator_json)
    mismatches = []

    for i, risk in enumerate(risks):
        risk_name = risk.get("risk_name", f"Risk[{i}]")
        try:
            impact = int(risk.get("impact", 0))
            likelihood = int(risk.get("likelihood", 0))
            detectability = int(risk.get("detectability", 0))
            rpn = int(risk.get("rpn", -1))
        except (TypeError, ValueError):
            mismatches.append({
                "risk_name": risk_name,
                "impact": risk.get("impact"),
                "likelihood": risk.get("likelihood"),
                "detectability": risk.get("detectability"),
                "reported_rpn": risk.get("rpn"),
                "computed_rpn": None,
                "reason": "One or more RPN component fields is non-numeric",
            })
            continue

        computed = impact * likelihood * detectability
        if computed != rpn:
            mismatches.append({
                "risk_name": risk_name,
                "impact": impact,
                "likelihood": likelihood,
                "detectability": detectability,
                "reported_rpn": rpn,
                "computed_rpn": computed,
                "reason": f"{impact} × {likelihood} × {detectability} = {computed}, but rpn = {rpn}",
            })

    return {
        "signal_id": "S7",
        "name": "RPN Formula Integrity",
        "status": "FAIL" if mismatches else "PASS",
        "total_risks": len(risks),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "description": (
            f"{len(mismatches)} RPN formula mismatch(es) — impact × likelihood × detectability ≠ rpn."
            if mismatches else
            "All RPNs satisfy: impact × likelihood × detectability = rpn."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HUMAN REVIEW LIST
# ─────────────────────────────────────────────────────────────────────────────

def build_human_review_list(generator_json: Dict) -> List[Dict]:
    """
    Compile the list of risks requiring human clinical review before submission.

    Includes all risks where intelligence.rpn_confidence = 'LOW'. These cannot
    be submitted without a qualified reviewer confirming the risk selection and
    RPN values are appropriate for the specific protocol.

    Args:
        generator_json: Parsed generator output dict

    Returns:
        List of dicts: {risk_name, rpn, rpn_confidence, benchmark_source, review_reason}
        Sorted by risk_name for consistent ordering.
    """
    review_list = []
    risks = get_all_risks(generator_json)

    for risk in risks:
        intelligence = risk.get("intelligence", {}) or {}
        confidence = str(intelligence.get("rpn_confidence", "UNKNOWN")).upper()

        if confidence == "LOW":
            review_list.append({
                "risk_name": risk.get("risk_name", "?"),
                "rpn": risk.get("rpn"),
                "rpn_confidence": confidence,
                "benchmark_source": intelligence.get("benchmark_source"),
                "benchmark_occurrence_rate": intelligence.get("benchmark_occurrence_rate"),
                "review_reason": "LOW rpn_confidence — clinical reviewer must confirm RPN and risk selection",
            })

    return sorted(review_list, key=lambda r: r.get("risk_name", ""))


# ─────────────────────────────────────────────────────────────────────────────
# OVERALL VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def compute_overall_verdict(signals: List[Dict]) -> Dict:
    """
    Aggregate individual signal statuses into a traffic-light overall verdict.

    Rules:
      RED   – any signal status = FAIL
      AMBER – one or two signals = WARN, zero FAILs
      GREEN – all signals = PASS

    Args:
        signals: List of signal dicts from all check_* functions

    Returns:
        Dict: {verdict (GREEN|AMBER|RED), fail_signals, warn_signals, pass_count}
    """
    fail_signals = [s for s in signals if s["status"] == "FAIL"]
    warn_signals = [s for s in signals if s["status"] == "WARN"]
    pass_signals = [s for s in signals if s["status"] == "PASS"]

    if fail_signals:
        verdict = "RED"
    elif warn_signals:
        verdict = "AMBER"
    else:
        verdict = "GREEN"

    return {
        "verdict": verdict,
        "fail_signals": [s["signal_id"] for s in fail_signals],
        "warn_signals": [s["signal_id"] for s in warn_signals],
        "pass_count": len(pass_signals),
        "total_signals": len(signals),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario2_eval(
    generator_json_path: str,
    study_id: str,
) -> Dict:
    """
    Orchestrate the full Scenario 2 evaluation for one study.

    Runs all 7 proxy signals, computes the overall verdict, builds the human review
    list, and attaches metadata. This is the primary entry point called by run_eval.py
    and api.py for live test protocols.

    Args:
        generator_json_path: Path to {study_id}_RiskProfile.json
        study_id: Study identifier for labelling

    Returns:
        Fully populated result dict including all signal results and overall verdict.
    """
    timestamp = datetime.utcnow().isoformat() + "Z"
    generator_json = load_generator_json(generator_json_path)

    signals = [
        check_hallucination_signals(generator_json),
        check_confidence_distribution(generator_json),
        check_risk_count_sanity(generator_json),
        check_usdm_traceability(generator_json),
        check_critical_factor_completeness(generator_json),
        check_placeholder_ids(generator_json),
        check_rpn_formula_integrity(generator_json),
    ]

    verdict_block = compute_overall_verdict(signals)
    review_list = build_human_review_list(generator_json)

    metadata = generator_json.get("metadata", {})
    study_overview = generator_json.get("study_overview", {})

    return {
        "study_id": study_id,
        "scenario": 2,
        "timestamp": timestamp,
        "verdict": verdict_block["verdict"],
        "generator_version": metadata.get("generator_version", "unknown"),
        "ta": study_overview.get("therapeutic_area", "UNKNOWN"),
        "phase": study_overview.get("development_phase", "UNKNOWN"),
        "signals": {s["signal_id"]: s for s in signals},
        "verdict_detail": verdict_block,
        "review_list": review_list,
        "review_list_count": len(review_list),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="D1 Risk Profile – Scenario 2 Eval (no ground truth)"
    )
    parser.add_argument("--generator_json", required=True,
                        help="Path to {study_id}_RiskProfile.json")
    parser.add_argument("--study_id", required=True,
                        help="Study identifier, e.g. PTEST001")
    parser.add_argument("--output_json", default=None,
                        help="Optional path to write JSON result (default: stdout only)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    print(f"\n{'='*60}")
    print(f"D1 Risk Profile Eval – Scenario 2 (No Ground Truth)")
    print(f"Study: {args.study_id}")
    print(f"{'='*60}\n")

    results = run_scenario2_eval(
        generator_json_path=args.generator_json,
        study_id=args.study_id,
    )

    verdict = results["verdict"]
    print(f"VERDICT: {verdict}\n")
    print(f"{'Signal':<8} {'Name':<38} {'Status'}")
    print("-" * 60)
    for sid, sig in results["signals"].items():
        print(f"  {sid:<6} {sig['name']:<38} {sig['status']}")

    if results["review_list"]:
        print(f"\nHuman Review Required ({len(results['review_list'])} risks):")
        for r in results["review_list"]:
            print(f"  {r['risk_name']} (RPN {r['rpn']}, confidence: {r['rpn_confidence']})")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        print(f"\nResults written to: {out_path}")
