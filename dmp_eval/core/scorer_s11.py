"""M4 — S11.4 reconciliation flags vs GT S11_reconciliation."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

S11_MAP: List[Tuple[str, str, str, str]] = [
    ("s11_4_1_irt", "IRT Reconciliation", "irt", "irt_reconciliation"),
    ("s11_4_2_ctms", "CTMS Reconciliation", "ctms", "ctms_reconciliation"),
    ("s11_4_3_sae", "SAE Reconciliation", "sae", "sae_reconciliation"),
    ("s11_4_4_central_lab", "Central Laboratory Data Reconciliation", "central_lab", "central_lab_reconciliation"),
    ("s11_4_5_retained_research_sample", "Retained Research Sample Reconciliation", "retained_research_sample", "retained_research_sample_reconciliation"),
    ("s11_4_6_sars_cov2_serology", "SARS-CoV-2 Serology Reconciliation", "sars_cov2_serology", "sars_cov2_serology_reconciliation"),
    ("s11_4_7_flow_cytometry", "Cell Phenotype Findings (Flow Cytometry) Reconciliation", "flow_cytometry", "flow_cytometry_reconciliation"),
    ("s11_4_8_viral_load", "Viral Load Reconciliation", "viral_load", "viral_load_reconciliation"),
    ("s11_4_9_ecoa", "eCOA Reconciliation", "ecoa", "ecoa_reconciliation"),
    ("s11_4_10_pk_pd", "PK/PD Reconciliation", "pkpd", "pkpd_reconciliation"),
    ("s11_4_11_adjudication", "Adjudication Reconciliation", "adjudication", "adjudication_reconciliation"),
]


def _bool_to_flag(b: bool) -> str:
    return "Applicable" if b else "N/A"


def _norm_conf(val: Any) -> str:
    s = str(val or "").upper()
    if "AUTO" in s:
        return "high"
    if "REVIEW" in s:
        return "medium"
    if "LOW" in s:
        return "low"
    return "high"


def _infer_extra_recon_flags(dmp: Dict[str, Any]) -> Dict[str, bool]:
    """
    Infer S11.4.5-11.4.8 applicability from S6.2 vendor/data_type rows when
    generator does not emit explicit S11 section keys for them.
    """
    out = {
        "retained_research_sample_reconciliation": False,
        "sars_cov2_serology_reconciliation": False,
        "flow_cytometry_reconciliation": False,
        "viral_load_reconciliation": False,
    }
    s6 = dmp.get("S6_data_flow") or {}
    s62 = s6.get("S6_2_esource_edata") or {}
    vendors = (s62.get("vendors") if isinstance(s62, dict) else None) or []

    for v in vendors:
        if not isinstance(v, dict):
            continue
        dt = str(v.get("data_type") or "").lower()
        if "retained research sample" in dt:
            out["retained_research_sample_reconciliation"] = True
        if "serology" in dt:
            out["sars_cov2_serology_reconciliation"] = True
        if "flow cytometry" in dt or "facs" in dt:
            out["flow_cytometry_reconciliation"] = True
        if "viral load" in dt:
            out["viral_load_reconciliation"] = True
    return out


def score_s11(
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from core.dmp_data import s11_gen_sections

    gt11 = gt_rec.get("S11_reconciliation") or {}
    gen_sec = s11_gen_sections(dmp)
    inferred = _infer_extra_recon_flags(dmp)
    rows: List[Dict[str, Any]] = []

    for sub, label, gt_key, gen_key in S11_MAP:
        gt_b = bool(gt11.get(gt_key))
        gt_flag = _bool_to_flag(gt_b)
        block = gen_sec.get(gen_key) or {}
        if not isinstance(block, dict):
            block = {}
        if block:
            gen_app = bool(block.get("applicable"))
            inferred_rule = ""
        else:
            gen_app = bool(inferred.get(gen_key, False))
            inferred_rule = "Inferred from S6.2 vendor/data_type evidence"
        gen_flag = _bool_to_flag(gen_app)
        flag_ok = 1.0 if gt_flag == gen_flag else 0.0

        rule = str(block.get("rule") or inferred_rule or "")
        rule_ok = 1.0 if rule else 0.0

        gen_conf = _norm_conf(block.get("confidence"))
        exp_conf = "high" if gen_flag == "N/A" or gt_key in ("ctms", "sae") else "high"
        conf_ok = 1.0 if gen_conf == exp_conf else 0.5

        w = [0.7, 0.2, 0.1]
        comp = w[0] * flag_ok + w[1] * rule_ok + w[2] * conf_ok
        item_score = round(100.0 * comp, 2)

        rows.append(
            {
                "subsection": sub,
                "subsection_label": label,
                "ground_truth_flag": gt_flag,
                "generated_flag": gen_flag,
                "item_score": item_score,
                "match_status": "correct" if flag_ok >= 1.0 else "incorrect",
                "attributes": {
                    "s11_flag": {
                        "score": flag_ok,
                        "match_type": "exact",
                        "generated": gen_flag,
                        "ground_truth": gt_flag,
                    },
                    "s11_inference_rule": {
                        "score": rule_ok,
                        "match_type": "boolean",
                        "rule": rule or None,
                        "valid": bool(rule),
                    },
                    "s11_confidence": {
                        "score": conf_ok,
                        "match_type": "exact",
                        "generated": gen_conf,
                        "expected": exp_conf,
                    },
                },
            }
        )

    sec = sum(r["item_score"] for r in rows) / max(len(rows), 1)
    meta = {
        "total_subsections": len(rows),
        "correct_flags": len([r for r in rows if r["ground_truth_flag"] == r["generated_flag"]]),
        "score": round(sec, 2),
    }
    return rows, meta
