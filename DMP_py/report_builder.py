"""Assemble DMP eval report dict, improvement actions, pass/fail gates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


def load_eval_config(path: str | Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def strip_private(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_private(x) for x in obj]
    return obj


def _count_hallucinations(
    s5_rows: List[Dict[str, Any]],
    s6_rows: List[Dict[str, Any]],
    s8_rows: List[Dict[str, Any]],
) -> int:
    n = 0
    for r in s5_rows:
        gt_empty = str(r.get("ground_truth_name") or "").strip().lower() in ("", "(empty)", "null", "none")
        gen_has = str(r.get("generated_name") or "").strip() != ""
        if gt_empty and gen_has:
            n += 1
            continue
        if r.get("_source_tag_valid") is False:
            n += 1
    for r in s6_rows:
        if r.get("match_status") == "extra":
            n += 1
            continue
        if r.get("_source_tag_valid") is False:
            n += 1
    for r in s8_rows:
        if r.get("match_status") == "extra":
            n += 1
            continue
        if r.get("_source_tag_valid") is False:
            n += 1
    return n


def _improvement_actions(
    s5: List[Dict[str, Any]],
    s6: List[Dict[str, Any]],
    s8: List[Dict[str, Any]],
    s11: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    endpoint_like_terms = (
        "progression-free survival",
        "objective response",
        "duration of response",
        "overall survival",
        "endpoint",
        "eq-5d",
        "pharmacokinetic",
        "pk",
    )
    for r in s8:
        if r.get("match_status") == "miss":
            out.append(
                {
                    "priority": "HIGH",
                    "section": "s8_critical_data",
                    "type": "miss",
                    "action": f"Ground-truth module not generated: {r.get('ground_truth_module')}",
                    "fix_location": "dmp_generator.py::build_s8_modules (CRF page inventory mapping)",
                }
            )
        elif r.get("match_status") == "near_miss":
            out.append(
                {
                    "priority": "LOW",
                    "section": "s8_critical_data",
                    "type": "near_miss",
                    "action": f"Module wording mismatch: GT {r.get('ground_truth_module')} vs Gen {r.get('generated_module')}",
                    "fix_location": "semantic_matcher.py::normalize_text + S8 module synonym map",
                }
            )
        elif r.get("match_status") == "extra":
            gen_mod = str(r.get("generated_module") or "")
            if any(t in gen_mod.lower() for t in endpoint_like_terms):
                out.append(
                    {
                        "priority": "HIGH",
                        "section": "s8_critical_data",
                        "type": "hallucination",
                        "action": (
                            f"S8 extra looks like endpoint text, not CRF module: '{gen_mod}'. "
                            "Generator is likely sourcing protocol endpoints into module slots."
                        ),
                        "fix_location": "dmp_generator.py::build_s8_modules (switch source from USDM endpoints to CRF modules)",
                    }
                )
            else:
                out.append(
                    {
                        "priority": "MEDIUM",
                        "section": "s8_critical_data",
                        "type": "hallucination",
                        "action": f"Extra S8 module not in GT: '{gen_mod}'.",
                        "fix_location": "dmp_generator.py::build_s8_modules (module whitelist / ontology)",
                    }
                )
    for r in s5:
        if r.get("match_status") == "mismatch":
            gt_name = str(r.get("ground_truth_name") or "")
            gen_name = str(r.get("generated_name") or "")
            if gt_name.strip().lower() in ("", "(empty)", "null", "none") and gen_name.strip():
                out.append(
                    {
                        "priority": "HIGH",
                        "section": "s5_systems",
                        "type": "hallucination",
                        "action": (
                            f"GT is null for {r.get('system_type')}, but generator produced '{gen_name}'. "
                            "Should remain null/empty when no benchmark signal exists."
                        ),
                        "fix_location": "dmp_generator.py::populate_s5_systems (disable cross-study fallback for null GT slots)",
                    }
                )
                continue
            out.append(
                {
                    "priority": "MEDIUM",
                    "section": "s5_systems",
                    "type": "mismatch",
                    "action": f"System {r.get('system_type')}: GT {r.get('ground_truth_name')} vs Gen {r.get('generated_name')}",
                    "fix_location": "dmp_generator.py::populate_s5_systems (field-level benchmark binding)",
                }
            )
    for r in s6:
        if r.get("match_status") == "miss":
            gt_vendor = str(r.get("ground_truth_vendor") or "")
            if gt_vendor.strip().upper() == "TBD":
                out.append(
                    {
                        "priority": "HIGH",
                        "section": "s6_vendors",
                        "type": "miss",
                        "action": "Pending vendor row missing while GT expects 'TBD'.",
                        "fix_location": "dmp_generator.py::populate_s6_vendors (vendor_pending -> output 'TBD')",
                    }
                )
                continue
            out.append(
                {
                    "priority": "HIGH",
                    "section": "s6_vendors",
                    "type": "miss",
                    "action": f"Vendor row missing: {r.get('ground_truth_vendor')}",
                    "fix_location": "dmp_generator.py::populate_s6_vendors (SDS row projection)",
                }
            )
        elif r.get("match_status") == "mismatch":
            gt_vendor = str(r.get("ground_truth_vendor") or "")
            if gt_vendor.strip().upper() == "TBD":
                out.append(
                    {
                        "priority": "HIGH",
                        "section": "s6_vendors",
                        "type": "mismatch",
                        "action": (
                            f"GT vendor is TBD, but generator emitted '{r.get('generated_vendor')}'. "
                            "Pending vendor state should not infer concrete vendor."
                        ),
                        "fix_location": "dmp_generator.py::populate_s6_vendors (block inferred vendors when pending)",
                    }
                )
            else:
                out.append(
                    {
                        "priority": "MEDIUM",
                        "section": "s6_vendors",
                        "type": "mismatch",
                        "action": f"Vendor mismatch: GT {r.get('ground_truth_vendor')} vs Gen {r.get('generated_vendor')}",
                        "fix_location": "dmp_generator.py::populate_s6_vendors (vendor resolution)",
                    }
                )
        elif r.get("match_status") == "extra":
            out.append(
                {
                    "priority": "HIGH",
                    "section": "s6_vendors",
                    "type": "hallucination",
                    "action": f"Extra vendor row not in GT: '{r.get('generated_vendor')}'.",
                    "fix_location": "dmp_generator.py::populate_s6_vendors (filter unsupported inferred rows)",
                }
            )
    for r in s11:
        if r.get("match_status") != "correct":
            label = str(r.get("subsection_label") or "")
            gt_flag = str(r.get("ground_truth_flag") or "")
            gen_flag = str(r.get("generated_flag") or "")
            if "central_lab" in label.lower():
                fix_loc = "dmp_reconciliation_benchmarks.yaml::central_lab rule (skip when vendor is TBD)"
            else:
                fix_loc = "dmp_reconciliation_benchmarks.yaml::rule set"
            out.append(
                {
                    "priority": "HIGH",
                    "section": "s11_reconciliation",
                    "type": "flag_mismatch",
                    "action": f"{label}: GT {gt_flag} vs Gen {gen_flag}",
                    "fix_location": fix_loc,
                }
            )
    return out[:50]


def build_report(
    study_id: str,
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    cfg: Dict[str, Any],
    cfg_path: Path,
    s5_rows: List[Dict[str, Any]],
    s5_meta: Dict[str, Any],
    s6_rows: List[Dict[str, Any]],
    s6_meta: Dict[str, Any],
    s8_rows: List[Dict[str, Any]],
    s8_meta: Dict[str, Any],
    s11_rows: List[Dict[str, Any]],
    s11_meta: Dict[str, Any],
    generator_version: str = "unknown",
    *,
    sds_available: bool = True,
) -> Dict[str, Any]:
    sections_cfg = cfg.get("sections") or {}
    scoring = cfg.get("scoring") or {}
    mt = scoring.get("metric_targets") or {}

    w5 = float(sections_cfg.get("s5_systems", {}).get("weight", 0.25))
    w6 = float(sections_cfg.get("s6_vendors", {}).get("weight", 0.25))
    w8 = float(sections_cfg.get("s8_critical_data", {}).get("weight", 0.40))
    w11 = float(sections_cfg.get("s11_reconciliation", {}).get("weight", 0.10))

    sc5 = float(s5_meta.get("score", 0))
    sc6 = float(s6_meta.get("score", 0))
    sc8 = float(s8_meta.get("score", 0))
    sc11 = float(s11_meta.get("score", 0))

    doc_score = sc5 * w5 + sc6 * w6 + sc8 * w8 + sc11 * w11
    thr = float(scoring.get("document_pass_threshold", 75))
    tgt = float(scoring.get("document_target", 80))

    m1 = sc5 / 100.0
    m2 = sc6 / 100.0
    m3 = sc8 / 100.0
    m4 = sc11 / 100.0

    t1 = float(mt.get("m1_s5_system_accuracy", 0.9))
    t2 = float(mt.get("m2_s6_vendor_recall", 0.9))
    t3 = float(mt.get("m3_s8_module_recall", 0.85))
    t4 = float(mt.get("m4_s11_reconciliation_accuracy", 0.95))
    hall_tgt = int(mt.get("m4_hallucination", 0))

    hall_n = _count_hallucinations(s5_rows, s6_rows, s8_rows)

    p1 = m1 >= t1 - 1e-9
    p2 = m2 >= t2 - 1e-9
    p3 = m3 >= t3 - 1e-9
    p4 = m4 >= t4 - 1e-9
    ph = hall_n <= hall_tgt

    overall = p1 and p2 and p3 and p4 and ph and doc_score >= thr - 1e-9

    from dmp_data import generation_fallback_used

    eval_metadata = {
        "study_id": study_id,
        "compound": str(dmp.get("compound") or ""),
        "indication": str(dmp.get("indication") or ""),
        "protocol_title": str(dmp.get("S2_purpose_scope", {}).get("study_context", {}).get("indication", "") if isinstance(dmp.get("S2_purpose_scope"), dict) else "") or str(dmp.get("indication") or ""),
        "therapeutic_area": str(dmp.get("therapeutic_area") or ""),
        "phase": str(dmp.get("phase") or ""),
        "yaml_bucket": str(dmp.get("yaml_bucket") or ""),
        "eval_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "config_version": str(scoring.get("config_version", cfg.get("config_version", "1.0"))),
        "config_file": cfg_path.name,
        "generator_version": generator_version,
        "ground_truth_sources": [
            "dmp_ground_truth_clean.json",
            "sds_non_crf_ground_truth_clean.csv (when fallback_used=false)",
        ],
        "fallback_used": generation_fallback_used(dmp),
        "fallback_mode": ("RCC_template" if generation_fallback_used(dmp) else None),
        "sds_available": sds_available,
        "asb_available": False,
        "supplemental_enriched": False,
        "out_of_scope_sections_excluded": [
            "S1", "S3", "S4", "S7", "S9", "S10",
            "S11.1", "S11.2", "S11.3", "S11.5", "S11.6", "S11.7", "S11.8",
            "S12", "S13", "S15", "S16",
        ],
        "out_of_scope_reason": "Boilerplate / non-scored sections per DMP eval framework",
    }

    if not eval_metadata.get("protocol_title"):
        ctx = dmp.get("S2_purpose_scope", {})
        if isinstance(ctx, dict):
            sc = ctx.get("study_context") or {}
            if isinstance(sc, dict):
                eval_metadata["protocol_title"] = str(
                    sc.get("indication") or sc.get("compound") or dmp.get("compound") or ""
                )

    section_scores = {
        "s5_systems": {
            "metric": "M1",
            "weight": w5,
            "score": round(sc5, 2),
            "weighted_contribution": round(sc5 * w5, 2),
            "generated_count": s5_meta.get("generated_count"),
            "ground_truth_count": s5_meta.get("ground_truth_count"),
            "matched": s5_meta.get("matched"),
            "misses": [strip_private(x) for x in s5_rows if x.get("match_status") == "mismatch"],
            "hallucinations": [],
        },
        "s8_critical_data": {
            "metric": "M3",
            "weight": w8,
            "score": round(sc8, 2),
            "weighted_contribution": round(sc8 * w8, 2),
            "generated_count": s8_meta.get("generated_count"),
            "ground_truth_count": s8_meta.get("ground_truth_count"),
            "matched": s8_meta.get("matched"),
            "misses": [strip_private(x) for x in s8_rows if x.get("match_status") == "miss"],
            "hallucinations": [strip_private(x) for x in s8_rows if x.get("match_status") == "extra"],
        },
        "s6_vendors": {
            "metric": "M2",
            "weight": w6,
            "score": round(sc6, 2),
            "weighted_contribution": round(sc6 * w6, 2),
            "generated_count": s6_meta.get("generated_count"),
            "ground_truth_count": s6_meta.get("ground_truth_count"),
            "matched": s6_meta.get("matched"),
            "misses": [strip_private(x) for x in s6_rows if x.get("match_status") == "miss"],
            "hallucinations": [strip_private(x) for x in s6_rows if x.get("match_status") == "extra"],
        },
        "s11_reconciliation": {
            "metric": "M4",
            "weight": w11,
            "score": round(sc11, 2),
            "weighted_contribution": round(sc11 * w11, 2),
            "total_subsections": s11_meta.get("total_subsections"),
            "correct_flags": s11_meta.get("correct_flags"),
            "misclassifications": [strip_private(x) for x in s11_rows if x.get("match_status") != "correct"],
        },
    }

    summary_metrics = {
        "m1_s5_system_accuracy": round(m1, 4),
        "m1_target": t1,
        "m1_pass": p1,
        "m2_s6_vendor_recall": round(m2, 4),
        "m2_target": t2,
        "m2_pass": p2,
        "m3_s8_module_recall": round(m3, 4),
        "m3_target": t3,
        "m3_pass": p3,
        "m4_reconciliation_accuracy": round(m4, 4),
        "m4_target": t4,
        "m4_pass": p4,
        "m4_hallucinations": hall_n,
        "m4_hallucination_target": hall_tgt,
        "m4_hallucination_pass": ph,
        "overall_pass": overall,
        "go_no_go": "GO" if overall else "NO-GO",
    }

    report = {
        "eval_metadata": eval_metadata,
        "document_score": round(doc_score, 2),
        "document_pass": overall,
        "document_target": tgt,
        "document_pass_threshold": thr,
        "summary_metrics": summary_metrics,
        "section_scores": section_scores,
        "s5_systems": strip_private(s5_rows),
        "s8_critical_data": strip_private(s8_rows),
        "s6_vendors": strip_private(s6_rows),
        "s11_reconciliation": strip_private(s11_rows),
        "improvement_actions": _improvement_actions(s5_rows, s6_rows, s8_rows, s11_rows),
    }
    return report
