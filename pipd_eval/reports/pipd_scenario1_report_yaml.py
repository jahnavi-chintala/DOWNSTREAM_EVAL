"""
Write ``{study_id}_scenario1_report.yaml`` as two YAML documents:

  1. PIPD eval configuration — **verbatim** from ``pipd_eval_config.yaml`` (or
     ``eval_config/reference.yaml``), starting with the standard
     ``# PIPD Eval Configuration`` header—no extra wrapper comments.
  2. After ``---``, ``scenario1_report`` — full report dict (same as the JSON).

Consumers can ``yaml.safe_load_all`` to read both documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

_PKG_DIR = Path(__file__).resolve().parent


def _resolve_eval_config_source() -> str:
    """Raw eval-config YAML text; preserves comments and formatting from disk."""
    from utils.pipd_eval_config import resolve_eval_config_path

    p = resolve_eval_config_path()
    if p.is_file():
        return p.read_text(encoding="utf-8")

    ref = _PKG_DIR / "eval_config" / "reference.yaml"
    if ref.is_file():
        return ref.read_text(encoding="utf-8")

    return (
        "# Eval configuration YAML not found.\n"
        "# Expected: eval_config/pipd_eval_config.yaml (see pipd_eval_config.resolve_eval_config_path)\n"
        "#     or:  eval_config/reference.yaml\n"
    )


def build_combined_scenario1_yaml(result: Dict[str, Any]) -> str:
    """
    Document 1 — eval config exactly as in ``pipd_eval_config.yaml`` (no wrapper).
    Document 2 — ``scenario1_report`` (same tree as ``{study}_scenario1_report.json``).
    """
    cfg_text = _resolve_eval_config_source()
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError("Writing report YAML requires: pip install pyyaml") from e

    doc1 = cfg_text.rstrip() + "\n"
    doc2 = yaml.safe_dump(
        {"scenario1_report": result},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return doc1 + "\n---\n\n" + doc2


def write_scenario1_report_yaml(output_path: Path, result: Dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_combined_scenario1_yaml(result), encoding="utf-8")
