"""
DMP Scenario 2 evaluator (no study-level ground truth available).

Performs proxy/internal-consistency checks and returns a traffic-light verdict.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.miss_explanation import UsdmIndex, _build_index, _load_usdm

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# USDM instance types expected to anchor a DMP generator's reasoning.
_DMP_EXPECTED_ANCHORS = ("Assessment", "DataAcquisition", "ScheduleOfActivities",
                         "Procedure")


def _s5_systems(dmp: Dict[str, Any]) -> Dict[str, Any]:
    s5 = dmp.get("S5_systems_tools") or dmp.get("S5_systems") or {}
    if not isinstance(s5, dict):
        return {}
    inner = s5.get("S5_2_other_systems") or {}
    return inner if isinstance(inner, dict) else {}


def _s6_vendors(dmp: Dict[str, Any]) -> List[Dict[str, Any]]:
    s6 = dmp.get("S6_data_flow") or {}
    if not isinstance(s6, dict):
        return []
    s62 = s6.get("S6_2_esource_edata") or {}
    if not isinstance(s62, dict):
        return []
    rows = s62.get("vendors") or []
    return [r for r in rows if isinstance(r, dict)]


def _s8_modules(dmp: Dict[str, Any]) -> List[Dict[str, Any]]:
    s8 = dmp.get("S8_critical_data") or {}
    if not isinstance(s8, dict):
        return []
    mods = s8.get("modules") or []
    return [m for m in mods if isinstance(m, dict)]


def _s11_sections(dmp: Dict[str, Any]) -> Dict[str, Any]:
    s11 = dmp.get("S11_data_review_validation") or {}
    if not isinstance(s11, dict):
        return {}
    r4 = s11.get("S11_4_reconciliation") or {}
    if not isinstance(r4, dict):
        return {}
    sec = r4.get("sections") or {}
    return sec if isinstance(sec, dict) else {}


def _signal_s1_required_sections(dmp: Dict[str, Any]) -> Dict[str, Any]:
    missing: List[str] = []
    checks = {
        "S5_2_other_systems": bool(_s5_systems(dmp)),
        "S6_2_esource_edata.vendors": len(_s6_vendors(dmp)) > 0,
        "S8_critical_data.modules": len(_s8_modules(dmp)) > 0,
        "S11_4_reconciliation.sections": bool(_s11_sections(dmp)),
    }
    for k, ok in checks.items():
        if not ok:
            missing.append(k)
    return {
        "signal_id": "S1",
        "name": "Required Section Presence",
        "status": FAIL if missing else PASS,
        "missing": missing,
        "description": "Major scored sections must be present in generated DMP.",
    }


def _signal_s2_source_tags(dmp: Dict[str, Any]) -> Dict[str, Any]:
    missing: List[Dict[str, Any]] = []
    for k, v in _s5_systems(dmp).items():
        if not isinstance(v, dict):
            continue
        if not str(v.get("source") or "").strip():
            missing.append({"section": "S5", "field": k})
    for i, row in enumerate(_s6_vendors(dmp)):
        if not str(row.get("source") or "").strip():
            missing.append({"section": "S6", "row": i, "vendor": row.get("vendor")})
    for i, row in enumerate(_s8_modules(dmp)):
        if not str(row.get("source") or "").strip():
            missing.append({"section": "S8", "row": i, "module": row.get("data_module")})
    status = FAIL if missing else PASS
    return {
        "signal_id": "S2",
        "name": "Source Tag Provenance",
        "status": status,
        "missing_count": len(missing),
        "missing": missing[:100],
        "description": "Rows should include source/provenance tags for traceability.",
    }


def _signal_s3_count_sanity(dmp: Dict[str, Any]) -> Dict[str, Any]:
    s6_n = len(_s6_vendors(dmp))
    s8_n = len(_s8_modules(dmp))
    warns: List[str] = []
    status = PASS
    if s8_n == 0:
        status = FAIL
        warns.append("S8 module count is zero")
    elif s8_n > 30:
        status = WARN
        warns.append(f"S8 module count unusually high ({s8_n})")
    if s6_n > 40:
        status = WARN if status == PASS else status
        warns.append(f"S6 vendor count unusually high ({s6_n})")
    return {
        "signal_id": "S3",
        "name": "Section Count Sanity",
        "status": status,
        "counts": {"s6_vendors": s6_n, "s8_modules": s8_n},
        "description": "; ".join(warns) if warns else "Row counts are within plausible range.",
    }


def _signal_s4_confidence_distribution(dmp: Dict[str, Any]) -> Dict[str, Any]:
    low = 0
    total = 0
    review_items: List[Dict[str, Any]] = []
    for sys_name, sys in _s5_systems(dmp).items():
        if not isinstance(sys, dict):
            continue
        c = str(sys.get("confidence") or "").lower()
        if not c:
            continue
        total += 1
        if "review" in c or "low" in c:
            low += 1
            review_items.append({"section": f"S5 system: {sys_name}", "confidence": c})
    for mod in _s8_modules(dmp):
        c = str(mod.get("confidence") or "").lower()
        if not c:
            continue
        total += 1
        if "review" in c or "low" in c:
            low += 1
            review_items.append({
                "section": f"S8 module: {str(mod.get('module_name') or mod.get('name') or '?')[:60]}",
                "confidence": c,
            })
    rate = (low / total) if total else 0.0
    status = WARN if rate > 0.40 else PASS
    return {
        "signal_id": "S4",
        "name": "Confidence Distribution",
        "status": status,
        "total_with_confidence": total,
        "low_or_review_count": low,
        "low_or_review_rate": round(rate, 4),
        "review_items": review_items,
        "description": "Warn when >40% rows are low/review confidence.",
    }


def _signal_s5_reconciliation_integrity(dmp: Dict[str, Any]) -> Dict[str, Any]:
    bad: List[Dict[str, Any]] = []
    for k, sec in _s11_sections(dmp).items():
        if not isinstance(sec, dict):
            bad.append({"section_key": k, "issue": "not_object"})
            continue
        if "applicable" not in sec:
            bad.append({"section_key": k, "issue": "missing_applicable"})
        rule = str(sec.get("rule") or "").strip()
        if not rule:
            bad.append({"section_key": k, "issue": "missing_rule"})
    status = FAIL if bad else PASS
    return {
        "signal_id": "S5",
        "name": "S11 Reconciliation Integrity",
        "status": status,
        "issue_count": len(bad),
        "issues": bad[:100],
        "description": "S11 reconciliation sections should include applicable flags and rule text.",
    }


def _signal_s6_vendor_row_quality(dmp: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    for i, row in enumerate(_s6_vendors(dmp)):
        vendor = str(row.get("vendor") or "").strip()
        dtype = str(row.get("data_type") or "").strip()
        if not vendor and not dtype:
            issues.append({"row": i, "issue": "empty_vendor_and_data_type"})
    status = WARN if issues else PASS
    return {
        "signal_id": "S6",
        "name": "Vendor Row Quality",
        "status": status,
        "issue_count": len(issues),
        "issues": issues[:50],
        "description": "Vendor rows should include at least vendor or data_type.",
    }


def _signal_s7_module_uniqueness(dmp: Dict[str, Any]) -> Dict[str, Any]:
    seen: Dict[str, int] = {}
    for mod in _s8_modules(dmp):
        key = str(mod.get("data_module") or "").strip().lower()
        if not key:
            continue
        seen[key] = seen.get(key, 0) + 1
    dups = sorted([k for k, v in seen.items() if v > 1])
    status = WARN if dups else PASS
    return {
        "signal_id": "S7",
        "name": "S8 Module Uniqueness",
        "status": status,
        "duplicate_count": len(dups),
        "duplicate_modules": dups[:50],
        "description": "Duplicate module names indicate likely repeated extraction artifacts.",
    }


def _overall(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    fail = [s["signal_id"] for s in signals if s["status"] == FAIL]
    warn = [s["signal_id"] for s in signals if s["status"] == WARN]
    verdict = "RED" if fail else ("AMBER" if warn else "GREEN")
    return {
        "verdict": verdict,
        "fail_signals": fail,
        "warn_signals": warn,
        "pass_count": len([s for s in signals if s["status"] == PASS]),
        "total_signals": len(signals),
    }


def _collect_usdm_refs(dmp: Dict[str, Any]) -> List[str]:
    """Scan the DMP JSON for any ``usdm_id`` / ``source_usdm_*`` references."""
    found: List[str] = []
    stack: List[Any] = [dmp]
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


def _signal_s8_usdm_trace(dmp: Dict[str, Any],
                          usdm_path: Optional[str]) -> Dict[str, Any]:
    """Real USDM traceability signal for DMP.

    DMP is mostly INFERRED so we do not require verbatim node matches.
    We only require that the *anchor classes* the DMP reasons from exist
    in the protocol. Any explicit usdm_id references (if the generator
    emits them) must still resolve.
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

    anchors = {a: (a in idx.by_type) for a in _DMP_EXPECTED_ANCHORS}
    any_anchor = any(anchors.values())
    refs = _collect_usdm_refs(dmp)
    unresolved = [r for r in refs if r not in idx.by_id]

    if not any_anchor:
        status = FAIL
        desc = (
            "None of the expected USDM anchor classes "
            f"({', '.join(_DMP_EXPECTED_ANCHORS)}) exist in the protocol; "
            "DMP output cannot be traced."
        )
    elif unresolved:
        status = FAIL
        desc = (
            f"{len(unresolved)} usdm_id reference(s) in the DMP JSON do not "
            "exist in the uploaded USDM protocol."
        )
    else:
        status = PASS
        desc = (
            f"USDM anchors present: {[a for a, p in anchors.items() if p]}."
            + (f" {len(refs)} usdm_id reference(s) all resolved."
               if refs else " No direct usdm_id refs on the DMP (inferred generator).")
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


def run_scenario2_eval(dmp: Dict[str, Any],
                       study_id: str,
                       *,
                       usdm_path: Optional[str] = None) -> Dict[str, Any]:
    resolved_usdm = usdm_path or os.environ.get("DMP_USDM_JSON_PATH")
    signals = [
        _signal_s1_required_sections(dmp),
        _signal_s2_source_tags(dmp),
        _signal_s3_count_sanity(dmp),
        _signal_s4_confidence_distribution(dmp),
        _signal_s5_reconciliation_integrity(dmp),
        _signal_s6_vendor_row_quality(dmp),
        _signal_s7_module_uniqueness(dmp),
        _signal_s8_usdm_trace(dmp, resolved_usdm),
    ]
    ov = _overall(signals)
    review: List[Dict[str, Any]] = []
    if "S4" in ov["warn_signals"]:
        review.append({"signal_id": "S4", "reason": "High low/review confidence rate."})
    for sid in ov["fail_signals"]:
        review.append({"signal_id": sid, "reason": "Critical scenario 2 check failed."})
    return {
        "scenario": 2,
        "study_id": study_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": ov["verdict"],
        "summary_metrics": {
            "overall_pass": ov["verdict"] in ("GREEN", "AMBER"),
            "go_no_go": "GO" if ov["verdict"] in ("GREEN", "AMBER") else "NO-GO",
            "signal_pass": ov["pass_count"],
            "signal_warn": len(ov["warn_signals"]),
            "signal_fail": len(ov["fail_signals"]),
        },
        "signals": {s["signal_id"]: s for s in signals},
        "verdict_detail": ov,
        "review_list": review,
        "review_list_count": len(review),
    }

