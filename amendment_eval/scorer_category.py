"""
M2 — Category Accuracy scorer.

For each predicted amendment, does the predicted category match the actual
category of the corresponding real amendment?

Scoring:
  - Exact match = 1.0
  - Multiple predicted vs specific actual (or vice versa) = 0.5
  - Wrong = 0.0
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def _load_header_gt(csv_path: str | Path, study_id: str) -> list[dict]:
    """Load amendment header ground truth rows for the given study."""
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["study_id"] == study_id and row["split"] == "verify":
                rows.append(row)
    return rows


def _category_score(predicted: str, actual: str) -> float:
    if actual.lower() == "unknown":
        return 1.0  # no ground truth — cannot penalize

    p = predicted.strip().lower()
    a = actual.strip().lower()

    if p == a:
        return 1.0

    if p == "multiple" and a != "multiple":
        return 0.5
    if a == "multiple" and p != "multiple":
        return 0.5

    return 0.0


def score_m2(
    generated_json: dict,
    header_csv_path: str | Path,
    study_id: str,
    config: dict,
) -> dict[str, Any]:
    """Compute M2 category accuracy for the study."""
    gt_rows = _load_header_gt(header_csv_path, study_id)

    gt_by_amend: dict[int, str] = {}
    for row in gt_rows:
        if row.get("summary_pdf_available", "").lower() == "yes":
            gt_by_amend[int(row["amendment_number"])] = row["actual_category"]

    if not gt_by_amend:
        return {
            "m2": {
                "score": None,
                "target": config.get("metric_targets", {}).get("m2", 0.75),
                "status": "N/A",
                "detail": [],
                "note": "No amendment summary PDFs available for this study",
            }
        }

    scores = []
    detail = []
    for amend in generated_json.get("predictedAmendments", []):
        amend_num = amend.get("amendmentNumber")
        predicted_cat = amend.get("predictedCategory", "Unknown")
        actual_cat = gt_by_amend.get(amend_num, "Unknown")
        s = _category_score(predicted_cat, actual_cat)
        scores.append(s)
        detail.append({
            "amendment_number": amend_num,
            "predicted_category": predicted_cat,
            "actual_category": actual_cat,
            "score": s,
        })

    avg_score = round(sum(scores) / len(scores), 3) if scores else None
    target = config.get("metric_targets", {}).get("m2", 0.75)

    predicted_cats = list({
        a.get("predictedCategory", "Unknown")
        for a in generated_json.get("predictedAmendments", [])
    })
    actual_cats = list(gt_by_amend.values())

    return {
        "m2": {
            "score": avg_score,
            "target": target,
            "status": "PASS" if avg_score is not None and avg_score >= target else (
                "FAIL" if avg_score is not None else "N/A"
            ),
            "predicted_categories": predicted_cats,
            "actual_category": actual_cats[0] if len(actual_cats) == 1 else actual_cats,
            "detail": detail,
        }
    }
