"""
M4 — Lineage Completeness scorer.

Every predicted change must have a fully populated lineage block with all 13
required fields non-null.  training_evidence[*].study_folder must NOT appear
in the verify set list (data contamination check).
"""

from __future__ import annotations

from typing import Any

REQUIRED_FIELDS = [
    "lineage.usdm_signal.entity",
    "lineage.usdm_signal.field",
    "lineage.usdm_signal.value",
    "lineage.usdm_signal.extraction_path",
    "lineage.yaml_match.benchmark_key",
    "lineage.yaml_match.benchmark_value",
    "lineage.yaml_match.yaml_file",
    "lineage.training_evidence",
    "lineage.confidence_basis.entity_rate",
    "lineage.confidence_basis.training_study_count",
    "lineage.confidence_basis.confidence_interval_note",
]

EVIDENCE_FIELDS = [
    "study_folder",
    "match_strength",
]


def _resolve(obj: dict, dotpath: str) -> Any:
    """Walk a dot-notation path into a nested dict."""
    parts = dotpath.replace("lineage.", "", 1).split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _is_valid(value: Any, dotpath: str) -> bool:
    """Check that a field is non-null and satisfies its specific rule."""
    if value is None:
        return False

    if dotpath == "lineage.usdm_signal.value":
        if isinstance(value, str) and (value.strip() == "" or value.strip() == "[VALUE]"):
            return False

    if dotpath == "lineage.yaml_match.benchmark_value":
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    if dotpath == "lineage.yaml_match.yaml_file":
        return value == "amendment_benchmarks.yaml"

    if dotpath == "lineage.training_evidence":
        return isinstance(value, list) and len(value) >= 1

    if dotpath == "lineage.confidence_basis.entity_rate":
        try:
            v = float(value)
            return 0 <= v <= 1
        except (TypeError, ValueError):
            return False

    if dotpath == "lineage.confidence_basis.training_study_count":
        try:
            return int(value) >= 1
        except (TypeError, ValueError):
            return False

    if isinstance(value, str):
        return value.strip() != ""

    return True


def score_m4(
    generated_json: dict,
    study_id: str,
    config: dict,
) -> dict[str, Any]:
    """Validate lineage completeness and verify-set contamination."""
    verify_set = set(config.get("verify_set", []))
    allowed_entities = set(config.get("allowed_usdm_entities", []))
    allowed_match_strengths = {"Direct", "Indirect"}

    total_changes = 0
    compliant = 0
    lineage_gaps: list[dict] = []
    contamination_issues: list[dict] = []

    for amend in generated_json.get("predictedAmendments", []):
        for ch in amend.get("predictedChanges", []):
            total_changes += 1
            lineage = ch.get("lineage", {})
            change_id = (
                f"Amend {amend.get('amendmentNumber')} "
                f"Ch{ch.get('changeNumber')}"
            )
            gaps_for_change: list[str] = []

            for field_path in REQUIRED_FIELDS:
                val = _resolve(lineage, field_path)
                if not _is_valid(val, field_path):
                    gaps_for_change.append(field_path)

            entity_val = _resolve(lineage, "lineage.usdm_signal.entity")
            if entity_val and entity_val not in allowed_entities:
                gaps_for_change.append(
                    f"lineage.usdm_signal.entity invalid: {entity_val}"
                )

            evidence_list = lineage.get("training_evidence", [])
            for idx, ev in enumerate(evidence_list):
                folder = ev.get("study_folder")
                if folder and folder in verify_set:
                    contamination_issues.append({
                        "change": change_id,
                        "study_id": study_id,
                        "evidence_index": idx,
                        "contaminated_folder": folder,
                    })
                strength = ev.get("match_strength")
                if strength and strength not in allowed_match_strengths:
                    gaps_for_change.append(
                        f"training_evidence[{idx}].match_strength "
                        f"invalid: {strength}"
                    )
                if not ev.get("study_folder"):
                    gaps_for_change.append(
                        f"training_evidence[{idx}].study_folder missing"
                    )

            if not gaps_for_change and not any(
                c["change"] == change_id for c in contamination_issues
            ):
                compliant += 1
            elif gaps_for_change:
                lineage_gaps.append({
                    "change": change_id,
                    "study_id": study_id,
                    "missing_fields": gaps_for_change,
                })

    target = config.get("metric_targets", {}).get("m4", 1.0)
    score = round(compliant / total_changes, 3) if total_changes else 0.0

    is_contaminated = len(contamination_issues) > 0
    status = "PASS"
    if score < target:
        status = "FAIL — STOP"
    if is_contaminated:
        status = "FAIL — STOP (verify-set contamination)"

    return {
        "m4": {
            "score": score,
            "target": target,
            "status": status,
            "compliant_changes": compliant,
            "total_changes": total_changes,
            "lineage_gaps": lineage_gaps,
            "contamination_issues": contamination_issues,
        }
    }
