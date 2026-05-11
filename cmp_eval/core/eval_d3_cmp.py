"""
eval_d3_cmp.py — CMP Generator Evaluation Script
Usage:
    python eval_d3_cmp.py --input path/to/CMP.json [--study-id B7981027]
    python eval_d3_cmp.py --input-dir outputs/generated/ --all
    python eval_d3_cmp.py --verify-set --seed 42

Produces:
    Outputs/eval/eval_report_{study_id}_{date}.json
    Outputs/eval/eval_report_{study_id}_{date}.yaml  (config + report; unless --no-yaml)
    Outputs/eval/CMP_Eval_Report_{study_id}.docx  (unless --no-word)
    Outputs/eval/eval_summary.csv  (multi-study)
    Console: structured pass/fail report
"""

import argparse
import json
import os
import sys
import glob
import yaml
from pathlib import Path

# ── Local imports ─────────────────────────────────────────────────────────────
from core.structure_validator import validate_structure
from core.content_scorer import (
    GroundTruth, SectionScorer,
    calculate_m1_kri_recall, calculate_m2_threshold_accuracy,
    calculate_m3_qtl_recall, calculate_m4_hallucinations,
    calculate_document_score,
)
from core.report_generator import (
    build_eval_report, print_report, save_summary_csv
)


# ─── Default Paths ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "cmp_eval_config.yaml"
KRI_GT_PATH = BASE_DIR / "data" / "cmp_kri_ground_truth.csv"
QTL_GT_PATH = BASE_DIR / "data" / "cmp_qtl_ground_truth.csv"
STUDY_META_PATH = BASE_DIR / "data" / "cmp_study_metadata.csv"
OUTPUT_DIR = BASE_DIR / "Outputs" / "eval"


# ─── Core Eval Function ───────────────────────────────────────────────────────

def run_eval(
    cmp_json: dict,
    study_id: str,
    config: dict,
    ground_truth: GroundTruth,
    generator_version: str = "unknown",
    verbose: bool = True,
    artifact_paths: dict | None = None,
) -> dict:
    """
    Run the full evaluation pipeline on one CMP JSON.
    Returns the eval report dict.
    """

    # ── Step 1: Structure validation ──────────────────────────────────────────
    structure_result = validate_structure(cmp_json)

    if verbose:
        print(f"\n[{study_id}] Structure: {'PASS' if structure_result.passed else 'FAIL'} "
              f"({len(structure_result.errors)} errors, {len(structure_result.warnings)} warnings)")

    # ── Step 2: Content scoring ───────────────────────────────────────────────
    scorer = SectionScorer(config, ground_truth)

    section_results = {
        "global_kris": scorer.score_global_kris(study_id, cmp_json),
        "study_specific_kris": scorer.score_ss_kris(study_id, cmp_json),
        "qtls": scorer.score_qtls(study_id, cmp_json),
        "metadata": scorer.score_metadata(study_id, cmp_json),
    }

    if verbose:
        gk = section_results["global_kris"]
        ss = section_results["study_specific_kris"]
        qt = section_results["qtls"]
        print(f"[{study_id}] Global KRIs: {gk['matched_count']}/{gk['kri_count_gt']} matched "
              f"(score: {gk['section_score']:.1f})")
        print(f"[{study_id}] SS KRIs:     {ss['matched_count']}/{ss['kri_count_gt']} matched "
              f"(score: {ss['section_score']:.1f})")
        print(f"[{study_id}] QTLs:        {qt['matched_count']}/{qt['qtl_count_gt']} matched "
              f"(score: {qt['section_score']:.1f})")

    # ── Step 3: Compute metrics ───────────────────────────────────────────────
    metrics = {
        "m1": calculate_m1_kri_recall(section_results, config),
        "m2": calculate_m2_threshold_accuracy(section_results, config),
        "m3": calculate_m3_qtl_recall(section_results, config),
        "m4": calculate_m4_hallucinations(section_results, cmp_json, config),
    }

    # ── Step 4: Document score ────────────────────────────────────────────────
    doc_result = calculate_document_score(section_results, structure_result.score, config)

    study_meta_row = ground_truth.get_study_metadata_row(study_id)
    report = build_eval_report(
        study_id=study_id,
        cmp_json=cmp_json,
        structure_result=structure_result,
        section_results=section_results,
        metrics=metrics,
        document_score_result=doc_result,
        config=config,
        generator_version=generator_version,
        study_metadata_row=study_meta_row,
        artifact_paths=artifact_paths,
    )

    return report


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_cmp_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def infer_study_id(cmp_json: dict, filepath: str) -> str:
    """Extract study_id from JSON, ``metadata.study_id``, or filename."""
    sid = cmp_json.get("study_id")
    if sid:
        return str(sid).strip()
    meta = cmp_json.get("metadata")
    if isinstance(meta, dict) and meta.get("study_id"):
        return str(meta["study_id"]).strip()
    # Try filename: B7981027_CMP.json
    stem = Path(filepath).stem
    import re
    m = re.search(r'([BCb][0-9]{7})', stem)
    if m:
        return m.group(1)
    return stem


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _write_word_report_path(report: dict, output_path: Path) -> None:
    """Write CMP Word report to a concrete path (python-docx)."""
    try:
        from reports.cmp_report_docx import write_cmp_eval_docx
    except ImportError as e:
        print(f"  [SKIP] Word report needs python-docx: pip install python-docx ({e})")
        return
    from datetime import datetime

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_cmp_eval_docx(report, str(output_path))
        print(f"Word report: {output_path.resolve()}")
    except PermissionError:
        alt = output_path.parent / f"{output_path.stem}_{datetime.now():%Y%m%d_%H%M%S}{output_path.suffix}"
        write_cmp_eval_docx(report, str(alt))
        print(
            f"  [WARN] Could not overwrite locked file: {output_path.name}\n"
            f"  Word report (new file): {alt.resolve()}"
        )


def _write_word_report(report: dict, output_dir: str, study_id: str) -> None:
    """Write ``CMP_Eval_Report_{study_id}.docx`` (python-docx)."""
    out = Path(output_dir) / f"CMP_Eval_Report_{study_id}.docx"
    _write_word_report_path(report, out)


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate CMP Generator output against ground truth."
    )
    parser.add_argument("--input", type=str, help="Path to a single CMP JSON file")
    parser.add_argument("--input-dir", type=str, help="Directory of CMP JSON files to evaluate")
    parser.add_argument("--study-id", type=str, help="Override study_id (default: from JSON)")
    parser.add_argument("--all", action="store_true", help="Evaluate all JSON files in --input-dir")
    parser.add_argument("--verify-set", action="store_true", help="Run eval on verify set studies")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (for reproducibility)")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="Path to eval config YAML")
    parser.add_argument("--kri-gt", type=str, default=str(KRI_GT_PATH), help="Path to KRI ground truth CSV")
    parser.add_argument("--qtl-gt", type=str, default=str(QTL_GT_PATH), help="Path to QTL ground truth CSV")
    parser.add_argument(
        "--study-meta",
        type=str,
        default=str(STUDY_META_PATH),
        help="Path to cmp_study_metadata.csv (eval_metadata + protocol context)",
    )
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR), help="Output directory")
    parser.add_argument("--generator-version", type=str, default="1.0", help="Generator version string")
    parser.add_argument("--no-print", action="store_true", help="Suppress console report")
    parser.add_argument("--json-only", action="store_true", help="Output JSON only (no summary CSV)")
    parser.add_argument("--no-word", action="store_true", help="Skip Word doc generation")
    parser.add_argument("--no-yaml", action="store_true", help="Skip combined YAML artifact")
    parser.add_argument(
        "--artifact-stem",
        default=None,
        help="Single --input only: write {stem}.json/.yaml/.docx instead of dated eval_report_* names",
    )
    args = parser.parse_args()

    # ── Load config and ground truth ──────────────────────────────────────────
    print(f"Loading config: {args.config}")
    config = load_config(Path(args.config))

    print(f"Loading ground truth: {args.kri_gt} / {args.qtl_gt}")
    sm_path = args.study_meta if Path(args.study_meta).is_file() else None
    if not Path(args.study_meta).is_file():
        print(f"  [WARN] Study metadata not found: {args.study_meta} — eval_metadata will omit row-level CMP metadata")
    ground_truth = GroundTruth(args.kri_gt, args.qtl_gt, sm_path)

    # ── Collect files to evaluate ─────────────────────────────────────────────
    files_to_eval = []

    if args.input:
        files_to_eval.append(args.input)

    if args.input_dir:
        pattern = os.path.join(args.input_dir, "*.json")
        found = sorted(glob.glob(pattern))
        if not found:
            print(f"[WARNING] No JSON files found in {args.input_dir}")
        files_to_eval.extend(found)

    if args.verify_set:
        # Verify set: look for JSONs in outputs/generated/
        gen_dir = BASE_DIR / "outputs" / "generated"
        found = sorted(glob.glob(str(gen_dir / "*.json")))
        if not found:
            print(f"[WARNING] No CMP JSONs found in {gen_dir} for verify set.")
        files_to_eval.extend(found)

    if not files_to_eval:
        print("\n[ERROR] No CMP JSON files to evaluate.")
        print("Usage: python eval_d3_cmp.py --input path/to/CMP.json")
        print("       python eval_d3_cmp.py --input-dir outputs/generated/")
        sys.exit(1)

    use_artifact_stem = bool(args.artifact_stem and str(args.artifact_stem).strip())
    if use_artifact_stem and len(files_to_eval) > 1:
        print(
            "[WARN] --artifact-stem applies to a single --input only; ignoring stem for batch run",
            file=sys.stderr,
        )
        use_artifact_stem = False

    # ── Run evaluation ────────────────────────────────────────────────────────
    all_reports = []

    for filepath in files_to_eval:
        print(f"\n{'='*60}")
        print(f"Evaluating: {filepath}")
        try:
            cmp_json = load_cmp_json(filepath)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"[ERROR] Could not load {filepath}: {e}")
            continue

        study_id = args.study_id or infer_study_id(cmp_json, filepath)
        print(f"Study ID: {study_id}")

        artifact_paths = {
            "config": str(Path(args.config).resolve()),
            "kri_gt": str(Path(args.kri_gt).resolve()),
            "qtl_gt": str(Path(args.qtl_gt).resolve()),
            "study_meta": str(Path(args.study_meta).resolve()) if sm_path else "",
        }

        report = run_eval(
            cmp_json=cmp_json,
            study_id=study_id,
            config=config,
            ground_truth=ground_truth,
            generator_version=args.generator_version,
            verbose=True,
            artifact_paths=artifact_paths,
        )

        if not args.no_print:
            print_report(report)

        # Save individual report (canonical names match reference_specs/*.json|.yaml|.docx)
        out_dir_p = Path(args.output_dir)
        out_dir_p.mkdir(parents=True, exist_ok=True)
        if use_artifact_stem:
            stem = str(args.artifact_stem).strip()
            report_path = str(out_dir_p / f"{stem}.json")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"Report saved: {report_path}")
        else:
            report_path = str(out_dir_p / f"cmp_eval_{study_id}.json")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"Report saved: {report_path}")

        if not args.no_yaml:
            try:
                from reports.cmp_eval_report_yaml import write_cmp_eval_report_yaml

                if use_artifact_stem:
                    stem = str(args.artifact_stem).strip()
                    yp = out_dir_p / f"{stem}.yaml"
                else:
                    yp = out_dir_p / f"cmp_eval_{study_id}.yaml"
                write_cmp_eval_report_yaml(yp, report)
                print(f"YAML artifact: {yp.resolve()}")
            except Exception as exc:
                print(f"  [WARN] YAML artifact skipped: {exc}")

        if not args.no_word:
            if use_artifact_stem:
                stem = str(args.artifact_stem).strip()
                _write_word_report_path(report, out_dir_p / f"{stem}.docx")
            else:
                _write_word_report(report, args.output_dir, study_id)

        all_reports.append(report)

    # ── Multi-study summary ───────────────────────────────────────────────────
    if len(all_reports) > 1 and not args.json_only:
        csv_path = save_summary_csv(all_reports, args.output_dir)
        print(f"\nSummary CSV saved: {csv_path}")
        _print_multi_summary(all_reports)

    # ── Exit code ─────────────────────────────────────────────────────────────
    all_passed = all(r.get("document_passed", r.get("document_pass")) for r in all_reports)
    sys.exit(0 if all_passed else 1)


def _print_multi_summary(reports: list[dict]):
    """Print a brief multi-study summary table."""
    print(f"\n{'='*80}")
    print(f"  MULTI-STUDY EVAL SUMMARY ({len(reports)} studies)")
    print(f"{'='*80}")
    print(f"  {'STUDY':>12}  {'SCORE':>7}  {'PASS':>6}  {'M1':>6}  {'M2':>6}  {'M3':>6}  {'M4':>6}")
    print(f"  {'─'*70}")
    passed_count = 0
    for r in reports:
        sid = r.get("study_id", "?")
        score = r.get("document_score", 0)
        doc_ok = r.get("document_passed", r.get("document_pass"))
        passed = "✓" if doc_ok else "✗"
        if doc_ok:
            passed_count += 1
        m1 = r.get("metrics", {}).get("M1_kri_recall", {}).get("score_pct", "?")
        m2 = r.get("metrics", {}).get("M2_threshold_accuracy", {}).get("score_pct", "?")
        m3 = r.get("metrics", {}).get("M3_qtl_recall", {}).get("score_pct", "?")
        m4 = str(r.get("metrics", {}).get("M4_hallucinations", {}).get("score", "?"))
        print(f"  {sid:>12}  {score:>7.1f}  {passed:>6}  {m1:>6}  {m2:>6}  {m3:>6}  {m4:>6}")
    print(f"  {'─'*70}")
    print(f"  Passed: {passed_count}/{len(reports)}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
