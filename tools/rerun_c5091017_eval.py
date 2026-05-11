"""Re-run CMP Scenario 1 evaluation against packaged ground truth (per-study)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_CMP = _REPO / "cmp_eval"

sys.path.insert(0, str(_CMP))

from core.eval_d3_cmp import _write_word_report_path, run_eval  # noqa: E402
from core.content_scorer import GroundTruth  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run CMP eval for one study with fixed GT CSVs.")
    parser.add_argument("--study-id", required=True, help="Protocol id (e.g. C5091017).")
    parser.add_argument(
        "--generator-json",
        required=True,
        type=Path,
        help="Path to {study}_CMP generator JSON.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory where JSON / YAML / DOCX artefacts are written.",
    )
    parser.add_argument(
        "--kri-ground-truth",
        type=Path,
        default=_CMP / "data" / "cmp_kri_ground_truth.csv",
        help="CMP KRI ground-truth CSV.",
    )
    parser.add_argument(
        "--qtl-ground-truth",
        type=Path,
        default=_CMP / "data" / "cmp_qtl_ground_truth.csv",
        help="CMP QTL ground-truth CSV.",
    )
    parser.add_argument(
        "--study-metadata-csv",
        type=Path,
        default=_CMP / "data" / "cmp_study_metadata.csv",
        help="CMP study metadata CSV.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_CMP / "config" / "cmp_eval_config.yaml",
        help="cmp_eval_config.yaml path.",
    )
    args = parser.parse_args(argv)

    study_id = args.study_id.strip()
    cmp_json = json.loads(Path(args.generator_json).read_text(encoding="utf-8"))

    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    gt = GroundTruth(str(args.kri_ground_truth), str(args.qtl_ground_truth), str(args.study_metadata_csv))
    result = run_eval(cmp_json, study_id, config, gt, verbose=True)

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    json_out = out_dir / f"cmp_eval_{study_id}.json"
    json_out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Written: {json_out}")

    yaml_out = out_dir / f"cmp_eval_{study_id}.yaml"
    yaml_out.write_text(
        yaml.dump(result, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Written: {yaml_out}")

    docx_out = out_dir / f"cmp_eval_{study_id}.docx"
    _write_word_report_path(result, docx_out)

    print()
    doc_score = result.get("document_score", "?")
    print(f"Document Score: {doc_score}")

    metrics = result.get("metrics", {})
    for key, val in metrics.items():
        if isinstance(val, dict):
            score = val.get("score")
            target = val.get("target")
            passed = val.get("passed")
            print(f"  {key}: score={score}  target={target}  pass={passed}")

    sections = result.get("section_scores", {})
    for key, val in sections.items():
        if isinstance(val, dict):
            score = val.get("score")
            matched = val.get("matched")
            gt_cnt = val.get("ground_truth_count")
            gen_cnt = val.get("generated_count")
            print(f"  section {key}: score={score}  matched={matched}  gt={gt_cnt}  gen={gen_cnt}")


if __name__ == "__main__":
    main()
