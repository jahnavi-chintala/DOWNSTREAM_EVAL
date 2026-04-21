"""M4 — S11.4 reconciliation flags vs GT S11_reconciliation."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

S11_MAP: List[Tuple[str, str, str, str]] = [
    ("s11_4_1_irt", "IRT Reconciliation", "irt", "irt_reconciliation"),
    ("s11_4_2_ctms", "CTMS Reconciliation", "ctms", "ctms_reconciliation"),
    ("s11_4_3_sae", "SAE Reconciliation", "sae", "sae_reconciliation"),
    ("s11_4_4_central_lab", "Central Laboratory Data Reconciliation", "central_lab", "central_lab_reconciliation"),
    ("s11_4_5_ecoa", "eCOA Reconciliation", "ecoa", "ecoa_reconciliation"),
    ("s11_4_6_pk_pd", "PK/PD Reconciliation", "pkpd", "pkpd_reconciliation"),
    ("s11_4_7_adjudication", "Adjudication Reconciliation", "adjudication", "adjudication_reconciliation"),
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


def score_s11(
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from dmp_data import s11_gen_sections

    gt11 = gt_rec.get("S11_reconciliation") or {}
    gen_sec = s11_gen_sections(dmp)
    rows: List[Dict[str, Any]] = []

    for sub, label, gt_key, gen_key in S11_MAP:
        gt_b = bool(gt11.get(gt_key))
        gt_flag = _bool_to_flag(gt_b)
        block = gen_sec.get(gen_key) or {}
        if not isinstance(block, dict):
            block = {}
        gen_app = bool(block.get("applicable"))
        gen_flag = _bool_to_flag(gen_app)
        flag_ok = 1.0 if gt_flag == gen_flag else 0.0

        rule = str(block.get("rule") or "")
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
