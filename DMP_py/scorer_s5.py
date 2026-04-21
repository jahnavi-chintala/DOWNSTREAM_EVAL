"""M1 — S5.2 system checkboxes vs dmp_ground_truth_clean.json S5_systems."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from dmp_data import norm_s5_confidence, s5_other_systems
from semantic_matcher import SemanticMatcher, verbatim_semantic_attribute_score, tier_score_fn, weighted_average


def _gt_s5_field(gt_rec: Dict[str, Any], key: str) -> str:
    s5 = gt_rec.get("S5_systems") or {}
    v = s5.get(key)
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def _gen_s5_value(block: Dict[str, Any]) -> Tuple[str, Any, str]:
    """Return display value, checkbox-ish truth, raw confidence string."""
    if not isinstance(block, dict):
        return "", False, ""
    val = block.get("value")
    if val is None:
        val = block.get("vendor") or block.get("applicable")
    if isinstance(val, list):
        disp = ", ".join(str(x) for x in val)
    else:
        disp = str(val or "").strip()
    checked = bool(disp) or bool(block.get("applicable"))
    conf = block.get("confidence") or block.get("source") or ""
    return disp, checked, str(conf)


# Keys aligned between GT S5 object and generator S5_2_other_systems
S5_KEY_PAIRS: List[Tuple[str, str, str]] = [
    ("crf_system", "crf_system", "crf_system"),
    ("cdms", "cdms", "cdms"),
    ("ecrf_config_tool", "ecrf_config_tool", "ecrf_config_tool"),
    ("ctms", "ctms", "ctms"),
    ("irt_system", "irt_system", "irt_system"),
    ("safety_db", "safety_db", "safety_db"),
    ("coding_dictionary", "coding_dictionary", "coding_dictionary"),
    ("rbm_system", "rbm_system", "rbm_system"),
    ("epro_system", "epro_system", "epro_vendor"),
    ("sdq_system", "sdq_system", "sdq_system"),
    ("adjudication_db", "adjudication_db", "adjudication_vendor"),
    ("pd_mgmt_system", "pd_mgmt_system", "pd_mgmt_system"),
    ("photography_system", "photography_system", "photography_vendor"),
    ("randomization_system", "randomization_system", "randomization_system"),
]


def score_s5(
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    cfg: Dict[str, Any],
    matcher: SemanticMatcher,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    attrs = cfg.get("attributes") or {}
    gen_block = s5_other_systems(dmp)
    rows: List[Dict[str, Any]] = []

    for gt_key, gen_key, system_type in S5_KEY_PAIRS:
        gt_name = _gt_s5_field(gt_rec, gt_key)
        gb = gen_block.get(gen_key)
        if not gt_name.strip() and not gb:
            continue

        gen_name, gen_checked, conf_raw = _gen_s5_value(gb if isinstance(gb, dict) else {})
        gt_checked = bool(gt_name.strip())

        a_name = attrs.get("s5_system_name") or {}
        a_cb = attrs.get("s5_checkbox_state") or {}
        a_tier = attrs.get("s5_confidence_tier") or {}

        name_score, match_lbl, ndetail = verbatim_semantic_attribute_score(
            gt_name, gen_name, a_name, matcher
        )

        cb_score = 1.0 if (gt_checked == gen_checked) else 0.0
        cb_detail = {
            "score": cb_score,
            "match_type": "exact",
            "generated": gen_checked,
            "ground_truth": gt_checked,
        }

        gen_t = norm_s5_confidence(conf_raw)
        exp_t = gen_t  # without benchmarks YAML, expect generator tier matches itself (neutral)
        tier_s, tier_d = tier_score_fn(gen_t, exp_t, a_tier)
        tier_d["generated"] = gen_t
        tier_d["expected"] = exp_t

        w = [
            float(a_name.get("weight", 0.5)),
            float(a_cb.get("weight", 0.3)),
            float(a_tier.get("weight", 0.2)),
        ]
        comp = weighted_average(w, [name_score, cb_score, tier_s])
        item_score = round(100.0 * comp, 2)

        if name_score >= 1.0:
            ms = "verbatim"
        elif match_lbl in ("near_miss", "semantic_high", "semantic_med"):
            ms = "near_miss"
        else:
            ms = "mismatch" if comp < 0.99 else "verbatim"

        source_valid = bool((gb or {}).get("source"))
        rows.append(
            {
                "system_type": system_type,
                "ground_truth_name": gt_name or "(empty)",
                "generated_name": gen_name or None,
                "item_score": item_score,
                "match_status": ms,
                "attributes": {
                    "s5_system_name": {
                        "score": round(name_score, 2),
                        "match_type": match_lbl,
                        **ndetail,
                    },
                    "s5_checkbox_state": cb_detail,
                    "s5_confidence_tier": {"score": round(tier_s, 2), **tier_d},
                },
                "_source_tag_valid": source_valid,
            }
        )

    n_gt = len([r for r in rows if r["ground_truth_name"] != "(empty)"])
    n_gen = len([r for r in rows if r.get("generated_name")])
    denom = max(n_gt, n_gen, 1)
    section_score = sum(float(r["item_score"]) for r in rows) / denom

    meta = {
        "generated_count": n_gen,
        "ground_truth_count": n_gt,
        "matched": len([r for r in rows if r["match_status"] in ("verbatim", "near_miss")]),
        "score": round(min(100.0, section_score), 2),
    }
    return rows, meta
