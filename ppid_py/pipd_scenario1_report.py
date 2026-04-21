"""
Scenario 1 only — reference-style Markdown + Word (no weighted composite).

Use when you have ground-truth CSV + PIPD JSON and want the same report layout as
``pipd_composite_report`` §1–§5 without running ``pipd_composite_eval``.

Optional **OpenAI reference layout** (``--openai-reference-layout`` or
``PIPD_OPENAI_REFERENCE_LAYOUT=1``): restructures the Markdown to mirror
``reference_spec/PIPD_Eval_Report_B7981027.docx``; all metrics still come from
the rule-based evaluator. Requires ``OPENAI_API_KEY``.

Optional **OpenAI source enrichment** (``--openai-enrich-sources`` or
``PIPD_OPENAI_REPORT_ENRICH=1``): appends a section grounded in USDM protocol JSON
and ground-truth CSV excerpts (fields not carried in PIPD JSON). Does not change scores.

Optional **§4 remediation summary** (``PIPD_IMPROVEMENT_ACTIONS_OPENAI=1`` + ``OPENAI_API_KEY``):
adds a short narrative comparing GT vs generated for missed lines and near-miss pairs; the
structured table still lists typed failures.

Example::

    python pipd_scenario1_report.py \\
        --generator_json data/B7981027_PIPD.json \\
        --ground_truth data/pipd_ground_truth.csv \\
        --study_id B7981027 \\
        --output_dir eval_outputs
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from eval_scenario1 import classify_failures, run_scenario1_eval
from pipd_composite_report import _PKG_DIR
from pipd_eval_charts import try_write_eval_charts, write_score_per_category_chart
from pipd_eval_report_reference import build_reference_eval_markdown, load_generator_report_meta
from pipd_openai_report_enrichment import append_usdm_gt_sourced_section, fill_gsop_codes_with_openai
from pipd_markdown_to_docx import write_docx_from_markdown
from pipd_structure_completeness import check_pipd_json
from pipd_scenario1_report_yaml import write_scenario1_report_yaml
from pipd_eval_config import category_weights_and_names, load_pipd_eval_config, resolve_eval_config_path
from pipd_usdm_support import build_intelligence_truth_markdown, build_usdm_and_truth_block

load_dotenv(_PKG_DIR / ".env")


def _synthetic_per_category_from_s1(s1: Dict[str, Any]) -> Dict[str, Any]:
    """Build composite-shaped ``per_category`` rows from Scenario 1 matched / extra lists."""
    out: Dict[str, Any] = {}
    raw = s1.get("per_category") or {}
    for key, block in raw.items():
        try:
            cn = int(key)
        except (TypeError, ValueError):
            continue
        rows: List[Dict[str, Any]] = []
        for t in block.get("matched_subcats") or []:
            rows.append(
                {
                    "ground_truth": t,
                    "generated": t,
                    "present": True,
                    "exact": True,
                    "semantic_f1": None,
                    "hallucination": False,
                }
            )
        for t in block.get("hallucinated_subcats") or []:
            rows.append(
                {
                    "ground_truth": "",
                    "generated": t,
                    "present": False,
                    "exact": False,
                    "hallucination": True,
                }
            )
        out[str(cn)] = {"category_num": cn, "rows": rows}
    return out


def build_scenario1_report_payload(
    s1: Dict[str, Any],
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    *,
    usdm_json_path: str | None = None,
    include_structure_check: bool = True,
) -> Dict[str, Any]:
    """
    Build the report dict used for reference Markdown / YAML / Word (Scenario 1).
    ``s1`` must be the scenario-1 results dict (typically includes ``classified_failures``).
    """
    # Use M1 subcategory recall as the headline overall score for scenario1_only
    _m1_score = float((s1.get("metrics") or {}).get("m1_subcategory_recall", {}).get("score") or 0.0)
    _overall_pct = round(_m1_score * 100, 1)

    result: Dict[str, Any] = {
        "study_id": study_id,
        "eval_date": datetime.now().isoformat(),
        "eval_report_mode": "scenario1_only",
        "scenario1_evaluation": s1,
        "scenario": 1,
        "overall_score_percent": _overall_pct,
        "generator_path": str(Path(generator_json_path).resolve()),
        "ground_truth_path": str(Path(ground_truth_csv_path).resolve()),
        "report_metadata": load_generator_report_meta(generator_json_path),
        "per_category": _synthetic_per_category_from_s1(s1),
    }

    if include_structure_check:
        with open(generator_json_path, encoding="utf-8") as fh:
            pipd_data = json.load(fh)
        result["pipd_structure"] = check_pipd_json(pipd_data, Path(generator_json_path).name)

    _pdd = os.environ.get("PIPD_DATA_DIR", "").strip()
    data_dir = Path(_pdd) if _pdd else _PKG_DIR / "data"
    _prev = os.environ.get("PIPD_USDM_JSON")
    if usdm_json_path and str(usdm_json_path).strip():
        u_p = Path(usdm_json_path).expanduser()
        if not u_p.is_file():
            raise ValueError(f"usdm_json_path not found: {u_p}")
        os.environ["PIPD_USDM_JSON"] = str(u_p.resolve())
    try:
        usdm_bundle = build_usdm_and_truth_block(
            generator_json_path,
            study_id,
            result,
            data_dir=data_dir,
        )
    finally:
        if usdm_json_path and str(usdm_json_path).strip():
            if _prev is None:
                os.environ.pop("PIPD_USDM_JSON", None)
            else:
                os.environ["PIPD_USDM_JSON"] = _prev

    result["usdm_protocol"] = usdm_bundle["usdm_protocol"]
    result["intelligence_truth"] = usdm_bundle["intelligence_truth"]
    return result


def write_scenario1_yaml_and_word(
    result: Dict[str, Any],
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: str | None = None,
    write_yaml: bool = True,
    write_docx: bool = True,
) -> Dict[str, str]:
    """
    Write combined eval YAML (config + report) and reference-layout Word from ``result``.

    Default filenames match ``reference_spec/`` (B7981027): ``pipd_eval_{study_id}.yaml``,
    ``PIPD_Eval_Report_{study_id}.docx``. With ``artifact_stem``, both use ``{stem}.*``.
    """
    paths: Dict[str, str] = {}
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    explicit = (artifact_stem or "").strip()
    if explicit:
        stem = explicit
        yname = f"{stem}.yaml"
        dname = f"{stem}.docx"
    else:
        stem = f"pipd_eval_{study_id}"
        yname = f"{stem}.yaml"
        dname = f"PIPD_Eval_Report_{study_id}.docx"

    md = build_reference_eval_markdown(result)
    # Remove chart placeholder if present (no output_dir context in this helper)
    md = md.replace("<!-- CHART_SCORE_PER_CAT -->\n", "").replace("<!-- CHART_SCORE_PER_CAT -->", "")

    if write_yaml:
        try:
            yp = out_dir / yname
            write_scenario1_report_yaml(yp, result)
            paths["yaml"] = str(yp.resolve())
        except Exception as exc:
            paths["yaml_error"] = str(exc)

    if write_docx:
        dp = out_dir / dname
        try:
            write_docx_from_markdown(md, str(dp), reference_eval=True)
            paths["docx"] = str(dp.resolve())
        except PermissionError:
            alt = out_dir / f"{dp.stem}_{datetime.now():%Y%m%d_%H%M%S}{dp.suffix}"
            write_docx_from_markdown(md, str(alt), reference_eval=True)
            paths["docx"] = str(alt.resolve())
            paths["docx_note"] = "Default .docx locked; wrote timestamped file."
        except Exception as exc:
            paths["docx_error"] = str(exc)

    return paths


def write_scenario1_report_json(output_path: Path, result: Dict[str, Any]) -> None:
    """
    Write the **scenario1_report** tree only (same object embedded under ``scenario1_report``
    in the second YAML document). Use this to keep ``.json`` aligned with ``.yaml`` / Word.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False, default=str)


def write_scenario1_yaml_word_json(
    result: Dict[str, Any],
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: str | None = None,
    write_yaml: bool = True,
    write_docx: bool = True,
    write_json: bool = True,
) -> Dict[str, str]:
    """
    Same as ``write_scenario1_yaml_and_word`` plus optional ``pipd_eval_<study>.json``
    containing the aligned report dict (matches YAML doc 2 ``scenario1_report``).
    """
    paths = write_scenario1_yaml_and_word(
        result,
        output_dir,
        study_id,
        artifact_stem=artifact_stem,
        write_yaml=write_yaml,
        write_docx=write_docx,
    )
    if not write_json:
        return paths
    out_dir = Path(output_dir)
    explicit = (artifact_stem or "").strip()
    stem = explicit if explicit else f"pipd_eval_{study_id}"
    jp = out_dir / f"{stem}.json"
    try:
        write_scenario1_report_json(jp, result)
        paths["json"] = str(jp.resolve())
    except Exception as exc:
        paths["json_error"] = str(exc)
    return paths


def write_scenario1_report_files(
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    output_dir: str,
    write_docx: bool = True,
    usdm_json_path: str | None = None,
    include_structure_check: bool = True,
    with_openai: bool = False,
    openai_reference_layout: bool = False,
    with_figures: bool = False,
    openai_enrich_sources: bool = False,
    artifacts: str = "all",
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    s1 = run_scenario1_eval(
        generator_json_path=generator_json_path,
        ground_truth_csv_path=ground_truth_csv_path,
        study_id=study_id,
        usdm_json_path=usdm_json_path,
    )
    s1 = dict(s1)
    s1["classified_failures"] = classify_failures(s1)

    result = build_scenario1_report_payload(
        s1,
        generator_json_path,
        ground_truth_csv_path,
        study_id,
        usdm_json_path=usdm_json_path,
        include_structure_check=include_structure_check,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    art = (artifacts or "all").strip().lower()
    if art in ("json_docx", "json-docx"):
        minimal_artifacts = True
    elif art == "all":
        minimal_artifacts = False
    else:
        raise ValueError("artifacts must be 'all' or 'json_docx'")

    md_path = out / f"{study_id}_scenario1_report.md"

    # --- GSOP codes via OpenAI (always, when API key is set) ---
    gsop_overrides: Dict[str, List] = {}
    if os.getenv("OPENAI_API_KEY", "").strip():
        _cat_subcat_items: List[Dict] = []
        _cfg_path = resolve_eval_config_path()
        _cfg = load_pipd_eval_config(_cfg_path)
        _cat_w, _yaml_cat_names = category_weights_and_names(_cfg)
        _per_s1 = s1.get("per_category") or {}
        for _cn in range(1, 12):
            _b = _per_s1.get(_cn) or _per_s1.get(str(_cn)) or {}
            _cname = _yaml_cat_names.get(_cn, f"Category {_cn}")
            for _txt in list(_b.get("matched_subcats") or []) + list(_b.get("hallucinated_subcats") or []):
                if _txt and _txt not in {x["text"] for x in _cat_subcat_items}:
                    _cat_subcat_items.append({"text": _txt, "category_num": _cn, "category_name": _cname})
        if _cat_subcat_items:
            try:
                gsop_overrides = fill_gsop_codes_with_openai(_cat_subcat_items)
            except Exception:
                gsop_overrides = {}

    # --- Build markdown with GSOP overrides ---
    md = build_reference_eval_markdown(result, gsop_overrides=gsop_overrides)

    # --- Always generate charts and insert weight-score chart after scorecard ---
    _cat_w_for_chart, _yaml_cat_names_for_chart = {}, {}
    try:
        _cfg2 = load_pipd_eval_config(resolve_eval_config_path())
        _cat_w_for_chart, _yaml_cat_names_for_chart = category_weights_and_names(_cfg2)
    except Exception:
        pass
    ws_chart = write_score_per_category_chart(
        s1, study_id, out,
        prefix=study_id,
        cat_names=_yaml_cat_names_for_chart,
    )
    if ws_chart and ws_chart.is_file():
        chart_img_md = f"![Category Score]({ws_chart.name})\n"
        md = md.replace("<!-- CHART_SCORE_PER_CAT -->", chart_img_md, 1)
    else:
        md = md.replace("<!-- CHART_SCORE_PER_CAT -->\n", "", 1)
        md = md.replace("<!-- CHART_SCORE_PER_CAT -->", "", 1)

    enrich_env = os.environ.get("PIPD_OPENAI_REPORT_ENRICH", "").strip().lower() in ("1", "true", "yes")
    if openai_enrich_sources or enrich_env:
        gt_path = result.get("ground_truth_path") or ground_truth_csv_path
        md, enrich_err = append_usdm_gt_sourced_section(md, result, str(gt_path))
        if enrich_err:
            result["openai_enrich_sources_error"] = enrich_err
    fig_env = os.environ.get("PIPD_EVAL_REPORT_FIGURES", "").strip().lower() in ("1", "true", "yes")
    if (with_figures or fig_env) and not minimal_artifacts:
        chart_paths = try_write_eval_charts(s1, study_id, out, prefix=study_id)
        if chart_paths:
            md += "\n\n## Figures\n\n"
            md += "_M1 recall charts (matplotlib)._\n\n"
            for cp in chart_paths:
                md += f"![{cp.stem}]({cp.name})\n\n"
    layout_env = os.environ.get("PIPD_OPENAI_REFERENCE_LAYOUT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if (openai_reference_layout or layout_env) and os.getenv("OPENAI_API_KEY", "").strip():
        from pipd_openai_reference_layout import rewrite_eval_markdown_like_reference

        md, layout_err = rewrite_eval_markdown_like_reference(md)
        if layout_err:
            result["openai_reference_layout_error"] = layout_err
    elif openai_reference_layout or layout_env:
        result["openai_reference_layout_error"] = "OPENAI_API_KEY not set; skipped reference layout"
    if with_openai and os.getenv("OPENAI_API_KEY", "").strip():
        from pipd_composite_report import add_ai_narrative_section

        md = add_ai_narrative_section(
            md,
            result,
            prepend=not (openai_reference_layout or layout_env),
        )
    paths: Dict[str, str] = {}
    if not minimal_artifacts:
        md_path.write_text(md, encoding="utf-8")
        paths["markdown"] = str(md_path.resolve())

    intel_md_text = build_intelligence_truth_markdown(
        study_id,
        {
            "usdm_protocol": result.get("usdm_protocol") or {},
            "intelligence_truth": result.get("intelligence_truth") or {},
        },
    )
    if not minimal_artifacts:
        intel_md = out / f"{study_id}_intelligence_truth.md"
        intel_md.write_text(intel_md_text, encoding="utf-8")
        paths["intelligence_truth_markdown"] = str(intel_md.resolve())

    json_path = out / f"pipd_eval_{study_id}.json"
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    paths["json"] = str(json_path.resolve())

    yaml_path = out / f"pipd_eval_{study_id}.yaml"
    try:
        write_scenario1_report_yaml(yaml_path, result)
        paths["yaml"] = str(yaml_path.resolve())
    except Exception as exc:
        paths["yaml_error"] = str(exc)

    if write_docx:
        docx_path = out / f"PIPD_Eval_Report_{study_id}.docx"
        try:
            write_docx_from_markdown(md, str(docx_path), reference_eval=True)
            paths["docx"] = str(docx_path.resolve())
        except PermissionError:
            alt = out / f"PIPD_Eval_Report_{study_id}_{datetime.now():%Y%m%d_%H%M%S}.docx"
            write_docx_from_markdown(md, str(alt), reference_eval=True)
            paths["docx"] = str(alt.resolve())
            paths["docx_note"] = "Default .docx locked; wrote timestamped file."
        if not minimal_artifacts:
            try:
                idoc = out / f"{study_id}_intelligence_truth.docx"
                write_docx_from_markdown(intel_md_text, str(idoc))
                paths["intelligence_truth_docx"] = str(idoc.resolve())
            except Exception as exc:
                paths["intelligence_truth_docx_error"] = str(exc)

    return result, paths


def main() -> None:
    p = argparse.ArgumentParser(description="PIPD Scenario 1 eval report (Markdown + Word)")
    p.add_argument("--generator_json", required=True)
    p.add_argument("--ground_truth", required=True)
    p.add_argument("--study_id", required=True)
    p.add_argument("--output_dir", default="eval_outputs")
    p.add_argument("--no-docx", action="store_true")
    p.add_argument("--usdm_json", default=None)
    p.add_argument("--skip-structure-check", action="store_true")
    p.add_argument(
        "--with-openai",
        action="store_true",
        help="OpenAI executive summary (after report if --openai-reference-layout, else prepended)",
    )
    p.add_argument(
        "--openai-reference-layout",
        action="store_true",
        help="Rewrite Markdown/Word to mirror PIPD_Eval_Report_B7981027.docx via OpenAI (metrics unchanged)",
    )
    p.add_argument(
        "--with-figures",
        action="store_true",
        help="Append matplotlib PNGs to Markdown/Word if matplotlib is installed",
    )
    p.add_argument(
        "--openai-enrich-sources",
        action="store_true",
        help="Append AI section from USDM JSON + ground-truth CSV excerpts (OPENAI_API_KEY; no metric edits)",
    )
    p.add_argument(
        "--artifacts",
        choices=("all", "json_docx"),
        default="all",
        help="all: markdown + intelligence sidecars + json + yaml + docx. json_docx: json + yaml + docx only",
    )
    args = p.parse_args()
    art_env = (os.environ.get("PIPD_SCENARIO1_ARTIFACTS") or "").strip().lower()
    artifacts = args.artifacts
    if art_env in ("json_docx", "json-docx"):
        artifacts = "json_docx"
    _, paths = write_scenario1_report_files(
        args.generator_json,
        args.ground_truth,
        args.study_id,
        args.output_dir,
        write_docx=not args.no_docx,
        usdm_json_path=args.usdm_json,
        include_structure_check=not args.skip_structure_check,
        with_openai=args.with_openai,
        openai_reference_layout=args.openai_reference_layout,
        with_figures=args.with_figures,
        openai_enrich_sources=args.openai_enrich_sources,
        artifacts=artifacts,
    )
    print(json.dumps(paths, indent=2))


if __name__ == "__main__":
    main()
