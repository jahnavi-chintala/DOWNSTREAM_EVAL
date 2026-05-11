"""
CMP Scenario 2 evaluator (no study-level ground truth available).

This mode focuses on internal quality/proxy checks, similar to the Scenario 2
approach used in the other product evaluators.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.structure_validator import validate_structure
from utils.miss_explanation import UsdmIndex, _build_index, _load_usdm

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# USDM instance types expected to anchor a CMP generator's reasoning.
_CMP_EXPECTED_ANCHORS = ("Endpoint", "Assessment", "Procedure")


def _all_kris(cmp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key, bucket in (("global_kris", cmp_json.get("global_kris")), ("study_specific_kris", cmp_json.get("study_specific_kris"))):
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if isinstance(item, dict):
                row = dict(item)
                row["_bucket"] = key
                out.append(row)
    return out


def _all_qtls(cmp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = cmp_json.get("qtls")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _signal_s1_structure(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    sv = validate_structure(cmp_json)
    status = FAIL if sv.errors else (WARN if sv.warnings else PASS)
    return {
        "signal_id": "S1",
        "name": "Structure Validity",
        "status": status,
        "error_count": len(sv.errors),
        "warning_count": len(sv.warnings),
        "description": "CMP JSON must satisfy schema/top-level/field structure checks.",
        "sample_errors": sv.errors[:10],
        "sample_warnings": sv.warnings[:10],
    }


def _signal_s2_coverage(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    g = cmp_json.get("global_kris")
    s = cmp_json.get("study_specific_kris")
    q = cmp_json.get("qtls")
    g_n = len(g) if isinstance(g, list) else 0
    s_n = len(s) if isinstance(s, list) else 0
    q_n = len(q) if isinstance(q, list) else 0

    msgs: List[str] = []
    status = PASS
    if g_n < 3:
        status = FAIL
        msgs.append(f"global_kris count={g_n} (<3)")
    if s_n < 1:
        status = FAIL
        msgs.append(f"study_specific_kris count={s_n} (<1)")
    if q_n < 1:
        status = FAIL
        msgs.append(f"qtls count={q_n} (<1)")

    return {
        "signal_id": "S2",
        "name": "Section Coverage",
        "status": status,
        "counts": {"global_kris": g_n, "study_specific_kris": s_n, "qtls": q_n},
        "description": "; ".join(msgs) if msgs else "All required sections have plausible minimum coverage.",
    }


def _signal_s3_threshold_sanity(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    violations: List[Dict[str, Any]] = []
    for kri in _all_kris(cmp_json):
        thr = kri.get("thresholds")
        if not isinstance(thr, dict):
            continue
        mod = ((thr.get("moderate") or {}) if isinstance(thr.get("moderate"), dict) else {}).get("relative_score")
        high = ((thr.get("high") or {}) if isinstance(thr.get("high"), dict) else {}).get("relative_score")
        try:
            mod_v = float(mod) if mod is not None else None
            high_v = float(high) if high is not None else None
        except (TypeError, ValueError):
            violations.append(
                {
                    "kri_id": kri.get("kri_id"),
                    "kri_label": kri.get("kri_label"),
                    "issue": "non_numeric_threshold",
                    "moderate": mod,
                    "high": high,
                }
            )
            continue
        if mod_v is not None and high_v is not None and high_v <= mod_v:
            violations.append(
                {
                    "kri_id": kri.get("kri_id"),
                    "kri_label": kri.get("kri_label"),
                    "issue": "high_not_above_moderate",
                    "moderate": mod_v,
                    "high": high_v,
                }
            )
    return {
        "signal_id": "S3",
        "name": "Threshold Sanity",
        "status": FAIL if violations else PASS,
        "violation_count": len(violations),
        "violations": violations[:50],
        "description": "KRI high thresholds should be numeric and greater than moderate thresholds.",
    }


def _signal_s4_provenance(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    missing: List[Dict[str, Any]] = []
    for kri in _all_kris(cmp_json):
        if not kri.get("active", True):
            continue
        iqmp = str(kri.get("iqmp_risk_id") or "").strip()
        if not iqmp:
            missing.append(
                {
                    "bucket": kri.get("_bucket"),
                    "kri_id": kri.get("kri_id"),
                    "kri_label": kri.get("kri_label"),
                    "issue": "missing_iqmp_risk_id",
                }
            )
    status = WARN if missing else PASS
    return {
        "signal_id": "S4",
        "name": "Protocol Provenance Presence",
        "status": status,
        "missing_count": len(missing),
        "missing": missing[:50],
        "description": "Active KRIs should provide iqmp_risk_id traceability.",
    }


def _signal_s5_confidence(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    low = 0
    total = 0
    review_items: List[Dict[str, Any]] = []
    for kri in _all_kris(cmp_json):
        tier = str(kri.get("confidence_tier") or "").strip().lower()
        if not tier:
            continue
        total += 1
        if tier in {"low", "review", "low_confidence"}:
            low += 1
            review_items.append(
                {
                    "type": "kri",
                    "bucket": kri.get("_bucket"),
                    "label": kri.get("kri_label"),
                    "confidence_tier": tier,
                }
            )
    for qtl in _all_qtls(cmp_json):
        tier = str(qtl.get("confidence_tier") or "").strip().lower()
        if not tier:
            continue
        total += 1
        if tier in {"low", "review", "low_confidence"}:
            low += 1
            review_items.append(
                {
                    "type": "qtl",
                    "label": qtl.get("name"),
                    "confidence_tier": tier,
                }
            )
    low_rate = (low / total) if total else 0.0
    status = WARN if low_rate > 0.50 else PASS
    return {
        "signal_id": "S5",
        "name": "Confidence Distribution",
        "status": status,
        "total_with_tier": total,
        "low_or_review_count": low,
        "low_or_review_rate": round(low_rate, 4),
        "review_items": review_items[:100],
        "description": "Warn when >50% of confidence-tagged rows are low/review.",
    }


def _signal_s6_uniqueness(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    labels: Dict[str, int] = {}
    for kri in _all_kris(cmp_json):
        lbl = str(kri.get("kri_label") or "").strip().lower()
        if not lbl:
            continue
        labels[lbl] = labels.get(lbl, 0) + 1
    dups = sorted([k for k, v in labels.items() if v > 1])
    status = WARN if dups else PASS
    return {
        "signal_id": "S6",
        "name": "KRI Label Uniqueness",
        "status": status,
        "duplicate_count": len(dups),
        "duplicate_labels": dups[:50],
        "description": "Duplicate labels across global/study KRIs increase downstream ambiguity.",
    }


def _signal_s7_analysis_frequency(cmp_json: Dict[str, Any]) -> Dict[str, Any]:
    af = cmp_json.get("analysis_frequency")
    status = PASS
    desc = "analysis_frequency present."
    if not isinstance(af, dict) or not af:
        status = WARN
        desc = "analysis_frequency missing or empty."
    return {
        "signal_id": "S7",
        "name": "Analysis Frequency Completeness",
        "status": status,
        "description": desc,
    }


def _collect_usdm_refs(cmp_json: Dict[str, Any]) -> List[str]:
    """Walk the CMP JSON and collect any usdm_id-like references.

    Most CMP generators emit inferred KRIs that do not carry direct USDM
    node ids, but some include ``source_usdm_ids``/``usdm_ref``/``usdm_id``.
    We return all non-empty string ids seen so S8 can verify them against
    the uploaded USDM index.
    """
    found: List[str] = []
    stack: List[Any] = [cmp_json]
    keys = {"usdm_id", "usdm_ref", "source_usdm_id", "usdm_entity_id"}
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    if isinstance(v, str) and v.strip() and v.strip().lower() not in {"n/a", "none", "null"}:
                        found.append(v.strip())
                    elif isinstance(v, list):
                        for s in v:
                            if isinstance(s, str) and s.strip():
                                found.append(s.strip())
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(obj, list):
            stack.extend(obj)
    return found


def _signal_s8_usdm_trace(cmp_json: Dict[str, Any],
                          usdm_path: Optional[str]) -> Dict[str, Any]:
    """Real USDM traceability signal (replaces proxy-only provenance check).

    Decision rules:
    * No USDM uploaded → WARN (cannot verify).
    * USDM uploaded but none of the expected anchor instanceTypes are
      present → FAIL (the protocol lacks the classes CMP would derive
      KRIs from; any generator output is at best invented).
    * Any explicit ``usdm_id`` reference that does not resolve in the
      index → FAIL (hallucinated trace).
    * Otherwise → PASS.
    """
    if not usdm_path:
        return {
            "signal_id": "S8",
            "name": "USDM Traceability",
            "status": WARN,
            "description": "USDM JSON not uploaded; traceability cannot be verified.",
            "usdm_path": None,
            "anchors_present": {},
            "unresolved_ids": [],
        }

    root = _load_usdm(usdm_path)
    idx: UsdmIndex = _build_index(root) if root is not None else UsdmIndex()
    if idx.empty:
        return {
            "signal_id": "S8",
            "name": "USDM Traceability",
            "status": FAIL,
            "description": "USDM JSON present but contained no indexable nodes.",
            "usdm_path": str(usdm_path),
            "anchors_present": {},
            "unresolved_ids": [],
        }

    anchors = {a: (a in idx.by_type) for a in _CMP_EXPECTED_ANCHORS}
    any_anchor = any(anchors.values())

    refs = _collect_usdm_refs(cmp_json)
    unresolved = [r for r in refs if r not in idx.by_id]

    if not any_anchor:
        status = FAIL
        desc = (
            "None of the expected USDM anchor classes "
            f"({', '.join(_CMP_EXPECTED_ANCHORS)}) exist in the protocol; "
            "CMP output cannot be traced."
        )
    elif unresolved:
        status = FAIL
        desc = (
            f"{len(unresolved)} usdm_id reference(s) in the CMP JSON do not "
            "exist in the uploaded USDM protocol."
        )
    else:
        status = PASS
        desc = (
            f"USDM anchors present: {[a for a, p in anchors.items() if p]}."
            + (f" {len(refs)} usdm_id reference(s) all resolved."
               if refs else " No direct usdm_id refs on the CMP (inferred generator).")
        )

    return {
        "signal_id": "S8",
        "name": "USDM Traceability",
        "status": status,
        "description": desc,
        "usdm_path": str(usdm_path),
        "anchors_present": anchors,
        "total_refs": len(refs),
        "unresolved_count": len(unresolved),
        "unresolved_ids": unresolved[:25],
        "usdm_types_sample": idx.types_available()[:12],
    }


def _overall_verdict(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    fails = [s["signal_id"] for s in signals if s["status"] == FAIL]
    warns = [s["signal_id"] for s in signals if s["status"] == WARN]
    if fails:
        verdict = "RED"
    elif warns:
        verdict = "AMBER"
    else:
        verdict = "GREEN"
    return {
        "verdict": verdict,
        "fail_signals": fails,
        "warn_signals": warns,
        "pass_count": len([s for s in signals if s["status"] == PASS]),
        "total_signals": len(signals),
    }


def run_scenario2_eval(cmp_json: Dict[str, Any],
                       study_id: str,
                       *,
                       usdm_path: Optional[str] = None) -> Dict[str, Any]:
    """Run CMP Scenario 2 signals.

    ``usdm_path`` is optional. If not provided we fall back to the
    ``CMP_USDM_JSON_PATH`` environment variable (set by the upload routes)
    so existing callers keep working unchanged.
    """
    resolved_usdm = usdm_path or os.environ.get("CMP_USDM_JSON_PATH")
    signals = [
        _signal_s1_structure(cmp_json),
        _signal_s2_coverage(cmp_json),
        _signal_s3_threshold_sanity(cmp_json),
        _signal_s4_provenance(cmp_json),
        _signal_s5_confidence(cmp_json),
        _signal_s6_uniqueness(cmp_json),
        _signal_s7_analysis_frequency(cmp_json),
        _signal_s8_usdm_trace(cmp_json, resolved_usdm),
    ]
    verdict = _overall_verdict(signals)
    review_list = []
    for s in signals:
        if s["signal_id"] == "S5":
            review_list.extend(s.get("review_items") or [])
        elif s["status"] in (FAIL, WARN):
            review_list.append({"signal_id": s["signal_id"], "reason": s.get("description", "")})
    return {
        "scenario": 2,
        "study_id": study_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict["verdict"],
        "summary_metrics": {
            "overall_pass": verdict["verdict"] in ("GREEN", "AMBER"),
            "go_no_go": "GO" if verdict["verdict"] in ("GREEN", "AMBER") else "NO-GO",
            "signal_pass": verdict["pass_count"],
            "signal_warn": len(verdict["warn_signals"]),
            "signal_fail": len(verdict["fail_signals"]),
        },
        "signals": {s["signal_id"]: s for s in signals},
        "verdict_detail": verdict,
        "review_list": review_list[:200],
        "review_list_count": len(review_list),
    }

