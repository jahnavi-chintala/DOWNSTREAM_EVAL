"""
Write ``eval_report_{study}_{date}.yaml``:
  document 1: raw ``risk_profile_eval_config.yaml``
  document 2: ``risk_profile_eval_report`` (full eval ``results`` dict).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

_PKG = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _PKG / "eval_config" / "risk_profile_eval_config.yaml"


def _config_source_text(config_path: Path | None) -> str:
    p = config_path if config_path is not None else _DEFAULT_CONFIG
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return (
        "# risk_profile_eval_config.yaml not found.\n"
        "# Expected: eval_config/risk_profile_eval_config.yaml\n"
    )


def build_risk_profile_eval_yaml(
    report: Dict[str, Any],
    *,
    config_path: Path | None = None,
) -> str:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError("pip install pyyaml") from e
    doc1 = _config_source_text(config_path).rstrip() + "\n"
    doc2 = yaml.safe_dump(
        {"risk_profile_eval_report": report},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return doc1 + "\n---\n\n" + doc2


def write_risk_profile_eval_report_yaml(
    output_path: Path,
    report: Dict[str, Any],
    *,
    config_path: Path | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_risk_profile_eval_yaml(report, config_path=config_path),
        encoding="utf-8",
    )
