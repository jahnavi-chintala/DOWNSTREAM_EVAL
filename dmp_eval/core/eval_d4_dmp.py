#!/usr/bin/env python3
"""
D4 DMP generator eval — black-box scoring vs ground truth.

Usage::
    python eval_d4_dmp.py --input B7981027_DMP.json --study-id B7981027 \\
        --output-dir eval_outputs/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG = Path(__file__).resolve().parent

from core.dmp_data import (
    infer_study_id,
    load_dmp_gt_record,
    load_dmp_json,
    load_sds_rows,
)
from core.report_builder import build_report, load_eval_config
from core.semantic_matcher import SemanticMatcher
from core.scorer_s5 import score_s5
from core.scorer_s6 import score_s6
from core.scorer_s8 import score_s8
from core.scorer_s11 import score_s11


def run_eval(
    input_path: str | Path,
    study_id: str | None,
    config_path: str | Path,
    dmp_gt_path: str | Path,
    sds_gt_path: str | Path,
    *,
    generator_version: str = "1.0",
    write_yaml: bool = True,
    write_word: bool = True,
    output_json: str | Path | None = None,
    output_dir: str | Path | None = None,
    artifact_stem: str | None = None,
) -> dict:
    dmp = load_dmp_json(input_path)
    sid = (study_id or infer_study_id(dmp, input_path)).strip().upper()
    cfg_path = Path(config_path)
    cfg = load_eval_config(cfg_path)

    gt_rec = load_dmp_gt_record(dmp_gt_path, sid)
    sem_cfg = (cfg.get("scoring") or {}).get("semantic") or {}
    matcher = SemanticMatcher(sem_cfg)

    sds_rows = load_sds_rows(sds_gt_path, sid)
    sds_available = bool(sds_rows)

    s5_rows, s5_meta = score_s5(dmp, gt_rec, cfg, matcher)
    s6_rows, s6_meta = score_s6(dmp, gt_rec, sds_rows, cfg, matcher, sid)
    s8_rows, s8_meta = score_s8(dmp, gt_rec, cfg, matcher)
    s11_rows, s11_meta = score_s11(dmp, gt_rec, cfg)

    gv = (
        str(dmp.get("generated_by") or "")
        + (f" — DMP v{dmp.get('dmp_version')}" if dmp.get("dmp_version") else "")
    ).strip() or str(dmp.get("dmp_version") or generator_version)
    report = build_report(
        sid,
        dmp,
        gt_rec,
        cfg,
        cfg_path,
        s5_rows,
        s5_meta,
        s6_rows,
        s6_meta,
        s8_rows,
        s8_meta,
        s11_rows,
        s11_meta,
        generator_version=gv,
        sds_available=sds_available,
    )

    out_dir = Path(output_dir or Path(input_path).parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (artifact_stem or "").strip() or f"dmp_eval_{sid}"
    json_path = Path(output_json) if output_json else out_dir / f"{stem}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] DMP eval JSON -> {json_path}")

    if write_yaml:
        try:
            from reports.dmp_eval_report_yaml import write_dmp_eval_report_yaml

            yp = out_dir / f"{stem}.yaml"
            ed = report.get("eval_metadata", {}).get("eval_date", "unknown")
            write_dmp_eval_report_yaml(yp, report, config_source_path=cfg_path, eval_date=ed)
            print(f"[INFO] DMP eval YAML -> {yp}")
        except Exception as exc:
            print(f"  [WARN] YAML skipped: {exc}", file=sys.stderr)

    if write_word:
        try:
            from reports.dmp_eval_report_docx import write_dmp_eval_docx

            dp = out_dir / f"DMP_Eval_Report_{sid}.docx"
            write_dmp_eval_docx(dp, report)
            print(f"[INFO] DMP eval Word -> {dp}")
        except Exception as exc:
            print(f"  [WARN] Word skipped: {exc}", file=sys.stderr)

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="D4 DMP eval — metrics M1–M4 + M4b hallucinations")
    ap.add_argument("--input", required=True, type=str, help="Path to generated *_DMP.json")
    ap.add_argument("--study-id", default=None, help="Study id (default: study_folder in JSON)")
    ap.add_argument(
        "--config",
        type=str,
        default=str(_PKG.parent / "config" / "dmp_eval_config.yaml"),
    )
    ap.add_argument(
        "--dmp-gt",
        type=str,
        default=str(_PKG.parent / "data" / "dmp_ground_truth_clean.json"),
    )
    ap.add_argument(
        "--sds-gt",
        type=str,
        default=str(_PKG.parent / "data" / "sds_non_crf_ground_truth_clean.csv"),
    )
    ap.add_argument("--output-dir", type=str, default=None)
    ap.add_argument("--output-json", type=str, default=None)
    ap.add_argument("--artifact-stem", type=str, default=None)
    ap.add_argument("--generator-version", type=str, default="1.0")
    ap.add_argument("--no-yaml", action="store_true")
    ap.add_argument("--no-word", action="store_true")
    args = ap.parse_args()

    try:
        report = run_eval(
            args.input,
            args.study_id,
            args.config,
            args.dmp_gt,
            args.sds_gt,
            generator_version=args.generator_version,
            write_yaml=not args.no_yaml,
            write_word=not args.no_word,
            output_json=args.output_json,
            output_dir=args.output_dir or str(Path(args.input).parent),
            artifact_stem=args.artifact_stem,
        )
        ok = bool(report.get("summary_metrics", {}).get("overall_pass"))
        return 0 if ok else 1
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
