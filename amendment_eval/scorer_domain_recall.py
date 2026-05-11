"""
M1 — Domain Recall and M5 — Confidence Calibration scorer.

M1: For each actual amendment change in ground truth, was the corresponding
USDM entity predicted at High or Medium confidence?
  - M1_global: recall on Global-scope changes
  - M1_country: recall on Country-specific changes

M5: Of predictions marked High confidence, what % actually match a real
change in the ground truth? (B7981027 Amendment 4 only)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_ground_truth(gt_path: str | Path) -> dict:
    with open(gt_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _entity_predicted(
    entity: str,
    scope: str,
    predicted_entities: list[tuple[str, str]],
    near_miss_map: dict[str, list[str]],
) -> tuple[bool, bool]:
    """Return (exact_match, near_miss) for one ground-truth row."""
    for p_entity, p_scope in predicted_entities:
        if p_entity == entity and _scope_matches(scope, p_scope):
            return True, False
    for p_entity, p_scope in predicted_entities:
        related = near_miss_map.get(entity, [])
        if p_entity in related and _scope_matches(scope, p_scope):
            return False, True
    return False, False


def _scope_matches(actual_scope: str, predicted_scope: str) -> bool:
    """Global matches Global; country matches same country or Global."""
    a = actual_scope.strip().lower()
    p = predicted_scope.strip().lower()
    if a == "global":
        return p == "global"
    return p == a or p == "global"


def score_m1(
    generated_json: dict,
    gt_path: str | Path,
    config: dict,
) -> dict[str, Any]:
    """Compute M1_global, M1_country, and per-change detail."""
    gt = _load_ground_truth(gt_path)
    near_miss_map = config.get("m1_near_miss_entities", {})
    high_medium = {"High", "Medium"}

    all_predicted: list[tuple[str, str]] = []
    for amend in generated_json.get("predictedAmendments", []):
        for ch in amend.get("predictedChanges", []):
            conf = ch.get("confidence", "")
            if conf in high_medium:
                all_predicted.append((ch["usdmEntity"], ch.get("scope", "Global")))

    global_detail = []
    for row in gt["global_changes"]:
        matched, near = _entity_predicted(
            row["usdm_entity"], row["scope"], all_predicted, near_miss_map
        )
        global_detail.append({
            "actual_entity": row["usdm_entity"],
            "scope": row["scope"],
            "description": row["description"],
            "matched": matched,
            "near_miss": near,
        })

    country_detail = []
    for row in gt["country_changes"]:
        matched, near = _entity_predicted(
            row["usdm_entity"], row["scope"], all_predicted, near_miss_map
        )
        country_detail.append({
            "actual_entity": row["usdm_entity"],
            "scope": row["scope"],
            "description": row["description"],
            "matched": matched,
            "near_miss": near,
        })

    g_matched = sum(1 for r in global_detail if r["matched"])
    g_total = len(global_detail)
    c_matched = sum(1 for r in country_detail if r["matched"])
    c_total = len(country_detail)

    targets = config.get("metric_targets", {})

    m1_global_score = round(g_matched / g_total, 3) if g_total else None
    m1_country_score = round(c_matched / c_total, 3) if c_total else None

    return {
        "m1_global": {
            "score": m1_global_score,
            "target": targets.get("m1_global", 0.60),
            "status": _status(m1_global_score, targets.get("m1_global", 0.60)),
            "numerator": g_matched,
            "denominator": g_total,
        },
        "m1_country": {
            "score": m1_country_score,
            "target": targets.get("m1_country", 0.40),
            "status": _status(m1_country_score, targets.get("m1_country", 0.40)),
            "numerator": c_matched,
            "denominator": c_total,
        },
        "m1_global_detail": global_detail,
        "m1_country_detail": country_detail,
        "m1_global_missed": [
            r["description"] for r in global_detail if not r["matched"]
        ],
        "m1_country_missed": [
            r["description"] for r in country_detail if not r["matched"]
        ],
    }


def score_m5(
    generated_json: dict,
    gt_path: str | Path,
    config: dict,
) -> dict[str, Any]:
    """M5 — confidence calibration on High-confidence predictions only.

    Unlike M1, M5 uses strict entity matching (no near-misses).
    A High-confidence prediction is validated only if the exact
    usdmEntity appears in the ground truth at a compatible scope.
    """
    gt = _load_ground_truth(gt_path)

    gt_entities: list[tuple[str, str]] = []
    for row in gt["global_changes"] + gt["country_changes"]:
        gt_entities.append((row["usdm_entity"], row["scope"]))

    high_preds = []
    for amend in generated_json.get("predictedAmendments", []):
        for ch in amend.get("predictedChanges", []):
            if ch.get("confidence") == "High":
                entity = ch["usdmEntity"]
                scope = ch.get("scope", "Global")
                matched = _gt_contains_exact(entity, scope, gt_entities)
                high_preds.append({
                    "change": f"Amend {amend['amendmentNumber']} Ch{ch['changeNumber']}",
                    "entity": entity,
                    "scope": scope,
                    "confidence": "High",
                    "matched": matched,
                })

    validated = sum(1 for p in high_preds if p["matched"])
    total = len(high_preds)
    targets = config.get("metric_targets", {})
    score = round(validated / total, 3) if total else None

    return {
        "m5": {
            "score": score,
            "target": targets.get("m5", 0.70),
            "status": _status(score, targets.get("m5", 0.70)),
            "high_confidence_detail": high_preds,
        }
    }


def _gt_contains_exact(
    entity: str,
    scope: str,
    gt_entities: list[tuple[str, str]],
) -> bool:
    """Strict match for M5 — entity must match exactly."""
    for gt_e, gt_s in gt_entities:
        if gt_e == entity and _scope_matches(gt_s, scope):
            return True
    return False


def _gt_contains(
    entity: str,
    scope: str,
    gt_entities: list[tuple[str, str]],
    near_miss_map: dict[str, list[str]],
) -> bool:
    if _gt_contains_exact(entity, scope, gt_entities):
        return True
    for gt_e, gt_s in gt_entities:
        related = near_miss_map.get(gt_e, [])
        if entity in related and _scope_matches(gt_s, scope):
            return True
    return False


def _status(score: float | None, target: float) -> str:
    if score is None:
        return "N/A"
    return "PASS" if score >= target else "FAIL"
