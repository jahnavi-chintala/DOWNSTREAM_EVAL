"""
Build Markdown report for PIPD composite eval (+ optional OpenAI narrative / full AI doc).

Environment (set in a ``.env`` file next to this module, or export in the shell):
  OPENAI_API_KEY      – required for any OpenAI call
  OPENAI_MODEL        – default gpt-4o-mini
  OPENAI_DOC_MAX_TOKENS – max tokens for the long AI document (default 4096)

Prompt files (edit these to change AI behaviour):
  prompts/ai_doc_system.txt – system message for the full documentation
  prompts/ai_doc_user.txt   – user instructions prepended before the JSON payload
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from eval_scenario1 import classify_failures, run_scenario1_eval
from pipd_composite_eval import run_composite_eval, save_composite_json
from pipd_eval_report_reference import build_reference_eval_markdown, load_generator_report_meta
from pipd_markdown_to_docx import write_docx_from_markdown
from pipd_structure_completeness import check_pipd_json
from pipd_usdm_support import build_intelligence_truth_markdown, build_usdm_and_truth_block

_PKG_DIR = Path(__file__).resolve().parent
load_dotenv(_PKG_DIR / ".env")
_PROMPTS_DIR = _PKG_DIR / "prompts"


def _read_text(rel: str, default: str) -> str:
    p = _PKG_DIR / rel
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return default


def _md_escape_cell(s: str, max_len: int = 600) -> str:
    t = (s or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


def _pct01(x: Any) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _scenario1_eval_markdown(s1: Dict[str, Any]) -> List[str]:
    """Markdown block mirroring ``run_eval.print_summary`` (Scenario 1)."""
    lines: List[str] = []
    sid = s1.get("study_id", "")
    sc = s1.get("scenario", 1)
    lines.append(f"## PIPD Eval Summary (Scenario {sc})")
    lines.append("")
    lines.append(
        f"_Same metrics and pass logic as the unified eval (`run_eval.py` / `{sid}_results.json`)._"
    )
    lines.append("")
    m = s1.get("metrics") or {}
    m1 = m.get("m1_subcategory_recall") or {}
    m2 = m.get("m2_flag_accuracy") or {}
    m3 = m.get("m3_empty_category_accuracy") or {}
    m4 = m.get("m4_hallucination_detection") or {}
    t1 = m1.get("target")
    t2 = m2.get("target")
    t3 = m3.get("target")
    t4 = m4.get("target")
    lines.append("| Metric | Result | Target | Pass |")
    lines.append("|--------|--------|--------|------|")
    lines.append(
        f"| M1 Subcategory Recall | {_pct01(m1.get('score'))} | "
        f"{_pct01(t1) if t1 is not None else '—'} | "
        f"{'PASS' if m1.get('pass') else 'FAIL'} |"
    )
    m2_val = _pct01(m2.get("auto_confirmed_accuracy"))
    if (m2.get("auto_confirmed_total") or 0) == 0:
        m2_val = f"{m2_val} *(no auto_confirmed rows)*"
    lines.append(
        f"| M2 Flag Accuracy | {m2_val} | "
        f"{_pct01(t2) if t2 is not None else '—'} | "
        f"{'PASS' if m2.get('pass') else 'FAIL'} |"
    )
    lines.append(
        f"| M3 Empty Category Accuracy | {_pct01(m3.get('score'))} | "
        f"{_pct01(t3) if t3 is not None else '—'} | "
        f"{'PASS' if m3.get('pass') else 'FAIL'} |"
    )
    lines.append(
        f"| M4 Hallucinations | {m4.get('hallucinations_found', '—')} | "
        f"{int(t4) if t4 is not None else 0} | "
        f"{'PASS' if m4.get('pass') else 'FAIL'} |"
    )
    lines.append("")
    verdict = s1.get("go_no_go", "UNKNOWN")
    lines.append(f"**Verdict: {verdict}**")
    lines.append("")
    failures = s1.get("classified_failures") or []
    if failures:
        lines.append("## Classified failures (generator remediation)")
        lines.append("")
        lines.append("| Failure type | Category | Example |")
        lines.append("|--------------|----------|---------|")
        max_rows = 60
        for i, f in enumerate(failures[:max_rows]):
            lines.append(
                f"| {_md_escape_cell(str(f.get('failure_type', '')), 40)} | "
                f"{f.get('category_num', '—')} | "
                f"{_md_escape_cell(str(f.get('example', '')), 100)} |"
            )
        if len(failures) > max_rows:
            lines.append(f"| … | … | *({len(failures) - max_rows} more rows — see JSON)* |")
        lines.append("")
    return lines


def build_markdown_report(result: Dict[str, Any]) -> str:
    """Default: layout aligned to ``reference_spec/PIPD_Eval_Report_*.docx``. Set ``PIPD_EVAL_REPORT_STYLE=legacy`` for the older report."""
    if str(os.environ.get("PIPD_EVAL_REPORT_STYLE", "reference")).lower() == "legacy":
        return _build_legacy_markdown_report(result)
    return build_reference_eval_markdown(result)


def _build_legacy_markdown_report(result: Dict[str, Any]) -> str:
    lines: list[str] = []
    sid = result["study_id"]
    lines.append(f"# PIPD Evaluation Report — `{sid}`")
    lines.append("")
    err_s1 = result.get("scenario1_evaluation_error")
    if err_s1:
        lines.append(f"_**Scenario 1 block skipped:** {err_s1}_")
        lines.append("")
    s1 = result.get("scenario1_evaluation")
    if s1:
        lines.append(f"**Therapeutic area:** {s1.get('ta', '—')}  ")
        lines.append(f"**Phase:** {s1.get('phase', '—')}")
        lines.append("")
        lines.append(
            f"**Scenario 1 evaluated:** {s1.get('eval_date', '—')}  \n"
            f"**Composite evaluated:** {result.get('eval_date', '—')}"
        )
        lines.append("")
        lines.extend(_scenario1_eval_markdown(s1))
        lines.append("---")
        lines.append("")
    else:
        lines.append(f"**Composite evaluated:** {result.get('eval_date', '—')}")
        lines.append("")
    lines.append("## Composite score (weighted + hallucination deductions)")
    lines.append("")
    lines.append(
        "_Supplementary alignment score from `pipd_composite_eval` (ground-truth CSV vs generated JSON)._"
    )
    lines.append("")
    lines.append("### Overall composite")
    lines.append("")
    ow = result.get("overall_score_percent_weighted")
    if ow is not None:
        lines.append(
            f"| **Weighted composite (before hallucination deductions)** | **{ow}%** "
            f"(0–1: {result.get('overall_score_0_1_weighted', '—')}) |"
        )
        lines.append("")
    ded = result.get("hallucination_deduction") or {}
    if ded:
        lines.append("### Hallucination deductions (percentage points off composite)")
        lines.append("")
        lines.append("| Item | Count | Rate (pp each) | Deduction (pp) |")
        lines.append("|------|-------|----------------|----------------|")
        lines.append(
            f"| Hallucinated subcategories | {ded.get('hallucinated_subcategory_count', '—')} | "
            f"{ded.get('deduction_percent_per_subcategory', '—')} | "
            f"{ded.get('deduction_from_subcategories_percent', '—')} |"
        )
        lines.append(
            f"| Hallucinated categories (generator-only vs GT) | "
            f"{ded.get('hallucinated_extra_category_count', '—')} | "
            f"{ded.get('deduction_percent_per_extra_category', '—')} | "
            f"{ded.get('deduction_from_categories_percent', '—')} |"
        )
        lines.append(
            f"| **Total deducted** | | | **{ded.get('total_deduction_percent', '—')}** |"
        )
        lines.append("")
    lines.append(
        f"| **Final composite score** | **{result.get('overall_score_percent', 0)}%** "
        f"(0–1 scale: {result.get('overall_score_0_1', 0)}) |"
    )
    lines.append("")
    wb = result.get("weighted_breakdown_percent", {})
    lines.append("| Component | Contribution to 100% |")
    lines.append("|-----------|------------------------|")
    lines.append(f"| Completeness (40%) | {wb.get('completeness', 0)} |")
    lines.append(f"| Accuracy (30%) | {wb.get('accuracy', 0)} |")
    lines.append(f"| Semantic (20%) | {wb.get('semantic', 0)} |")
    lines.append(f"| Hallucination term (10%) | {wb.get('hallucination', 0)} |")
    lines.append("")

    meth = result.get("methodology", {})
    lines.append("## How metrics are computed")
    lines.append("")
    for k, v in meth.items():
        lines.append(f"- **{k.replace('_', ' ')}:** {v}")
    lines.append("")

    comp = result.get("components", {})
    lines.append("## Component scores (composite inputs)")
    lines.append("")
    lines.append("| Component | Value (0–1) | % of full scale |")
    lines.append("|-----------|-------------|----------------|")
    for name in ("completeness", "accuracy", "semantic", "hallucination"):
        c = comp.get(name, {}) or {}
        lines.append(
            f"| {name.replace('_', ' ').title()} | {c.get('value_0_1', '—')} | "
            f"{c.get('percent', '—')}% |"
        )
    lines.append("")
    cc = comp.get("completeness") or {}
    if cc:
        lines.append(
            f"_Completeness detail: categories present {cc.get('present_categories_count', '—')} / "
            f"{len(cc.get('expected_categories', []) or [])} expected; "
            f"GT subcats matched {cc.get('gt_subcategories_matched', '—')} / "
            f"{cc.get('gt_subcategories_total', '—')}._"
        )
        lines.append("")
    hh = comp.get("hallucination") or {}
    if hh:
        lines.append(
            f"_Hallucination term: unmatched generated subcategories "
            f"{hh.get('unmatched_generated_subcategories', '—')} / "
            f"{hh.get('total_generated_subcategories', '—')} "
            f"(rate {hh.get('hallucination_rate_0_1', '—')}). "
            "Includes subcategories under **whole extra categories** "
            "(category numbers in the generated file that do not appear in the GT CSV for this study)._"
        )
        extra_cats = hh.get("categories_in_generator_not_in_ground_truth") or []
        if extra_cats:
            n_extra = hh.get("generated_subcategories_in_non_gt_categories", 0)
            lines.append(
                f"_Generator-only category numbers (not in GT for this study): "
                f"{extra_cats} — **{n_extra}** subcategory rows counted as unmatched._"
            )
        lines.append("")

    micro = result.get("micro_subcategory", {})
    lines.append("## Composite scoring by category (Actual PIPD vs generated)")
    lines.append("")
    lines.append(
        f"Micro-point total: **{micro.get('points_sum_earned', 0)}** / "
        f"**{micro.get('max_points_if_perfect', 100)}** "
        f"over **{micro.get('total_gt_subcategories', 0)}** GT subcategories "
        f"(~{micro.get('weight_per_subcategory', 0)} points per line if all were perfect). "
        "Extra generated lines (no Actual match) do not earn micro-points; they affect the "
        "hallucination component and count toward **subcategory-level** deductions on the final %."
    )
    lines.append("")
    per = result.get("per_category", {})
    for cat_key in sorted(per.keys(), key=lambda x: int(x)):
        block = per[cat_key]
        cn = block["category_num"]
        lines.append(f"### Category {cn}")
        lines.append("")
        lines.append(
            f"_Category present in generated file: {block.get('category_in_json')} | "
            f"Actual rows: {block.get('gt_subcategory_count')} | "
            f"Generated rows: {block.get('generated_subcategory_count')} | "
            f"Matched pairs: {block.get('matched_count')}_"
        )
        lines.append("")
        lines.append(
            "| Actual PIPD | Generated PIPD | Present | Exact | Dist | Semantic | Points earned | Notes |"
        )
        lines.append(
            "|-------------|----------------|---------|-------|------|----------|---------------|-------|"
        )
        for r in block.get("rows", []):
            notes = ""
            if r.get("hallucination"):
                notes = "Extra generated (no Actual match)"
            pe = r.get("points_earned")
            pe_s = "—" if pe is None else str(pe)
            lines.append(
                f"| {_md_escape_cell(r.get('ground_truth', ''), 120)} | "
                f"{_md_escape_cell(r.get('generated', ''), 120)} | "
                f"{'Yes' if r.get('present') else 'No'} | "
                f"{'Yes' if r.get('exact') else 'No'} | "
                f"{r.get('distance') if r.get('distance') is not None else '—'} | "
                f"{r.get('semantic_f1') if r.get('semantic_f1') is not None else '—'} | "
                f"{pe_s} | "
                f"{notes} |"
            )
        lines.append("")

    return "\n".join(lines)


def _openai_chat(messages: List[Dict[str, str]], max_tokens: int) -> Tuple[Optional[str], Optional[str]]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None, "OPENAI_API_KEY is not set (add it to .env next to api.py)."
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return out["choices"][0]["message"]["content"], None
    except Exception as exc:
        return None, str(exc)


def _fetch_openai_narrative(payload_summary: str) -> Optional[str]:
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    text, err = _openai_chat(
        [
            {
                "role": "system",
                "content": (
                    "You write concise evaluation reports for clinical protocol digitization QA. "
                    "Explain strengths, gaps, and how the numeric scores were produced. No hype."
                ),
            },
            {"role": "user", "content": payload_summary[:24000]},
        ],
        max_tokens=1200,
    )
    if err:
        return f"*(OpenAI narrative unavailable: {err})*"
    return text or None


def generate_ai_documentation_markdown(result: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Full Markdown document from prompts + evaluation JSON.
    Returns (markdown_or_none, error_message_or_none).
    """
    system = _read_text(
        "prompts/ai_doc_system.txt",
        "You write technical QA documentation from structured JSON. Use Markdown.",
    )
    user_prefix = _read_text(
        "prompts/ai_doc_user.txt",
        "Document the following PIPD composite evaluation JSON with clear sections.\n\n",
    )
    payload = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    max_chars = 120_000
    if len(payload) > max_chars:
        payload = payload[:max_chars] + "\n\n… [truncated for model context]"
    user_content = f"{user_prefix}\n{payload}"

    max_tok = int(os.getenv("OPENAI_DOC_MAX_TOKENS", "4096"))
    return _openai_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        max_tokens=max_tok,
    )


def add_ai_narrative_section(
    markdown_body: str,
    result: Dict[str, Any],
    *,
    prepend: bool = True,
) -> str:
    if str(result.get("eval_report_mode") or "").lower() == "scenario1_only":
        s1 = result.get("scenario1_evaluation") or {}
        m = s1.get("metrics") or {}
        m1 = m.get("m1_subcategory_recall") or {}
        summary = (
            f"Study {result.get('study_id')} — Scenario 1 evaluation only (document score row is M1-led; "
            f"no weighted composite in this artifact).\n"
            f"M1 recall: {m1.get('score')} | Go/No-Go: {s1.get('go_no_go')}\n"
            f"Keys in metrics: {list(m.keys())}\n"
            "Write 3–6 short paragraphs for stakeholders on quality and gaps. Do not invent percentages."
        )
    else:
        summary = (
            f"Study {result.get('study_id')}. Final composite {result.get('overall_score_percent')}% "
            f"(weighted before deductions {result.get('overall_score_percent_weighted', 'n/a')}%).\n"
            f"Hallucination deductions: {json.dumps(result.get('hallucination_deduction'), default=str)}\n"
            f"Weights: {json.dumps(result.get('weights'))}\n"
            f"Methodology keys: {list((result.get('methodology') or {}).keys())}\n"
            "Write 3–6 short paragraphs for stakeholders."
        )
    narrative = _fetch_openai_narrative(summary)
    if not narrative:
        return markdown_body
    block = "# AI-generated executive summary\n\n" + narrative + "\n\n"
    sep = "---\n\n"
    if prepend:
        return block + sep + markdown_body
    return markdown_body + "\n\n" + sep + block


def write_composite_report_files(
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    output_dir: str,
    use_bertscore: bool = True,
    with_openai: bool = True,
    generate_ai_doc: bool = False,
    write_docx: bool = True,
    include_scenario1_eval: bool = True,
    usdm_json_path: Optional[str] = None,
    include_structure_check: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    s1_embed: Optional[Dict[str, Any]] = None
    s1_err: Optional[str] = None
    if include_scenario1_eval:
        try:
            s1_run = run_scenario1_eval(
                generator_json_path=generator_json_path,
                ground_truth_csv_path=ground_truth_csv_path,
                study_id=study_id,
                usdm_json_path=usdm_json_path,
            )
            s1_embed = dict(s1_run)
            s1_embed["classified_failures"] = classify_failures(s1_embed)
        except Exception as exc:
            s1_err = str(exc)
    result = run_composite_eval(
        generator_json_path,
        ground_truth_csv_path,
        study_id,
        use_bertscore=use_bertscore,
    )
    if include_structure_check:
        with open(generator_json_path, encoding="utf-8") as fh:
            pipd_data = json.load(fh)
        result["pipd_structure"] = check_pipd_json(
            pipd_data, Path(generator_json_path).name
        )
    if s1_embed is not None:
        result["scenario1_evaluation"] = s1_embed
    elif s1_err is not None:
        result["scenario1_evaluation_error"] = s1_err
    result["report_metadata"] = load_generator_report_meta(generator_json_path)
    _pdd = os.environ.get("PIPD_DATA_DIR", "").strip()
    data_dir = Path(_pdd) if _pdd else _PKG_DIR / "data"
    _prev_usdm_env = os.environ.get("PIPD_USDM_JSON")
    if usdm_json_path and str(usdm_json_path).strip():
        u_p = Path(usdm_json_path).expanduser()
        if not u_p.is_file():
            raise ValueError(f"usdm_json_path not found or not a file: {u_p}")
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
            if _prev_usdm_env is None:
                os.environ.pop("PIPD_USDM_JSON", None)
            else:
                os.environ["PIPD_USDM_JSON"] = _prev_usdm_env
    result["usdm_protocol"] = usdm_bundle["usdm_protocol"]
    result["intelligence_truth"] = usdm_bundle["intelligence_truth"]

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = str(out / f"{study_id}_pipd_composite.json")
    md_path = str(out / f"{study_id}_pipd_composite_report.md")
    save_composite_json(result, json_path)
    md = build_markdown_report(result)
    if with_openai:
        md = add_ai_narrative_section(md, result)
    Path(md_path).write_text(md, encoding="utf-8")
    paths: Dict[str, str] = {"json": json_path, "markdown": md_path}

    intel_md_path = str(out / f"{study_id}_intelligence_truth.md")
    Path(intel_md_path).write_text(
        build_intelligence_truth_markdown(study_id, usdm_bundle),
        encoding="utf-8",
    )
    paths["intelligence_truth_markdown"] = intel_md_path
    if write_docx:
        docx_path = str(out / f"{study_id}_pipd_composite_report.docx")
        try:
            write_docx_from_markdown(md, docx_path, reference_eval=True)
            paths["docx"] = docx_path
        except PermissionError:
            alt = str(
                out / f"{study_id}_pipd_composite_report_{datetime.now():%Y%m%d_%H%M%S}.docx"
            )
            try:
                write_docx_from_markdown(md, alt, reference_eval=True)
                paths["docx"] = alt
                paths["docx_note"] = (
                    f"Default file was locked (close Word); wrote: {alt}"
                )
            except Exception as exc:
                paths["docx_error"] = str(exc)
        except Exception as exc:
            paths["docx_error"] = str(exc)
    if write_docx and paths.get("intelligence_truth_markdown"):
        intel_docx = str(out / f"{study_id}_intelligence_truth.docx")
        try:
            write_docx_from_markdown(Path(intel_md_path).read_text(encoding="utf-8"), intel_docx)
            paths["intelligence_truth_docx"] = intel_docx
        except Exception as exc:
            paths["intelligence_truth_docx_error"] = str(exc)
    if generate_ai_doc:
        ai_md, err = generate_ai_documentation_markdown(result)
        ai_path = str(out / f"{study_id}_pipd_ai_document.md")
        if ai_md:
            header = (
                f"<!-- AI-generated using prompts/ai_doc_system.txt + ai_doc_user.txt | "
                f"model {os.getenv('OPENAI_MODEL', 'gpt-4o-mini')} -->\n\n"
            )
            full_md = header + ai_md
            Path(ai_path).write_text(full_md, encoding="utf-8")
            paths["ai_document"] = ai_path
            if write_docx:
                ai_docx = str(out / f"{study_id}_pipd_ai_document.docx")
                try:
                    write_docx_from_markdown(full_md, ai_docx, reference_eval=True)
                    paths["ai_document_docx"] = ai_docx
                except PermissionError:
                    alt_ai = str(
                        out / f"{study_id}_pipd_ai_document_{datetime.now():%Y%m%d_%H%M%S}.docx"
                    )
                    try:
                        write_docx_from_markdown(full_md, alt_ai, reference_eval=True)
                        paths["ai_document_docx"] = alt_ai
                        paths["ai_document_docx_note"] = (
                            "Default AI docx locked; wrote alternate path."
                        )
                    except Exception as exc:
                        paths["ai_document_docx_error"] = str(exc)
                except Exception as exc:
                    paths["ai_document_docx_error"] = str(exc)
        else:
            Path(ai_path).write_text(
                f"# AI document not generated\n\nError: {err or 'unknown'}\n",
                encoding="utf-8",
            )
            paths["ai_document"] = ai_path
            paths["ai_document_error"] = err or "unknown"
    return result, paths


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="PIPD composite eval + Markdown report")
    p.add_argument("--generator_json", required=True)
    p.add_argument("--ground_truth", required=True)
    p.add_argument("--study_id", required=True)
    p.add_argument("--output_dir", default="eval_outputs")
    p.add_argument("--no-bertscore", action="store_true", help="Use Levenshtein proxy only")
    p.add_argument("--no-openai", action="store_true", help="Skip AI narrative block")
    p.add_argument(
        "--ai-doc",
        action="store_true",
        help="Generate full AI Markdown doc (prompts/ai_doc_*.txt + OPENAI_API_KEY in .env)",
    )
    p.add_argument("--no-docx", action="store_true", help="Skip .docx export (Markdown only)")
    p.add_argument(
        "--skip-scenario1",
        action="store_true",
        help="Do not run/embed Scenario 1 eval block (composite only, faster)",
    )
    p.add_argument(
        "--usdm_json",
        default=None,
        help="USDM protocol JSON path (sets PIPD_USDM_JSON for this run); default auto-discovery",
    )
    p.add_argument(
        "--skip-structure-check",
        action="store_true",
        help="Skip pipd_structure completeness block in composite JSON",
    )
    args = p.parse_args()
    _, paths = write_composite_report_files(
        args.generator_json,
        args.ground_truth,
        args.study_id,
        args.output_dir,
        use_bertscore=not args.no_bertscore,
        with_openai=not args.no_openai,
        generate_ai_doc=args.ai_doc,
        write_docx=not args.no_docx,
        include_scenario1_eval=not args.skip_scenario1,
        usdm_json_path=args.usdm_json,
        include_structure_check=not args.skip_structure_check,
    )
    print(json.dumps(paths, indent=2))
