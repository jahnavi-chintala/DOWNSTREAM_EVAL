#!/usr/bin/env python3
"""
D5 Protocol Amendment Profile — Black-Box Evaluation Framework
==============================================================

Main entry point.  Orchestrates M1–M5 scorers, writes structured JSON,
Word report, and aggregate CSV for the verify set.

Usage:
    # Single study
    python eval_d5_amendment.py \
        --study B7981027 \
        --generated-json outputs/B7981027_AmendmentProfile.json \
        --config eval_config.yaml \
        --output-dir eval_outputs/

    # Full verify set
    python eval_d5_amendment.py \
        --verify-set \
        --generated-dir outputs/verify_set/ \
        --config eval_config.yaml \
        --output-dir eval_outputs/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from scorer_domain_recall import score_m1, score_m5
from scorer_category import score_m2
from scorer_hallucination import score_m3
from scorer_lineage import score_m4
from report_builder import build_report


def load_config(config_path: str | Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw.get("eval_d5", raw)


def load_generated_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_gt_path(config: dict, key: str, config_dir: Path) -> Path:
    """Resolve a ground-truth path relative to the config file location."""
    raw = config.get("ground_truth", {}).get(key, "")
    p = Path(raw)
    if p.is_absolute():
        return p
    return config_dir / p


def evaluate_study(
    study_id: str,
    generated_json: dict,
    config: dict,
    config_dir: Path,
    output_dir: Path,
) -> dict:
    """Run all applicable scorers for a single study."""
    start = time.time()
    is_primary = study_id == "B7981027"

    result: dict[str, Any] = {
        "study_id": study_id,
        "eval_date": datetime.now().strftime("%Y-%m-%d"),
        "generator_version": generated_json.get("generatorVersion", "unknown"),
        "eval_framework_version": "1.0",
        "overall_status": "PENDING",
        "metrics": {},
    }

    # --- M3 (all studies) — run first: hard-fail gate ---
    m3_result = score_m3(generated_json, study_id, config)
    result["metrics"]["m3"] = m3_result["m3"]

    if m3_result["m3"]["score"] > 0:
        result["overall_status"] = "FAIL — STOP (M3 hallucination detected)"
        result["metrics"]["m3"]["status"] = "FAIL — STOP"
        _write_outputs(result, output_dir, study_id)
        return result

    # --- M4 (all studies) — hard-fail gate ---
    m4_result = score_m4(generated_json, study_id, config)
    result["metrics"]["m4"] = m4_result["m4"]

    if m4_result["m4"]["score"] < 1.0 or m4_result["m4"]["contamination_issues"]:
        result["overall_status"] = f"FAIL — STOP ({m4_result['m4']['status']})"
        _write_outputs(result, output_dir, study_id)
        return result

    # --- M1/M2/M5 (B7981027 only — ground truth available) ---
    if is_primary:
        gt_json_path = resolve_gt_path(config, "b7981027_amendment4_json", config_dir)
        header_csv_path = resolve_gt_path(
            config, "amendment_header_csv", config_dir
        )

        m1_result = score_m1(generated_json, gt_json_path, config)
        result["metrics"]["m1_global"] = m1_result["m1_global"]
        result["metrics"]["m1_country"] = m1_result["m1_country"]
        result["m1_global_detail"] = m1_result["m1_global_detail"]
        result["m1_country_detail"] = m1_result["m1_country_detail"]

        # M1 global hard-fail check
        hard_threshold = config.get("hard_fail_threshold_m1", 0.40)
        if (
            result["metrics"]["m1_global"]["score"] is not None
            and result["metrics"]["m1_global"]["score"] < hard_threshold
        ):
            result["overall_status"] = (
                f"FAIL — STOP (M1_global {result['metrics']['m1_global']['score']}"
                f" < {hard_threshold})"
            )
            _write_outputs(result, output_dir, study_id)
            return result

        m2_result = score_m2(
            generated_json, header_csv_path, study_id, config
        )
        result["metrics"]["m2"] = m2_result["m2"]

        m5_result = score_m5(generated_json, gt_json_path, config)
        result["metrics"]["m5"] = m5_result["m5"]

        result["ground_truth_source"] = (
            "B7981027 Amendment 4 Summary of Change PDF (07 Oct 2025)"
        )

        result["analysis"] = _build_analysis(result)
    else:
        result["ground_truth_source"] = (
            f"No amendment summary PDFs available for {study_id}"
        )
        result["analysis"] = {
            "note": (
                f"M1/M2/M5 not evaluated — no ground truth for {study_id}. "
                "M3 and M4 evaluated on generated JSON only."
            )
        }

    # --- Per-change scores ---
    result["per_change_scores"] = _build_per_change(generated_json, study_id, config)

    # --- Overall status ---
    if result["overall_status"] == "PENDING":
        result["overall_status"] = _compute_overall(result, config)

    elapsed = round(time.time() - start, 2)
    result["eval_runtime_seconds"] = elapsed

    _write_outputs(result, output_dir, study_id)
    return result


def _build_per_change(
    generated_json: dict, study_id: str, config: dict,
) -> list[dict]:
    """Build per-change score list."""
    verify_set = set(config.get("verify_set", []))
    rows = []
    for amend in generated_json.get("predictedAmendments", []):
        for ch in amend.get("predictedChanges", []):
            lineage = ch.get("lineage", {})
            cb = lineage.get("confidence_basis", {})
            entity_rate = cb.get("entity_rate", 0)
            evidence = lineage.get("training_evidence", [])

            is_hallucination = entity_rate == 0 and len(evidence) == 0

            contaminated = any(
                ev.get("study_folder") in verify_set for ev in evidence
            )

            rows.append({
                "amendment_number": amend.get("amendmentNumber"),
                "change_number": ch.get("changeNumber"),
                "usdm_entity": ch.get("usdmEntity"),
                "scope": ch.get("scope", "Global"),
                "language_pattern": ch.get("languagePattern"),
                "confidence": ch.get("confidence"),
                "lineage_complete": not contaminated and _lineage_complete(lineage, config),
                "is_hallucination": is_hallucination,
            })
    return rows


def _lineage_complete(lineage: dict, config: dict) -> bool:
    """Quick check that all required lineage fields are present."""
    checks = [
        lineage.get("usdm_signal", {}).get("entity"),
        lineage.get("usdm_signal", {}).get("field"),
        lineage.get("usdm_signal", {}).get("value"),
        lineage.get("usdm_signal", {}).get("extraction_path"),
        lineage.get("yaml_match", {}).get("benchmark_key"),
        lineage.get("yaml_match", {}).get("benchmark_value"),
        lineage.get("yaml_match", {}).get("yaml_file"),
        lineage.get("confidence_basis", {}).get("entity_rate"),
        lineage.get("confidence_basis", {}).get("training_study_count"),
        lineage.get("confidence_basis", {}).get("confidence_interval_note"),
    ]
    evidence = lineage.get("training_evidence", [])
    if not evidence:
        return False
    for ev in evidence:
        if not ev.get("study_folder") or not ev.get("match_strength"):
            return False
    return all(v is not None for v in checks)


def _build_analysis(result: dict) -> dict:
    """Build analysis block for primary study."""
    metrics = result.get("metrics", {})

    m1_global_missed = [
        r["description"]
        for r in result.get("m1_global_detail", [])
        if not r.get("matched")
    ]
    m1_country_missed = [
        r["description"]
        for r in result.get("m1_country_detail", [])
        if not r.get("matched")
    ]

    all_pass = all(
        metrics.get(k, {}).get("status", "").startswith("PASS")
        for k in ["m1_global", "m1_country", "m2", "m3", "m4"]
    )
    m5_status = metrics.get("m5", {}).get("status", "N/A")

    if all_pass:
        go_text = (
            f"GO — all hard-fail gates cleared. "
            f"M3={metrics['m3']['score']:.1f} (no hallucinations), "
            f"M4={metrics['m4']['score']:.1f} (all lineage complete), "
            f"M1_global={metrics['m1_global']['score']:.2f} "
            f"(above {metrics['m1_global']['target']:.2f} target)."
        )
    else:
        failed = [
            k for k in ["m1_global", "m1_country", "m2", "m3", "m4"]
            if not metrics.get(k, {}).get("status", "").startswith("PASS")
        ]
        go_text = f"NO-GO — failed metrics: {', '.join(failed)}"

    improvement_actions = []
    if m1_global_missed:
        improvement_actions.append({
            "priority": "LOW",
            "action": (
                "Investigate missed global entities: "
                + "; ".join(m1_global_missed[:3])
            ),
            "impact": "M1 global improvement",
        })
    if m5_status and "NEAR" in m5_status.upper():
        improvement_actions.append({
            "priority": "LOW",
            "action": "Review confidence calibration algorithm",
            "impact": "M5 calibration improvement",
        })

    return {
        "m1_global_missed": m1_global_missed,
        "m1_country_missed": m1_country_missed,
        "go_no_go": go_text,
        "improvement_actions": improvement_actions,
    }


def _compute_overall(result: dict, config: dict) -> str:
    """Compute overall PASS/FAIL status."""
    metrics = result.get("metrics", {})
    hard_fail_metrics = config.get("hard_fail_metrics", ["m3", "m4"])

    for hf in hard_fail_metrics:
        m = metrics.get(hf, {})
        if "FAIL" in m.get("status", ""):
            return f"FAIL — {hf} triggered STOP"

    for key in ["m1_global", "m1_country", "m2"]:
        m = metrics.get(key, {})
        if m.get("score") is not None and "FAIL" in m.get("status", ""):
            return f"FAIL — {key} below target"

    return "PASS"


def _write_outputs(result: dict, output_dir: Path, study_id: str):
    """Write eval JSON and Word report."""
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"d5_eval_{study_id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  [JSON] {json_path}")

    try:
        report_path = output_dir / f"D5_Eval_Report_{study_id}.docx"
        build_report(result, report_path)
        print(f"  [DOCX] {report_path}")
    except Exception as e:
        print(f"  [WARN] Report generation failed: {e}")


def run_verify_set(config: dict, config_dir: Path, generated_dir: Path, output_dir: Path):
    """Run eval on all verify-set studies."""
    verify_studies = config.get("verify_set", [])
    results = []
    stop_triggered = False

    for study_id in verify_studies:
        print(f"\n{'='*60}")
        print(f"Evaluating: {study_id}")
        print(f"{'='*60}")

        json_file = find_generated_json(generated_dir, study_id)
        if json_file is None:
            print(f"  [SKIP] No generated JSON found for {study_id}")
            results.append({
                "study_id": study_id,
                "status": "SKIPPED",
                "note": "No generated JSON found",
            })
            continue

        generated_json = load_generated_json(json_file)
        result = evaluate_study(
            study_id, generated_json, config, config_dir, output_dir
        )
        results.append(result)

        if "STOP" in result.get("overall_status", ""):
            stop_triggered = True
            print(f"\n  *** STOP TRIGGERED: {result['overall_status']} ***")

    _write_verify_csv(results, output_dir)

    print(f"\n{'='*60}")
    if stop_triggered:
        print("VERIFY SET RESULT: STOP — hard-fail gate triggered")
    else:
        all_pass = all(
            r.get("overall_status", "").startswith("PASS")
            for r in results
            if r.get("status") != "SKIPPED"
        )
        print(f"VERIFY SET RESULT: {'GO' if all_pass else 'NO-GO'}")
    print(f"{'='*60}")

    return results


def find_generated_json(directory: Path, study_id: str) -> Path | None:
    """Find the generated AmendmentProfile JSON for a study."""
    patterns = [
        f"{study_id}_AmendmentProfile.json",
        f"{study_id}_amendment_profile.json",
        f"{study_id}.json",
    ]
    for pattern in patterns:
        candidate = directory / pattern
        if candidate.exists():
            return candidate
    for f in directory.glob(f"{study_id}*Profile*.json"):
        return f
    for f in directory.glob(f"{study_id}*.json"):
        return f
    return None


def _write_verify_csv(results: list[dict], output_dir: Path):
    """Write aggregate verify-set CSV."""
    csv_path = output_dir / "d5_eval_verify_set.csv"
    fieldnames = [
        "study_id", "m1_global", "m1_country", "m2", "m3", "m4", "m5",
        "overall_status", "notes",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in results:
            if r.get("status") == "SKIPPED":
                writer.writerow({
                    "study_id": r["study_id"],
                    "overall_status": "SKIPPED",
                    "notes": r.get("note", ""),
                })
                continue

            metrics = r.get("metrics", {})
            writer.writerow({
                "study_id": r.get("study_id", ""),
                "m1_global": _fmt(metrics.get("m1_global", {}).get("score")),
                "m1_country": _fmt(metrics.get("m1_country", {}).get("score")),
                "m2": _fmt(metrics.get("m2", {}).get("score")),
                "m3": _fmt(metrics.get("m3", {}).get("score")),
                "m4": _fmt(metrics.get("m4", {}).get("score")),
                "m5": _fmt(metrics.get("m5", {}).get("score")),
                "overall_status": r.get("overall_status", ""),
                "notes": r.get("analysis", {}).get("go_no_go", ""),
            })

    print(f"\n  [CSV] {csv_path}")


def _fmt(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val:.3f}"


def main():
    parser = argparse.ArgumentParser(
        description="D5 Protocol Amendment Profile Eval Framework"
    )
    parser.add_argument("--study", type=str, help="Single study ID to evaluate")
    parser.add_argument(
        "--generated-json", type=str,
        help="Path to generated AmendmentProfile JSON (single study mode)",
    )
    parser.add_argument("--config", type=str, required=True, help="eval_config.yaml")
    parser.add_argument("--output-dir", type=str, required=True, help="Output directory")
    parser.add_argument(
        "--verify-set", action="store_true",
        help="Run on full verify set",
    )
    parser.add_argument(
        "--generated-dir", type=str,
        help="Directory containing generated JSONs (verify-set mode)",
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    config_dir = config_path.parent
    output_dir = Path(args.output_dir)

    print("D5 Protocol Amendment Profile — Eval Framework v1.0")
    print(f"Config: {config_path}")
    print(f"Output: {output_dir}")

    if args.verify_set:
        if not args.generated_dir:
            parser.error("--generated-dir required with --verify-set")
        run_verify_set(config, config_dir, Path(args.generated_dir), output_dir)
    elif args.study:
        if not args.generated_json:
            parser.error("--generated-json required with --study")
        generated_json = load_generated_json(args.generated_json)
        evaluate_study(args.study, generated_json, config, config_dir, output_dir)
    else:
        parser.error("Specify --study or --verify-set")


if __name__ == "__main__":
    main()
