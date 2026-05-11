#!/usr/bin/env python3
"""
Build a standalone **intelligence truth** report (Markdown / optional DOCX).

Uses the same logic as the composite pipeline: PIPD generator JSON as the intelligence
output, ground-truth CSV + composite alignment for recall, USDM protocol JSON for
entity traceability.

Example::

    python pipd_intelligence_truth_report.py \\
        --generator_json data/B7981027_PIPD.json \\
        --ground_truth data/pipd_ground_truth.csv \\
        --study_id B7981027 \\
        --output_dir eval_outputs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.eval_scenario1 import run_scenario1_eval
from core.pipd_composite_eval import run_composite_eval
from core.pipd_usdm_support import build_intelligence_truth_markdown, build_usdm_and_truth_block
from reports.pipd_markdown_to_docx import write_docx_from_markdown


def main() -> None:
    p = argparse.ArgumentParser(description="PIPD intelligence output truth verification report")
    p.add_argument("--generator_json", required=True)
    p.add_argument("--ground_truth", required=True)
    p.add_argument("--study_id", required=True)
    p.add_argument("--output_dir", default="eval_outputs")
    p.add_argument("--no-docx", action="store_true")
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    result = run_composite_eval(
        args.generator_json,
        args.ground_truth,
        args.study_id,
        use_bertscore=False,
    )
    result["scenario1_evaluation"] = run_scenario1_eval(
        args.generator_json,
        args.ground_truth,
        args.study_id,
    )
    bundle = build_usdm_and_truth_block(
        args.generator_json,
        args.study_id,
        result,
    )
    md = build_intelligence_truth_markdown(args.study_id, bundle)
    md_path = out / f"{args.study_id}_intelligence_truth.md"
    md_path.write_text(md, encoding="utf-8")
    print(json.dumps({"markdown": str(md_path)}, indent=2))

    if not args.no_docx:
        docx_path = out / f"{args.study_id}_intelligence_truth.docx"
        try:
            write_docx_from_markdown(md, str(docx_path))
            print(json.dumps({"docx": str(docx_path)}, indent=2))
        except Exception as exc:
            print(json.dumps({"docx_error": str(exc)}, indent=2))


if __name__ == "__main__":
    main()
