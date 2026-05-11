"""
report_generator.py — CMP Eval Report Generator
Produces structured JSON eval report + printable console summary.
Canonical keys align with reference_specs/cmp_eval_B7981027.json; Word/YAML consume the same object.
"""

import json
import os
import glob
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─── Export helpers (canonical / reference-shaped) ─────────────────────────────

def _export_kri_attributes(attrs: dict) -> dict:
    out: Dict[str, Any] = {}
    for key in (
        "kri_label",
        "moderate_threshold",
        "high_threshold",
        "iqmp_risk_id",
        "confidence_tier",
        "weight_field",
        "forms_variables",
        "logic_summary",
        "corrective_action",
    ):
        if key not in attrs:
            continue
        raw = dict(attrs[key])
        if key == "kri_label":
            out[key] = {
                "score": raw.get("score"),
                "match_type": raw.get("match_type"),
                "generated": raw.get("generated"),
                "ground_truth": raw.get("ground_truth"),
            }
        elif key in ("moderate_threshold", "high_threshold"):
            out[key] = {
                "score": raw.get("score"),
                "match_type": "numeric_tolerance",
                "generated": raw.get("generated"),
                "ground_truth": raw.get("ground_truth"),
            }
        elif key == "iqmp_risk_id":
            out[key] = {
                "score": raw.get("score"),
                "match_type": "boolean",
                "generated": raw.get("generated"),
                "ground_truth": raw.get("ground_truth"),
                "valid": float(raw.get("score") or 0) >= 1.0,
                "is_hallucination": raw.get("is_hallucination"),
            }
        else:
            out[key] = {k: v for k, v in raw.items() if k != "weight"}
    return out


def _export_kri_section_rows(section_data: dict) -> list:
    rows: List[dict] = []
    for m in section_data.get("matched_kris", []):
        attrs = m.get("attribute_scores", {})
        lbl = attrs.get("kri_label", {})
        rows.append(
            {
                "ground_truth_label": m.get("gt_label"),
                "generated_label": m.get("generated_label"),
                "kri_score": m.get("kri_score"),
                "match_status": lbl.get("match_type", "matched"),
                "is_hallucination": m.get("is_hallucination", False),
                "attributes": _export_kri_attributes(attrs),
            }
        )
    for miss in section_data.get("missed_kris", []):
        rows.append(
            {
                "ground_truth_label": miss.get("gt_label"),
                "generated_label": None,
                "kri_score": 0.0,
                "match_status": "miss",
                "note": "Not generated or not matched to ground truth.",
            }
        )
    return rows


def _export_qtl_rows(qtls_block: dict) -> list:
    rows: List[dict] = []
    for q in qtls_block.get("matched_qtls", []):
        rows.append(
            {
                "ground_truth_name": q.get("gt_name"),
                "generated_name": q.get("generated_name"),
                "qtl_score": q.get("qtl_score"),
                "match_status": q.get("name_match_type", "matched"),
                "name_score": q.get("name_score"),
                "expectation_score": q.get("expectation_score"),
                "tolerance_score": q.get("tolerance_score"),
            }
        )
    for miss in qtls_block.get("missed_qtls", []):
        if miss.get("reason") == "not_generated":
            rows.append(
                {
                    "ground_truth_name": miss.get("gt_name"),
                    "generated_name": None,
                    "qtl_score": 0.0,
                    "match_status": "miss",
                    "note": "QTL not generated.",
                }
            )
        else:
            rows.append(
                {
                    "ground_truth_name": "",
                    "generated_name": miss.get("generated_name"),
                    "qtl_score": 0.0,
                    "match_status": "extra",
                    "note": str(miss.get("reason", "")),
                }
            )
    return rows


def _section_scores_block(
    section_results: dict,
    document_score_result: dict,
    config: dict,
) -> dict:
    sec_cfg = config.get("sections", {})
    contrib = document_score_result.get("section_contributions", {})
    out: Dict[str, Any] = {}

    mapping = [
        ("global_kris", "global_kris", "kri_count_gen", "kri_count_gt", "matched_count"),
        ("study_specific_kris", "study_specific_kris", "kri_count_gen", "kri_count_gt", "matched_count"),
        ("qtls", "qtls", "qtl_count_gen", "qtl_count_gt", "matched_count"),
    ]
    for json_key, sr_key, gen_k, gt_k, mkey in mapping:
        cfg = sec_cfg.get(json_key, {})
        sr = section_results.get(sr_key, {})
        c = contrib.get(json_key, {})
        matched_val = sr.get(mkey, 0)
        if json_key == "qtls":
            matched_val = sr.get("gt_matched_count", matched_val)

        # Build misses / hallucinations lists for reference shape compliance
        if json_key in ("global_kris", "study_specific_kris"):
            misses = [
                str(m.get("gt_label") or "")
                for m in sr.get("missed_kris", [])
            ]
            hallucinations = [
                str(m.get("generated_label") or m.get("gt_label") or "")
                for m in sr.get("matched_kris", [])
                if m.get("is_hallucination")
            ]
        elif json_key == "qtls":
            misses = [
                str(m.get("gt_name") or "")
                for m in sr.get("missed_qtls", [])
                if m.get("reason") == "not_generated"
            ]
            hallucinations = [
                str(m.get("generated_name") or "")
                for m in sr.get("missed_qtls", [])
                if m.get("reason") != "not_generated"
            ]
        else:
            misses = []
            hallucinations = []

        out[json_key] = {
            "weight": float(cfg.get("weight", 0.0)),
            "score": round(float(sr.get("section_score", 0.0)), 1),
            "weighted_contribution": round(float(c.get("weighted", 0.0)), 2),
            "generated_count": int(sr.get(gen_k, 0)),
            "ground_truth_count": int(sr.get(gt_k, 0)),
            "matched": int(matched_val),
            "misses": misses,
            "hallucinations": hallucinations,
        }

    meta_cfg = sec_cfg.get("section_metadata", {})
    meta_sr = section_results.get("metadata", {})
    meta_c = contrib.get("section_metadata", {})
    out["section_metadata"] = {
        "weight": float(meta_cfg.get("weight", 0.05)),
        "score": round(float(meta_sr.get("section_score", 0.0)), 1),
        "weighted_contribution": round(float(meta_c.get("weighted", 0.0)), 2),
        "note": meta_sr.get("details"),
    }
    return out


def _summary_metrics(metrics: dict, study_id: str, doc_pass: bool) -> dict:
    m1, m2, m3, m4 = metrics.get("m1"), metrics.get("m2"), metrics.get("m3"), metrics.get("m4")
    sid_key = f"m1_kri_recall_{study_id.lower()}"
    m1_score = m1.get("score") if isinstance(m1, dict) else None
    return {
        "m1_kri_recall": m1_score,            # canonical stable key for all studies
        "m1_kri_recall_b7981027": m1_score,   # legacy compatibility key (avoid null in non-B798 runs)
        sid_key: m1_score,                     # study-specific alias for B7981027 compatibility
        "m1_target": m1.get("target") if isinstance(m1, dict) else None,
        "m1_pass": m1.get("passed") if isinstance(m1, dict) else None,
        "m2_threshold_accuracy": m2.get("score") if isinstance(m2, dict) else None,
        "m2_target": m2.get("target") if isinstance(m2, dict) else None,
        "m2_pass": m2.get("passed") if isinstance(m2, dict) else None,
        "m3_qtl_recall": m3.get("score") if isinstance(m3, dict) else None,
        "m3_target": m3.get("target") if isinstance(m3, dict) else None,
        "m3_pass": m3.get("passed") if isinstance(m3, dict) else None,
        "m4_hallucinations": m4.get("score") if isinstance(m4, dict) else None,
        "m4_target": m4.get("target") if isinstance(m4, dict) else None,
        "m4_pass": m4.get("passed") if isinstance(m4, dict) else None,
        "overall_pass": doc_pass,
        "go_no_go": "GO" if doc_pass else "NO-GO",
    }


def _infer_ta_from_study_id(study_id: str) -> str:
    sid = str(study_id or "").strip().lower()
    if sid.startswith("b79"):
        return "i_and_i"
    if sid.startswith(("c107", "c232", "c422", "c489")):
        return "oncology"
    if sid.startswith(("c365", "c367", "c459", "c460", "c509")):
        return "vaccines"
    return "unknown"


def _extract_usdm_title(study_id: str) -> Optional[str]:
    """Best-effort Study title lookup from USDM JSON bundled with the protocol."""
    env_usdm = (os.environ.get("CMP_USDM_JSON_PATH") or "").strip()
    if env_usdm and Path(env_usdm).is_file():
        try:
            with open(env_usdm, "r", encoding="utf-8") as fh:
                usdm = json.load(fh)
            for key in ("title", "studyTitle", "name"):
                val = usdm.get(key) if isinstance(usdm, dict) else None
                if isinstance(val, str) and val.strip():
                    return val.strip()
            study_block = usdm.get("Study") if isinstance(usdm, dict) else None
            if isinstance(study_block, dict):
                for key in ("title", "studyTitle", "name"):
                    val = study_block.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            pass
    base = Path(__file__).resolve().parent.parent.parent
    bundle_dir = base / "protocol_eval_hub" / "protocol_bundles" / str(study_id)
    if not bundle_dir.is_dir():
        return None
    candidates = sorted(glob.glob(str(bundle_dir / f"USDM_{study_id}*.json")))
    if not candidates:
        return None
    try:
        with open(candidates[0], "r", encoding="utf-8") as fh:
            usdm = json.load(fh)
    except Exception:
        return None
    for key in ("title", "studyTitle", "name"):
        val = usdm.get(key) if isinstance(usdm, dict) else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    study_block = usdm.get("Study") if isinstance(usdm, dict) else None
    if isinstance(study_block, dict):
        for key in ("title", "studyTitle", "name"):
            val = study_block.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _is_toc_blob(text: str) -> bool:
    s = str(text or "")
    return (
        len(s) > 120
        and ("..." in s or "LIST OF TABLES" in s.upper())
        and bool(re.search(r"\b\d+\.\d+\b", s))
    )


# ─── Report Builder ───────────────────────────────────────────────────────────

def build_eval_report(
    study_id: str,
    cmp_json: dict,
    structure_result,
    section_results: dict,
    metrics: dict,
    document_score_result: dict,
    config: dict,
    generator_version: str = "unknown",
    study_metadata_row: Optional[dict] = None,
    artifact_paths: Optional[dict] = None,
) -> dict:
    """Canonical eval report for JSON / YAML / Word."""
    artifact_paths = artifact_paths or {}
    eval_dt = str(date.today())
    signals = cmp_json.get("signals_detected", {}) or {}
    ta_raw = (
        cmp_json.get("therapeutic_area")
        or signals.get("therapeutic_area")
        or signals.get("ta")
        or _infer_ta_from_study_id(study_id)
    )
    ta = str(ta_raw or "unknown").strip().lower()
    phase = cmp_json.get("phase", cmp_json.get("signals_detected", {}).get("phase", "unknown"))
    meta = cmp_json.get("metadata") or {}
    sm = study_metadata_row or {}
    protocol_title = (
        meta.get("protocol_title")
        or meta.get("title")
        or cmp_json.get("protocol_title")
        or cmp_json.get("title")
        or (cmp_json.get("Study", {}) or {}).get("title")
        or _extract_usdm_title(study_id)
        or f"CMP {study_id}"
    )
    if _is_toc_blob(protocol_title):
        protocol_title = _extract_usdm_title(study_id) or f"CMP {study_id}"

    gen_ver = str(generator_version)
    if gen_ver and not gen_ver.lower().startswith("v"):
        gen_ver = f"v{gen_ver}"

    cfg_p = artifact_paths.get("config", "")
    kri_p = artifact_paths.get("kri_gt", "")
    qtl_p = artifact_paths.get("qtl_gt", "")
    sm_p = artifact_paths.get("study_meta", "")

    rel_form = (config.get("reference_artifacts") or {}).get("cmp_form_docx")
    cmp_form_meta: Dict[str, Any] = {}
    if rel_form and cfg_p:
        form_abs = Path(cfg_p).resolve().parent / str(rel_form).strip()
        cmp_form_meta["cmp_form_reference"] = form_abs.name
        cmp_form_meta["cmp_form_resolved_path"] = str(form_abs.resolve())
        cmp_form_meta["cmp_form_present"] = form_abs.is_file()
    elif rel_form:
        cmp_form_meta["cmp_form_reference"] = Path(str(rel_form)).name
        cmp_form_meta["cmp_form_resolved_path"] = None
        cmp_form_meta["cmp_form_present"] = False

    eval_metadata: Dict[str, Any] = {
        "study_id": study_id,
        "protocol_title": protocol_title,
        "therapeutic_area": ta,
        "phase": phase,
        "eval_date": eval_dt,
        "config_version": str(config.get("scoring", {}).get("config_version", "1.0")),
        "config_file": Path(cfg_p).name if cfg_p else "cmp_eval_config.yaml",
        "generator_version": gen_ver,
        "ground_truth_sources": [
            Path(kri_p).name if kri_p else "cmp_kri_ground_truth.csv",
            Path(qtl_p).name if qtl_p else "cmp_qtl_ground_truth.csv",
        ],
        "study_metadata_csv": Path(sm_p).name if sm_p else None,
        "out_of_scope_sections_excluded": list(
            config.get("scoring", {}).get("out_of_scope_exclusions", [])
        ),
        **cmp_form_meta,
    }

    doc_score = float(document_score_result.get("document_score", 0.0))
    doc_pass = bool(document_score_result.get("passed"))
    thresh = int(document_score_result.get("pass_threshold", 75))
    tgt = int(document_score_result.get("target", 80))

    metrics_ui = {
        "M1_kri_recall": metrics.get("m1"),
        "M2_threshold_accuracy": metrics.get("m2"),
        "M3_qtl_recall": metrics.get("m3"),
        "M4_hallucinations": metrics.get("m4"),
    }

    report: Dict[str, Any] = {
        "eval_metadata": eval_metadata,
        "document_score": round(doc_score, 1),
        "document_pass": doc_pass,
        "document_target": tgt,
        "document_pass_threshold": thresh,
        "document_score_breakdown": {
            "pre_structure_score": document_score_result.get("pre_structure_score"),
            "structure_factor": document_score_result.get("structure_factor"),
        },
        "structure_validation": structure_result.summary(),
        "summary_metrics": _summary_metrics(metrics, study_id, doc_pass),
        "section_scores": _section_scores_block(section_results, document_score_result, config),
        "global_kris": _export_kri_section_rows(section_results.get("global_kris", {})),
        "study_specific_kris": _export_kri_section_rows(section_results.get("study_specific_kris", {})),
        "qtls": _export_qtl_rows(section_results.get("qtls", {})),
        "improvement_actions": _derive_improvement_actions(section_results, metrics, structure_result),
        "metrics_detail": {
            "m1": metrics.get("m1"),
            "m2": metrics.get("m2"),
            "m3": metrics.get("m3"),
            "m4": metrics.get("m4"),
        },
        "kri_counts": {
            "global_generated": len(cmp_json.get("global_kris", []) or []),
            "global_gt": section_results.get("global_kris", {}).get("kri_count_gt", 0),
            "ss_generated": len(cmp_json.get("study_specific_kris", []) or []),
            "ss_gt": section_results.get("study_specific_kris", {}).get("kri_count_gt", 0),
            "qtls_generated": len(cmp_json.get("qtls", []) or []),
            "qtls_gt": section_results.get("qtls", {}).get("qtl_count_gt", 0),
        },
    }

    # Aliases for CLI / CSV / legacy tools
    report["eval_date"] = eval_dt
    report["study_id"] = study_id
    report["therapeutic_area"] = ta
    report["phase"] = phase
    report["generator_version"] = generator_version
    report["config_version"] = eval_metadata["config_version"]
    report["document_passed"] = doc_pass
    report["pass_threshold"] = thresh
    report["target"] = tgt
    report["metrics"] = metrics_ui
    report["section_scorecard"] = _legacy_section_scorecard(
        section_results, document_score_result, config
    )
    report["kri_detail"] = {
        "global_kris": _legacy_kri_detail_flat(section_results.get("global_kris", {})),
        "study_specific_kris": _legacy_kri_detail_flat(section_results.get("study_specific_kris", {})),
        "qtls": section_results.get("qtls", {}).get("matched_qtls", []),
    }

    return report


def _legacy_section_scorecard(section_results, document_score_result, config):
    sec_cfg = config.get("sections", {})
    contrib = document_score_result.get("section_contributions", {})
    return {
        "global_kris": _build_section_card(
            sec_cfg.get("global_kris", {}).get("name", "Global Standard KRIs"),
            float(sec_cfg.get("global_kris", {}).get("weight", 0.35)),
            section_results.get("global_kris", {}),
            contrib.get("global_kris", {}),
        ),
        "study_specific_kris": _build_section_card(
            sec_cfg.get("study_specific_kris", {}).get("name", "Study-Specific KRIs"),
            float(sec_cfg.get("study_specific_kris", {}).get("weight", 0.40)),
            section_results.get("study_specific_kris", {}),
            contrib.get("study_specific_kris", {}),
        ),
        "qtls": _build_section_card(
            sec_cfg.get("qtls", {}).get("name", "Quality Tolerance Limits"),
            float(sec_cfg.get("qtls", {}).get("weight", 0.20)),
            section_results.get("qtls", {}),
            contrib.get("qtls", {}),
        ),
        "metadata": _build_section_card(
            sec_cfg.get("section_metadata", {}).get("name", "Section 1 Metadata"),
            float(sec_cfg.get("section_metadata", {}).get("weight", 0.05)),
            section_results.get("metadata", {}),
            contrib.get("section_metadata", {}),
        ),
    }


def _build_section_card(name, weight, section_data, contribution_data):
    gen = section_data.get("kri_count_gen", section_data.get("qtl_count_gen", 0))
    gt = section_data.get("kri_count_gt", section_data.get("qtl_count_gt", 0))
    matched = section_data.get("matched_count", 0)
    if "qtl_count_gt" in section_data:
        matched = section_data.get("gt_matched_count", matched)
    return {
        "name": name,
        "weight": weight,
        "score": section_data.get("section_score", 0.0),
        "weighted_contribution": contribution_data.get("weighted", 0.0),
        "count": f"{matched} / {gt}",
        "generated_count": gen,
        "matched": matched,
        "missed": section_data.get("missed_count", section_data.get("missed_count", 0)),
    }


def _legacy_kri_detail_flat(section_data: dict) -> list:
    detail = []
    for m in section_data.get("matched_kris", []):
        attrs = m.get("attribute_scores", {})
        iqmp_info = attrs.get("iqmp_risk_id", {})
        mod_info = attrs.get("moderate_threshold", {})
        high_info = attrs.get("high_threshold", {})
        wt_info = attrs.get("weight_field", {})
        issues = []
        if attrs.get("kri_label", {}).get("score", 0) < 1.0:
            issues.append("label_near_miss")
        if mod_info.get("score", 0) < 1.0:
            issues.append("moderate_threshold_mismatch")
        if high_info.get("score", 0) < 1.0:
            issues.append("high_threshold_mismatch")
        if iqmp_info.get("is_hallucination"):
            issues.append("hallucinated_iqmp_id")
        if wt_info.get("score", 0) < 1.0:
            issues.append("weight_mismatch")
        detail.append(
            {
                "generated_label": m.get("generated_label"),
                "gt_label": m.get("gt_label"),
                "kri_score": m.get("kri_score"),
                "match_type": attrs.get("kri_label", {}).get("match_type", ""),
                "mod_threshold": f"gen={mod_info.get('generated')} gt={mod_info.get('ground_truth')} score={mod_info.get('score', 0):.2f}",
                "high_threshold": f"gen={high_info.get('generated')} gt={high_info.get('ground_truth')} score={high_info.get('score', 0):.2f}",
                "iqmp_id_score": iqmp_info.get("score", 0),
                "weight_score": wt_info.get("score", 0),
                "is_hallucination": m.get("is_hallucination", False),
                "issues": issues,
            }
        )
    for m in section_data.get("missed_kris", []):
        detail.append(
            {
                "generated_label": "** MISS **",
                "gt_label": m.get("gt_label"),
                "kri_score": 0,
                "match_type": "MISS",
                "issues": ["kri_not_generated"],
            }
        )
    return detail


def _derive_improvement_actions(section_results: dict, metrics: dict, structure_result) -> list:
    """Generate prioritized improvement actions from eval results."""
    actions = []

    structure_errors = list(structure_result.errors)
    grouped_structure_errors: Dict[tuple, List[str]] = {}
    qtl_missing_fields = set()
    qtl_missing_total = 0
    for err in structure_errors:
        msg = str(err.get("message", ""))
        path = str(err.get("path", ""))
        if err.get("code") == "QTL_MISSING_FIELD" and path.startswith("$.qtls["):
            m = re.search(r"Required field '([^']+)'", msg)
            if m:
                qtl_missing_fields.add(m.group(1))
                qtl_missing_total += 1
            continue
        key = (str(err.get("code", "")), msg)
        grouped_structure_errors.setdefault(key, []).append(path)
    for (_code, msg), paths in grouped_structure_errors.items():
        if len(paths) > 1:
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "Structure",
                    "type": "structure_error",
                    "action": f"{msg} ({len(paths)} occurrences)",
                    "fix_location": paths[0],
                }
            )
        else:
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "Structure",
                    "type": "structure_error",
                    "action": msg,
                    "fix_location": paths[0] if paths else "",
                }
            )
    if qtl_missing_fields:
        fields_txt = ", ".join(sorted(qtl_missing_fields))
        actions.append(
            {
                "priority": "HIGH",
                "section": "Structure",
                "type": "structure_error",
                "action": (
                    f"QTL structure fields missing across generated rows: {fields_txt} "
                    f"({qtl_missing_total} missing-field events)."
                ),
                "fix_location": "cmp_generator -> qtl_builder",
            }
        )

    m4 = metrics.get("m4", {})
    for item in m4.get("hallucinated_items", []):
        if isinstance(item, dict) and item.get("type") == "extra_qtl_no_gt_match":
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "QTLs",
                    "type": "hallucination",
                    "action": f"Extra QTL generated with no GT match: '{item.get('qtl_name')}'.",
                    "fix_location": "qtl_selector.py -> study-specific gating",
                }
            )
            continue
        if isinstance(item, dict) and item.get("type") == "extra_kri_no_gt_match":
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "KRIs",
                    "type": "hallucination",
                    "action": f"Extra KRI generated with no GT match: '{item.get('kri_label')}'.",
                    "fix_location": "signal_detector.py / kri_selector.py",
                }
            )
            continue
        label = item.get("kri_label", item) if isinstance(item, dict) else item
        gen_id = item.get("generated_iqmp", "") if isinstance(item, dict) else ""
        gt_id = item.get("gt_iqmp", "") if isinstance(item, dict) else ""
        actions.append(
            {
                "priority": "HIGH",
                "section": "KRIs",
                "type": "hallucination",
                "action": f"Conflicting IQMP ID for '{label}': generated={gen_id} vs GT={gt_id}. Verify against training YAML.",
                "fix_location": "ss_kri_benchmarks.yaml or global_kri_rules.yaml",
            }
        )

    for section in ["global_kris", "study_specific_kris"]:
        for miss in section_results.get(section, {}).get("missed_kris", []):
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "SS KRIs" if section == "study_specific_kris" else "Global KRIs",
                    "type": "miss",
                    "action": f"KRI not generated: '{miss.get('gt_label')}'. Check signal_detector.py instrument/signal list.",
                    "fix_location": "signal_detector.py → KNOWN_INSTRUMENTS or kri_selector.py",
                }
            )

    for miss in section_results.get("qtls", {}).get("missed_qtls", []):
        if miss.get("reason") == "not_generated":
            actions.append(
                {
                    "priority": "HIGH",
                    "section": "QTLs",
                    "type": "miss",
                    "action": f"QTL not generated: '{miss.get('gt_name')}'. Check qtl_selector.py trigger conditions.",
                    "fix_location": "qtl_selector.py → trigger logic",
                }
            )

    for section in ["global_kris", "study_specific_kris"]:
        for kri_m in section_results.get(section, {}).get("matched_kris", []):
            attrs = kri_m.get("attribute_scores", {})
            for thr in ["moderate_threshold", "high_threshold"]:
                if attrs.get(thr, {}).get("score", 1.0) < 0.5:
                    actions.append(
                        {
                            "priority": "MEDIUM",
                            "section": section.replace("_", " ").title(),
                            "type": "threshold_mismatch",
                            "action": f"'{kri_m.get('generated_label')}' {thr}: gen={attrs[thr]['generated']} vs gt={attrs[thr]['ground_truth']}. Check YAML modal threshold.",
                            "fix_location": "ss_kri_benchmarks.yaml or global_kri_rules.yaml",
                        }
                    )

    for section in ["global_kris", "study_specific_kris"]:
        for kri_m in section_results.get(section, {}).get("matched_kris", []):
            attrs = kri_m.get("attribute_scores", {})
            wt = attrs.get("weight_field", {})
            if wt.get("score", 1.0) < 1.0:
                actions.append(
                    {
                        "priority": "MEDIUM",
                        "section": section.replace("_", " ").title(),
                        "type": "weight_mismatch",
                        "action": f"'{kri_m.get('generated_label')}' weight: gen='{wt.get('generated')}' vs gt='{wt.get('ground_truth')}'. Apply YAML weight verbatim.",
                        "fix_location": "ss_kri_benchmarks.yaml → weight field",
                    }
                )

    for qtl_m in section_results.get("qtls", {}).get("matched_qtls", []):
        if qtl_m.get("name_score", 100) < 100:
            actions.append(
                {
                    "priority": "LOW",
                    "section": "QTLs",
                    "type": "near_miss",
                    "action": f"QTL name near-miss: gen='{qtl_m.get('generated_name')}' vs gt='{qtl_m.get('gt_name')}'. Inject study ID from USDM at render time.",
                    "fix_location": "qtl_selector.py → QTL name rendering",
                }
            )

    for warn in structure_result.warnings:
        actions.append(
            {
                "priority": "LOW",
                "section": "Structure",
                "type": "structure_warning",
                "action": warn["message"],
                "fix_location": warn.get("path", ""),
            }
        )

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    actions.sort(key=lambda x: order.get(x.get("priority", "LOW"), 2))
    return actions


# ─── Console Printer ────────────────────────────────────────────────────────────

def print_report(report: dict):
    """Print a human-readable eval report to console."""
    study = report.get("study_id") or report.get("eval_metadata", {}).get("study_id", "?")
    ta = report.get("therapeutic_area", report.get("eval_metadata", {}).get("therapeutic_area", ""))
    phase = report.get("phase", report.get("eval_metadata", {}).get("phase", ""))
    doc_score = report["document_score"]
    passed = report.get("document_passed", report.get("document_pass"))
    threshold = report.get("pass_threshold", report.get("document_pass_threshold"))
    target = report.get("target", report.get("document_target"))
    eval_dt = report.get("eval_date") or report.get("eval_metadata", {}).get("eval_date", "")

    bar = "=" * 80
    print(f"\n{bar}")
    print(f"  CMP EVAL REPORT — {study}")
    print(f"  TA: {ta} | Phase: {phase}")
    print(f"  Eval date: {eval_dt}")
    print(bar)

    status = "PASS ✓" if passed else "FAIL ✗"
    print(f"\n  DOCUMENT SCORE:  {doc_score} / 100   {status}")
    print(f"  Threshold: {threshold}   Target: {target}\n")

    sv = report.get("structure_validation", {})
    s_pass = "PASS ✓" if sv.get("passed") else "FAIL ✗"
    print(f"  Structure Validation: {s_pass}  (score: {sv.get('structure_score', 0):.0f}/100)")
    print(f"  Errors: {sv.get('error_count', 0)}   Warnings: {sv.get('warning_count', 0)}")
    if sv.get("errors"):
        for e in sv["errors"][:15]:
            print(f"    [ERROR] {e['path']}: {e['message']}")
    if sv.get("warnings"):
        for w in sv["warnings"][:5]:
            print(f"    [WARN]  {w['path']}: {w['message']}")

    print(f"\n  {'─'*76}")
    print(f"  {'METRIC':<35} {'SCORE':>8}  {'TARGET':>8}  {'PASS/FAIL':>10}")
    print(f"  {'─'*76}")
    for _k, m in report.get("metrics", {}).items():
        if not m:
            continue
        if str(m.get("metric", "")).startswith("M4"):
            score_str = str(m.get("score", ""))
            tv = m.get("target")
            target_str = str(int(tv)) if isinstance(tv, float) and tv == int(tv) else str(tv)
        else:
            score_str = str(m.get("score_pct", m.get("score", "")))
            t = m.get("target")
            target_str = f"{t * 100:.0f}%" if isinstance(t, float) and 0 < t <= 1 else str(t)
        pf = "PASS ✓" if m.get("passed") else "FAIL ✗"
        print(f"  {str(m.get('metric', '')):<35} {score_str:>8}  {target_str:>8}  {pf:>10}")
    print(f"  {'─'*76}")

    print(f"\n  {'─'*76}")
    print(f"  {'SECTION':<32} {'WEIGHT':>7}  {'SCORE':>7}  {'WEIGHTED':>8}  {'KRIs':>8}")
    print(f"  {'─'*76}")
    for _k, s in report.get("section_scorecard", {}).items():
        name = str(s.get("name", _k))[:31]
        w = f"{float(s.get('weight', 0)) * 100:.0f}%"
        sc = f"{float(s.get('score', 0)):.1f}"
        wc = f"{float(s.get('weighted_contribution', 0)):.2f}"
        cnt = s.get("count", "")
        print(f"  {name:<32} {w:>7}  {sc:>7}  {wc:>8}  {cnt:>8}")
    print(f"  {'─'*76}")

    counts = report.get("kri_counts", {})
    print(f"\n  KRI Counts:")
    print(f"    Global KRIs:   generated={counts.get('global_generated', 0)}  gt={counts.get('global_gt', 0)}")
    print(f"    SS KRIs:       generated={counts.get('ss_generated', 0)}  gt={counts.get('ss_gt', 0)}")
    print(f"    QTLs:          generated={counts.get('qtls_generated', 0)}  gt={counts.get('qtls_gt', 0)}")

    print(f"\n  Key Issues:")
    issues_found = 0
    for section in ["global_kris", "study_specific_kris"]:
        kri_detail = report.get("kri_detail", {}).get(section, [])
        for kri in kri_detail:
            if kri.get("match_type") == "MISS":
                print(f"    [MISS]  {section}: '{kri.get('gt_label')}'")
                issues_found += 1
            elif kri.get("is_hallucination"):
                gen_label = kri.get("generated_label", "?")
                print(f"    [HALLUCINATION] IQMP conflict on: '{gen_label}'")
                issues_found += 1
    for qtl in report.get("kri_detail", {}).get("qtls", []):
        if qtl.get("name_score", 100) < 70:
            print(f"    [QTL NEAR-MISS] gen='{qtl.get('generated_name')}' gt='{qtl.get('gt_name')}'")
            issues_found += 1
    if issues_found == 0:
        print(f"    None detected")

    actions = report.get("improvement_actions", [])
    if actions:
        print(f"\n  Improvement Actions ({len(actions)} total):")
        for a in actions[:10]:
            p = a.get("priority", "?")
            print(f"    [{p}] {a.get('section', '')}: {str(a.get('action', ''))[:80]}")

    print(f"\n{bar}\n")


# ─── Report Saver ─────────────────────────────────────────────────────────────

def save_report(report: dict, output_dir: str, study_id: str) -> str:
    """Save report as JSON file. Returns file path."""
    os.makedirs(output_dir, exist_ok=True)
    ed = report.get("eval_date") or report.get("eval_metadata", {}).get("eval_date", "unknown")
    filename = f"eval_report_{study_id}_{ed}.json"
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


def save_summary_csv(reports: list[dict], output_dir: str) -> str:
    """Save multi-study summary as CSV."""
    import csv

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "eval_summary.csv")
    if not reports:
        return path

    rows = []
    for r in reports:
        rows.append(
            {
                "study_id": r.get("study_id"),
                "eval_date": r.get("eval_date") or r.get("eval_metadata", {}).get("eval_date"),
                "document_score": r.get("document_score"),
                "passed": r.get("document_passed", r.get("document_pass")),
                "structure_passed": r.get("structure_validation", {}).get("passed"),
                "structure_score": r.get("structure_validation", {}).get("structure_score"),
                "m1_kri_recall": r.get("metrics", {}).get("M1_kri_recall", {}).get("score_pct"),
                "m1_passed": r.get("metrics", {}).get("M1_kri_recall", {}).get("passed"),
                "m2_threshold_accuracy": r.get("metrics", {}).get("M2_threshold_accuracy", {}).get("score_pct"),
                "m2_passed": r.get("metrics", {}).get("M2_threshold_accuracy", {}).get("passed"),
                "m3_qtl_recall": r.get("metrics", {}).get("M3_qtl_recall", {}).get("score_pct"),
                "m3_passed": r.get("metrics", {}).get("M3_qtl_recall", {}).get("passed"),
                "m4_hallucinations": r.get("metrics", {}).get("M4_hallucinations", {}).get("score"),
                "m4_passed": r.get("metrics", {}).get("M4_hallucinations", {}).get("passed"),
                "global_kri_score": r.get("section_scorecard", {}).get("global_kris", {}).get("score"),
                "ss_kri_score": r.get("section_scorecard", {}).get("study_specific_kris", {}).get("score"),
                "qtl_score": r.get("section_scorecard", {}).get("qtls", {}).get("score"),
                "global_kri_counts": r.get("section_scorecard", {}).get("global_kris", {}).get("count"),
                "ss_kri_counts": r.get("section_scorecard", {}).get("study_specific_kris", {}).get("count"),
                "qtl_counts": r.get("section_scorecard", {}).get("qtls", {}).get("count"),
                "improvements_high": sum(
                    1 for a in r.get("improvement_actions", []) if a.get("priority") == "HIGH"
                ),
            }
        )

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    return path
