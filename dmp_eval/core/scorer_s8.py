"""M3 — S8 critical data modules vs GT S8_critical_data."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.dmp_data import (
    expected_s8_confidence_from_layer,
    gen_s8_confidence_tier,
    normalize_s8_source_tag,
    tier_from_rationale,
)


def _gt_layer_hint(rationale: str) -> str:
    t = (rationale or "").lower()
    if "primary endpoint" in t:
        return "L1"
    if "jak" in t or "signal-conditional" in t or "conditional" in t:
        return "L3"
    return "L2"
from core.semantic_matcher import SemanticMatcher, verbatim_semantic_attribute_score, tier_score_fn, weighted_average


def score_s8(
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    cfg: Dict[str, Any],
    matcher: SemanticMatcher,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    attrs = cfg.get("attributes") or {}
    gt_mods = list(gt_rec.get("S8_critical_data") or [])
    s8 = dmp.get("S8_critical_data") or {}
    gen_mods = list(s8.get("modules") or []) if isinstance(s8, dict) else []

    gtn = [str(m.get("data_module") or "").strip() for m in gt_mods]
    ggn = [str(m.get("data_module") or "").strip() for m in gen_mods]

    mat = matcher.batch_cosine_matrix(gtn, ggn) if gtn and ggn else []

    used_gen: set[int] = set()
    rows: List[Dict[str, Any]] = []

    a_mod = attrs.get("s8_module_name") or {}
    a_tier = attrs.get("s8_tier") or {}
    a_src = attrs.get("s8_source_tag") or {}
    a_layer = attrs.get("s8_layer") or {}
    a_conf = attrs.get("s8_confidence_tier") or {}

    for i, gt in enumerate(gt_mods):
        gt_name = str(gt.get("data_module") or "").strip()
        gt_rat = str(gt.get("rationale") or "")
        gt_tier = tier_from_rationale(gt_rat)
        best_j = -1
        best_sim = -1.0
        if mat and i < len(mat):
            for j in range(len(ggn)):
                if j in used_gen:
                    continue
                if mat[i][j] > best_sim:
                    best_sim = mat[i][j]
                    best_j = j
        if best_j < 0 or best_sim < 0.25:
            rows.append(
                {
                    "ground_truth_module": gt_name,
                    "generated_module": None,
                    "item_score": 0.0,
                    "match_status": "miss",
                    "layer": "L3",
                    "note": f"No generated module matched (best similarity {best_sim:.2f}).",
                    "_source_tag_valid": True,
                }
            )
            continue

        used_gen.add(best_j)
        gm = gen_mods[best_j]
        gen_name = str(gm.get("data_module") or "").strip()
        gen_rat = str(gm.get("rationale") or "")
        gen_tier = tier_from_rationale(gen_rat)
        layer = gm.get("layer", 2)
        try:
            li = int(layer)
        except (TypeError, ValueError):
            li = 2
        layer_str = f"L{li}"

        sm, ml, md = verbatim_semantic_attribute_score(gt_name, gen_name, a_mod, matcher)
        tier_ex = gt_tier
        tier_ok = 1.0 if gen_tier == tier_ex else 0.0
        tier_detail = {
            "score": tier_ok,
            "match_type": "exact",
            "generated": gen_tier,
            "ground_truth": tier_ex,
        }
        tag = normalize_s8_source_tag(gm)
        src_ok = tag is not None
        src_score = 1.0 if src_ok else 0.0
        src_detail = {
            "score": src_score,
            "match_type": "boolean",
            "generated": tag,
            "valid": src_ok,
        }
        exp_layer = _gt_layer_hint(gt_rat)
        gen_ls = layer_str
        lay_ok = 1.0 if exp_layer == gen_ls else 0.0
        lay_detail = {
            "score": lay_ok,
            "match_type": "exact",
            "generated": gen_ls,
            "expected": exp_layer,
        }

        gen_c = gen_s8_confidence_tier(gm)
        exp_c = expected_s8_confidence_from_layer(li)
        csc, cde = tier_score_fn(gen_c, exp_c, a_conf)

        w = [
            float(a_mod.get("weight", 0.35)),
            float(a_tier.get("weight", 0.25)),
            float(a_src.get("weight", 0.20)),
            float(a_layer.get("weight", 0.10)),
            float(a_conf.get("weight", 0.10)),
        ]
        comp = weighted_average(w, [sm, tier_ok, src_score, lay_ok, csc])
        item_score = round(100.0 * comp, 2)

        if sm >= 1.0:
            ms = "verbatim"
        elif ml in ("near_miss", "semantic_high", "semantic_med"):
            ms = "near_miss"
        else:
            ms = "mismatch"

        rows.append(
            {
                "ground_truth_module": gt_name,
                "generated_module": gen_name,
                "item_score": item_score,
                "match_status": ms,
                "layer": layer_str,
                "attributes": {
                    "s8_module_name": {"score": round(sm, 2), "match_type": ml, **md},
                    "s8_tier": tier_detail,
                    "s8_source_tag": src_detail,
                    "s8_layer": lay_detail,
                    "s8_confidence_tier": {"score": round(csc, 2), **cde},
                },
                "_source_tag_valid": src_ok,
            }
        )

    for j, gm in enumerate(gen_mods):
        if j in used_gen:
            continue
        tag = normalize_s8_source_tag(gm)
        rows.append(
            {
                "ground_truth_module": None,
                "generated_module": str(gm.get("data_module") or ""),
                "item_score": 0.0,
                "match_status": "extra",
                "layer": f"L{gm.get('layer', '?')}",
                "note": "Extra generated module — not in ground truth list",
                "_source_tag_valid": tag is not None,
            }
        )

    n_gt = len(gt_mods)
    n_gen = len(gen_mods)
    denom = max(n_gt, n_gen, 1)
    sec = sum(float(r["item_score"]) for r in rows if isinstance(r.get("item_score"), (int, float))) / denom

    meta = {
        "generated_count": n_gen,
        "ground_truth_count": n_gt,
        "matched": len(used_gen),
        "score": round(min(100.0, sec), 2),
    }
    return rows, meta
