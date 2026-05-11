"""Write ``eval_report_{study}_{date}.yaml`` — doc1: raw ``cmp_eval_config.yaml``; doc2: ``cmp_eval_report``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

_PKG = Path(__file__).resolve().parent.parent


def _config_source_text() -> str:
    p = _PKG / "config" / "cmp_eval_config.yaml"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return (
        "# cmp_eval_config.yaml not found.\n"
        "# Expected: config/cmp_eval_config.yaml\n"
    )


def build_cmp_eval_yaml(report: Dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError("pip install pyyaml") from e
    doc1 = _config_source_text().rstrip() + "\n"
    doc2 = yaml.safe_dump(
        {"cmp_eval_report": report},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return doc1 + "\n---\n\n" + doc2


def write_cmp_eval_report_yaml(output_path: Path, report: Dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_cmp_eval_yaml(report), encoding="utf-8")
