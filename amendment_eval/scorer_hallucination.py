"""
M3 — Hallucination Rate scorer.

A hallucinated prediction is any predicted change where:
  - lineage.confidence_basis.entity_rate == 0  AND
  - len(lineage.training_evidence) == 0

Any hallucination on any verify study triggers an immediate STOP.
"""

from __future__ import annotations

from typing import Any


def score_m3(
    generated_json: dict,
    study_id: str,
    config: dict,
) -> dict[str, Any]:
    """Detect hallucinations across all predicted changes."""
    hallucinations = []
    total_changes = 0

    for amend in generated_json.get("predictedAmendments", []):
        for ch in amend.get("predictedChanges", []):
            total_changes += 1
            lineage = ch.get("lineage", {})
            cb = lineage.get("confidence_basis", {})
            entity_rate = cb.get("entity_rate", 0)
            evidence = lineage.get("training_evidence", [])
            evidence_count = len(evidence)

            if entity_rate == 0 and evidence_count == 0:
                hallucinations.append({
                    "study_id": study_id,
                    "amendment_number": amend.get("amendmentNumber"),
                    "change_number": ch.get("changeNumber"),
                    "usdm_entity": ch.get("usdmEntity"),
                    "scope": ch.get("scope", "Global"),
                    "confidence": ch.get("confidence"),
                    "reason": "entity_rate=0 AND training_evidence.count=0",
                })

    hallucination_rate = (
        round(len(hallucinations) / total_changes, 3) if total_changes else 0.0
    )
    target = config.get("metric_targets", {}).get("m3", 0.0)

    return {
        "m3": {
            "score": hallucination_rate,
            "target": target,
            "status": "PASS" if hallucination_rate == 0.0 else "FAIL — STOP",
            "hallucinations": hallucinations,
            "total_predicted_changes": total_changes,
        }
    }
