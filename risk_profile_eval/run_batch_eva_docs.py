"""
Batch Risk Profile eval: writes **only** three artifacts per study
that has a generator JSON:

  • {stem}.json   — full eval result
  • {stem}.yaml   — config + report bundle
  • Risk_Profile_Eval_Report_{study_id}.docx

No CSV, no near_misses, no separate hallucination_report.json
(near-miss / hallucination data remain inside the main JSON).

Used by run_verify_downloads.ps1 with output_dir = Downloads/eval_docs/risk_profile.
Each study is written under ``{output_dir}/{study_id}/`` (json, yaml, docx).

By default, **all** Risk Profile JSON files under ``--generator_output_dir`` are
discovered (one best file per protocol id). Use ``--verify-set-only`` for the
legacy fixed verify list only.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from eval_scenario1 import VERIFY_STUDIES
from run_all_verify import discover_risk_profile_generators, find_generator_json
from run_eval import run_eval


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch eval: json + yaml + docx only (no supplementary files)"
    )
    parser.add_argument("--generator_output_dir", required=True)
    parser.add_argument("--ground_truth_risks", required=True)
    parser.add_argument("--ground_truth_factors", required=True)
    parser.add_argument(
        "--output_dir",
        required=True,
        help="Parent folder (e.g. .../eva_docs/risk_profile); each study -> {output_dir}/{study_id}/",
    )
    parser.add_argument(
        "--verify-set-only",
        action="store_true",
        help="Only run studies in VERIFY_STUDIES (legacy 8-study batch order).",
    )
    parser.add_argument(
        "--study-ids",
        default="",
        help="Comma-separated protocol IDs (overrides discovery; still uses find_generator_json per id).",
    )
    args = parser.parse_args()

    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)

    if args.study_ids.strip():
        ids = [x.strip() for x in args.study_ids.split(",") if x.strip()]
        planned = [(sid, find_generator_json(sid, args.generator_output_dir)) for sid in ids]
    elif args.verify_set_only:
        planned = [
            (sid, find_generator_json(sid, args.generator_output_dir))
            for sid in VERIFY_STUDIES
        ]
    else:
        planned = discover_risk_profile_generators(args.generator_output_dir)

    if not planned:
        print("  [WARN] No Risk Profile JSON candidates found under generator_output_dir.")
        return

    for study_id, json_path in planned:
        study_out = root / study_id
        study_out.mkdir(parents=True, exist_ok=True)
        if not json_path:
            skip = study_out / "SKIPPED_no_risk_profile_json.txt"
            skip.write_text(
                "No Risk Profile generator JSON was found under the input tree.\n"
                f"Protocol id: {study_id}\n"
                f"Searched: {args.generator_output_dir}\n"
                "Expected a *RiskProfile*.json (or JSON with Risk Profile shape) whose name or "
                "content includes this protocol id.\n",
                encoding="utf-8",
            )
            print(f"  [{study_id}] skipped (no Risk Profile JSON) -> {skip.name}")
            continue
        print(f"  [{study_id}] -> {study_out}")
        run_eval(
            generator_json=json_path,
            ground_truth_risks_csv=args.ground_truth_risks,
            ground_truth_factors_csv=args.ground_truth_factors,
            study_id=study_id,
            output_dir=str(study_out),
            write_supplementary=False,
            force_scenario1_when_no_gt=True,
        )


if __name__ == "__main__":
    main()
