"""
PIPD composite quality score (0–100%) and per-subcategory breakdown.

Weighted composite uses three components (default weights, auto-normalised to 1.0):
  Completeness  40% – categories present in JSON + GT subcategories matched (greedy, Lev <= threshold)
  Accuracy      30% – exact string match among matched pairs
  Semantic      20% – mean BERTScore F1 if bert-score installed, else normalized Levenshtein

  weighted_composite_% = 100 × Σ(weight_i × component_i)

Hallucination is NOT part of the weighted composite.
Instead, **percentage-point deductions** are applied after the weighted composite:
  – per hallucinated subcategory (unmatched generated row, incl. rows under generator-only categories)
  – per hallucinated (generator-only) category number
  final_% = max(0, weighted_composite_% − sub_deductions − cat_deductions)

  Override deduction rates with env ``PIPD_HALLUCINATION_DEDUCTION_SUB_PCT``
  and ``PIPD_HALLUCINATION_DEDUCTION_CAT_PCT`` (defaults 0.25 and 1.0 pp respectively).

Per-subcategory micro-score (used in the document report):
  micro_weight  = 100 / total_GT_subcategories          (equal share of 100 points)
  weighted_0_1  = w_c×1.0 + w_a×exact_match + w_s×semantic_f1   (for matched rows)
                  0.0                                              (for missed rows)
  points_earned = micro_weight × min(1.0, weighted_0_1)
  Overall points for the document = Σ points_earned across all GT subcategories (out of 100).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from Levenshtein import distance as lev

from core.eval_scenario1 import (
    NEAR_MISS_THRESHOLD,
    NUM_CATEGORIES,
    get_subcategories_by_category,
    load_generator_json,
    load_ground_truth,
)

# Hallucination is penalised via post-hoc pp deductions only — not in the weighted composite.
DEFAULT_WEIGHTS = {
    "completeness": 0.40,
    "accuracy": 0.30,
    "semantic": 0.20,
}

# Percentage points deducted from final composite per hallucinated item (after weighted sum).
_DEFAULT_DEDUCT_SUB_PCT = "0.25"
_DEFAULT_DEDUCT_CAT_PCT = "1.0"


def _hallucination_deduction_sub_pct() -> float:
    return float(os.environ.get("PIPD_HALLUCINATION_DEDUCTION_SUB_PCT", _DEFAULT_DEDUCT_SUB_PCT))


def _hallucination_deduction_cat_pct() -> float:
    return float(os.environ.get("PIPD_HALLUCINATION_DEDUCTION_CAT_PCT", _DEFAULT_DEDUCT_CAT_PCT))


def _greedy_match_category(
    gt_texts: List[str],
    gen_texts: List[str],
) -> Tuple[List[Tuple[str, str, int, bool]], Set[int]]:
    pairs: List[Tuple[str, str, int, bool]] = []
    used_gen: Set[int] = set()

    for gt in gt_texts:
        best_j: Optional[int] = None
        best_d = NEAR_MISS_THRESHOLD + 1
        for j, g in enumerate(gen_texts):
            if j in used_gen:
                continue
            d = 0 if gt == g else lev(gt, g)
            if d < best_d:
                best_d = d
                best_j = j
        if best_j is not None and best_d <= NEAR_MISS_THRESHOLD:
            used_gen.add(best_j)
            g = gen_texts[best_j]
            pairs.append((gt, g, best_d, gt == g))

    return pairs, used_gen


def _normalized_lev_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    m = max(len(a), len(b), 1)
    return max(0.0, 1.0 - lev(a, b) / m)


def _bertscore_f1_batch(refs: List[str], cands: List[str]) -> Optional[List[float]]:
    try:
        from bert_score import score as bert_score_fn
    except ImportError:
        return None
    if not refs:
        return []
    import torch
    with torch.inference_mode():
        _, _, f1 = bert_score_fn(
            cands,
            refs,
            lang="en",
            verbose=False,
            rescale_with_baseline=True,
        )
    return [float(x) for x in f1]


def run_composite_eval(
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    weights: Optional[Dict[str, float]] = None,
    use_bertscore: bool = True,
) -> Dict[str, Any]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    s = sum(w.values())
    if abs(s - 1.0) > 1e-6:
        w = {k: v / s for k, v in w.items()}

    gt_df = load_ground_truth(ground_truth_csv_path, study_id)
    if gt_df.empty:
        raise ValueError(f"No ground-truth rows for study_id={study_id!r}")

    gen = load_generator_json(generator_json_path)
    gen_by_cat = get_subcategories_by_category(gen)

    exp_cats: List[int] = sorted(
        int(x) for x in gt_df["category_num"].dropna().unique().tolist()
    )
    if not exp_cats:
        exp_cats = list(range(1, NUM_CATEGORIES + 1))

    present_cats = [c for c in exp_cats if c in gen_by_cat]
    category_completeness = len(present_cats) / len(exp_cats) if exp_cats else 1.0
    exp_cat_set = set(exp_cats)

    all_pairs: List[Dict[str, Any]] = []
    total_gt_subs = 0
    matched_gt_subs = 0
    exact_matches = 0
    total_gen_subs = 0
    unmatched_gen_total = 0
    per_category_tables: Dict[str, Any] = {}

    for cat_num in exp_cats:
        gt_cat = gt_df[gt_df["category_num"] == cat_num]
        gt_texts = (
            gt_cat["subcategory_text"].dropna().astype(str).tolist()
            if not gt_cat.empty
            else []
        )
        gen_subs = gen_by_cat.get(cat_num, [])
        gen_texts = [str(s.get("subcategory_text", "") or "") for s in gen_subs]
        total_gen_subs += len(gen_texts)
        total_gt_subs += len(gt_texts)

        cat_in_json = cat_num in gen_by_cat
        pairs, used_idx = _greedy_match_category(gt_texts, gen_texts)
        matched_gt_subs += len(pairs)
        for gt, g, dist, is_ex in pairs:
            if is_ex:
                exact_matches += 1

        unmatched_gen_total += sum(1 for j in range(len(gen_texts)) if j not in used_idx)

        rows: List[Dict[str, Any]] = []
        for gt in gt_texts:
            row: Dict[str, Any] = {
                "ground_truth": gt,
                "generated": "",
                "present": False,
                "distance": None,
                "exact": False,
                "semantic_f1": None,
            }
            for pgt, pg, pdist, pex in pairs:
                if pgt == gt:
                    row["generated"] = pg
                    row["present"] = True
                    row["distance"] = pdist
                    row["exact"] = pex
                    break
            rows.append(row)

        for j, gtxt in enumerate(gen_texts):
            if j in used_idx:
                continue
            rows.append({
                "ground_truth": "",
                "generated": gtxt,
                "present": False,
                "distance": None,
                "exact": False,
                "hallucination": True,
                "semantic_f1": None,
            })

        per_category_tables[str(cat_num)] = {
            "category_num": cat_num,
            "category_in_json": cat_in_json,
            "gt_subcategory_count": len(gt_texts),
            "generated_subcategory_count": len(gen_texts),
            "matched_count": len(pairs),
            "rows": rows,
        }
        all_pairs.extend(
            [{"category_num": cat_num, "gt": a, "gen": b, "d": d, "exact": e} for a, b, d, e in pairs]
        )

    for _, block in per_category_tables.items():
        for r in block["rows"]:
            if r.get("hallucination"):
                r["points_earned"] = None
            elif r.get("ground_truth"):
                r["points_earned"] = 0.0

    subcategory_completeness = matched_gt_subs / total_gt_subs if total_gt_subs else 1.0
    completeness = 0.5 * category_completeness + 0.5 * subcategory_completeness

    n_matched = len(all_pairs)
    accuracy = exact_matches / n_matched if n_matched else 1.0

    semantic_scores: List[float] = [0.0] * n_matched
    nonex_refs: List[str] = []
    nonex_cands: List[str] = []
    nonex_idx: List[int] = []
    for i, p in enumerate(all_pairs):
        if p["exact"]:
            semantic_scores[i] = 1.0
        else:
            nonex_refs.append(p["gt"])
            nonex_cands.append(p["gen"])
            nonex_idx.append(i)

    semantic_method = "normalized_levenshtein"
    if use_bertscore and nonex_refs:
        bert_f1s = _bertscore_f1_batch(nonex_refs, nonex_cands)
        if bert_f1s is not None:
            semantic_method = "bert-score"
            for j, idx in enumerate(nonex_idx):
                semantic_scores[idx] = bert_f1s[j]
        else:
            for idx in nonex_idx:
                p = all_pairs[idx]
                semantic_scores[idx] = _normalized_lev_similarity(p["gt"], p["gen"])
    else:
        for idx in nonex_idx:
            p = all_pairs[idx]
            semantic_scores[idx] = _normalized_lev_similarity(p["gt"], p["gen"])

    semantic_mean = sum(semantic_scores) / len(semantic_scores) if semantic_scores else 1.0

    pair_sem_key: Dict[Tuple[int, str, str], float] = {}
    for i, p in enumerate(all_pairs):
        pair_sem_key[(p["category_num"], p["gt"], p["gen"])] = round(semantic_scores[i], 4)

    for _, block in per_category_tables.items():
        for r in block["rows"]:
            if r.get("hallucination") or not r.get("present"):
                continue
            k = (block["category_num"], r["ground_truth"], r["generated"])
            if k in pair_sem_key:
                r["semantic_f1"] = pair_sem_key[k]

    categories_in_generator_not_in_gt = sorted(
        c for c in gen_by_cat.keys() if c not in exp_cat_set
    )
    subcats_in_non_gt_categories = sum(
        len(gen_by_cat[c]) for c in categories_in_generator_not_in_gt
    )
    # Within GT-listed categories only (loop over exp_cats)
    total_gen_in_gt_cats = total_gen_subs
    unmatched_in_gt_cats = unmatched_gen_total
    # Whole extra categories = 100% of their subcats are "unmatched" to any GT line
    total_gen_all = total_gen_in_gt_cats + subcats_in_non_gt_categories
    unmatched_all = unmatched_in_gt_cats + subcats_in_non_gt_categories
    hallucination_rate = unmatched_all / total_gen_all if total_gen_all else 0.0
    hallucination_score = max(0.0, 1.0 - hallucination_rate)

    # Hallucination is penalised via post-hoc deductions only (see below).
    overall_0_1 = (
        w["completeness"] * completeness
        + w["accuracy"] * accuracy
        + w["semantic"] * semantic_mean
    )
    overall_percent_weighted = round(100.0 * overall_0_1, 2)

    d_sub = _hallucination_deduction_sub_pct()
    d_cat = _hallucination_deduction_cat_pct()
    n_halluc_sub = int(unmatched_all)
    n_halluc_cat = len(categories_in_generator_not_in_gt)
    deduction_from_subcategories_pct = round(n_halluc_sub * d_sub, 2)
    deduction_from_categories_pct = round(n_halluc_cat * d_cat, 2)
    total_hallucination_deduction_pct = round(
        deduction_from_subcategories_pct + deduction_from_categories_pct, 2
    )
    overall_percent = max(0.0, round(overall_percent_weighted - total_hallucination_deduction_pct, 2))
    overall_0_1_final = round(overall_percent / 100.0, 6)

    hallucination_deduction = {
        "hallucinated_subcategory_count": n_halluc_sub,
        "hallucinated_extra_category_count": n_halluc_cat,
        "deduction_percent_per_subcategory": d_sub,
        "deduction_percent_per_extra_category": d_cat,
        "deduction_from_subcategories_percent": deduction_from_subcategories_pct,
        "deduction_from_categories_percent": deduction_from_categories_pct,
        "total_deduction_percent": total_hallucination_deduction_pct,
    }

    micro_weight = 100.0 / total_gt_subs if total_gt_subs else 0.0
    micro_rows: List[Dict[str, Any]] = []
    for _, block in sorted(per_category_tables.items(), key=lambda x: int(x[0])):
        cn = block["category_num"]
        for r in block["rows"]:
            if r.get("hallucination") or not r["ground_truth"]:
                continue
            gt_t = r["ground_truth"]
            earned = 0.0
            if r["present"]:
                sem = float(r.get("semantic_f1") or 0.0)
                ex = 1.0 if r["exact"] else 0.0
                local_0_1 = (
                    w["completeness"] * 1.0
                    + w["accuracy"] * ex
                    + w["semantic"] * sem
                )
                earned = micro_weight * min(1.0, local_0_1)
            r["points_earned"] = round(earned, 4)
            micro_rows.append({
                "category_num": cn,
                "ground_truth_preview": (gt_t[:120] + "…") if len(gt_t) > 120 else gt_t,
                "micro_point_max": round(micro_weight, 4),
                "points_earned": round(earned, 4),
            })

    weighted_pct = {
        "completeness": round(100 * w["completeness"] * completeness, 2),
        "accuracy": round(100 * w["accuracy"] * accuracy, 2),
        "semantic": round(100 * w["semantic"] * semantic_mean, 2),
    }

    return {
        "study_id": study_id,
        "eval_date": datetime.now().isoformat(),
        "generator_path": str(generator_json_path),
        "ground_truth_path": str(ground_truth_csv_path),
        "weights": w,
        "methodology": {
            "matching": f"Per category: greedy 1:1 align; Levenshtein distance <= {NEAR_MISS_THRESHOLD} counts as matched.",
            "penalties_and_negative_marking": (
                "Weighted composite % is computed from completeness, accuracy, and semantic only. "
                "Then percentage-point deductions apply for hallucinations: "
                "each hallucinated subcategory (unmatched generated row, including all rows under "
                "generator-only categories) and each generator-only category number."
            ),
            "hallucination_deduction_formula": (
                f"final_% = max(0, weighted_composite_% - (sub_count × {d_sub}) - (extra_cat_count × {d_cat})). "
                "Override rates with env PIPD_HALLUCINATION_DEDUCTION_SUB_PCT / _CAT_PCT."
            ),
            "completeness_formula": "0.5 * (expected_categories_present) + 0.5 * (gt_subcats_matched / gt_subcats_total).",
            "accuracy_formula": "exact_matches / matched_pairs.",
            "semantic_formula": f"Mean pairwise similarity on matched pairs ({semantic_method}).",
            "hallucination_note": (
                "Hallucination score [1 - (unmatched_generated / total_generated)] is computed and reported "
                "but is NOT a weighted composite term. It is used exclusively to compute the post-hoc "
                "percentage-point deductions applied after the weighted composite."
            ),
            "overall_formula": (
                "weighted_composite_% = 100 × (w_c×completeness + w_a×accuracy + w_s×semantic); "
                "final overall % = weighted_composite_% minus hallucination deductions (see hallucination_deduction_formula)."
            ),
            "micro_points": (
                f"Up to 100 points split evenly across {total_gt_subs} GT subcategories (~{micro_weight:.4f} pts/line). "
                "For each matched subcategory: "
                "  weighted_0_1 = w_c×1.0 + w_a×exact_match + w_s×semantic_f1 (same three weights, normalised). "
                "  points_earned = micro_weight × min(1.0, weighted_0_1). "
                "Missed subcategories earn 0. "
                "Σ points_earned across all GT subcategories = overall document score (out of 100)."
            ),
        },
        "components": {
            "completeness": {
                "value_0_1": round(completeness, 6),
                "percent": round(100 * completeness, 2),
                "category_completeness_0_1": round(category_completeness, 6),
                "subcategory_completeness_0_1": round(subcategory_completeness, 6),
                "expected_categories": exp_cats,
                "present_categories_count": len(present_cats),
                "gt_subcategories_total": total_gt_subs,
                "gt_subcategories_matched": matched_gt_subs,
            },
            "accuracy": {
                "value_0_1": round(accuracy, 6),
                "percent": round(100 * accuracy, 2),
                "matched_pairs": n_matched,
                "exact_matches": exact_matches,
            },
            "semantic": {
                "value_0_1": round(semantic_mean, 6),
                "percent": round(100 * semantic_mean, 2),
                "method": semantic_method,
            },
            "hallucination": {
                "value_0_1": round(hallucination_score, 6),
                "percent": round(100 * hallucination_score, 2),
                "unmatched_generated_subcategories": unmatched_all,
                "total_generated_subcategories": total_gen_all,
                "hallucination_rate_0_1": round(hallucination_rate, 6),
                "within_gt_categories_only": {
                    "unmatched_generated_subcategories": unmatched_in_gt_cats,
                    "total_generated_subcategories": total_gen_in_gt_cats,
                },
                "categories_in_generator_not_in_ground_truth": categories_in_generator_not_in_gt,
                "generated_subcategories_in_non_gt_categories": subcats_in_non_gt_categories,
            },
        },
        "overall_score_percent_weighted": overall_percent_weighted,
        "overall_score_0_1_weighted": round(overall_0_1, 6),
        "hallucination_deduction": hallucination_deduction,
        "overall_score_percent": overall_percent,
        "overall_score_0_1": overall_0_1_final,
        "weighted_breakdown_percent": weighted_pct,
        "micro_subcategory": {
            "total_gt_subcategories": total_gt_subs,
            "max_points_if_perfect": 100.0,
            "weight_per_subcategory": round(micro_weight, 6),
            "rows": micro_rows,
            "points_sum_earned": round(sum(m["points_earned"] for m in micro_rows), 4),
        },
        "per_category": per_category_tables,
        "pairing_summary": all_pairs,
    }


def save_composite_json(result: Dict[str, Any], path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
