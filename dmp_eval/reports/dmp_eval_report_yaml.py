"""Two-document YAML: verbatim dmp_eval_config.yaml + dmp_eval_report."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def write_dmp_eval_report_yaml(
    output_path: Path,
    report: Dict[str, Any],
    *,
    config_source_path: Path,
    eval_date: str,
) -> None:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError("pip install pyyaml") from e

    cfg_text = config_source_path.read_text(encoding="utf-8").rstrip() + "\n"
    doc2 = yaml.safe_dump(
        {"dmp_eval_report": report, "scoring_note": "Section and attribute weights are in document 1 (attributes / sections / scoring)."},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    body = cfg_text + "\n---\n\n" + doc2
    output_path.write_text(body, encoding="utf-8")
