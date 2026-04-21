"""Build ``eval_metadata`` for Risk Profile eval JSON / YAML / Word (mirrors CMP pattern)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def build_eval_metadata(
    results: Dict[str, Any],
    config: Dict[str, Any],
    config_path: Path,
    artifact_paths: Dict[str, str],
) -> Dict[str, Any]:
    """
    Assemble eval provenance: config version, ground-truth filenames, optional Word form reference.

    ``artifact_paths`` keys: config, generator, risks, factors.
    """
    ts = results.get("timestamp") or ""
    eval_date = (
        ts[:10]
        if isinstance(ts, str) and len(ts) >= 10
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    study_id = results.get("study_id", "")

    rel_form = (config.get("reference_artifacts") or {}).get("risk_profile_form_docx")
    form_meta: Dict[str, Any] = {}
    if rel_form:
        form_abs = config_path.parent / str(rel_form).strip()
        form_meta["risk_profile_form_reference"] = form_abs.name
        form_meta["risk_profile_form_resolved_path"] = str(form_abs.resolve())
        form_meta["risk_profile_form_present"] = form_abs.is_file()

    risks_p = artifact_paths.get("risks") or ""
    factors_p = artifact_paths.get("factors") or ""
    gen_p = artifact_paths.get("generator") or ""

    scoring = config.get("scoring") or {}
    oos = list(scoring.get("out_of_scope_exclusions", []))

    return {
        "study_id": study_id,
        "scenario": results.get("scenario"),
        "eval_date": eval_date,
        "eval_timestamp_utc": ts,
        "config_version": str(scoring.get("config_version", "1.0")),
        "config_file": config_path.name,
        "generator_version": results.get("generator_version", "unknown"),
        "ta": results.get("ta"),
        "phase": results.get("phase"),
        "ground_truth_sources": [
            Path(risks_p).name if risks_p else "risk_profile_ground_truth.csv",
            Path(factors_p).name if factors_p else "critical_factors_ground_truth.csv",
        ],
        "generator_json_reference": Path(gen_p).name if gen_p else None,
        "generator_json_path": str(Path(gen_p).resolve()) if gen_p and Path(gen_p).is_file() else None,
        "risk_ground_truth_csv_path": str(Path(risks_p).resolve()) if risks_p and Path(risks_p).is_file() else None,
        "critical_factors_ground_truth_csv_path": str(Path(factors_p).resolve()) if factors_p and Path(factors_p).is_file() else None,
        "out_of_scope_exclusions": oos,
        **form_meta,
    }
