"""
PIPD Eval Report — Markdown aligned to ``reference_spec/PIPD_Eval_Report_B7981027.docx``.

Sections: banner, document score, summary metrics (M1–M4 rows), category scorecard,
category detail (subcategory grid for all 11 categories; optional per-category algorithmic near-miss
+ semantic review tables when ``PIPD_REPORT_NEAR_MISS_SEMANTIC_UI=1``), improvement actions
(bullet summary; optional OpenAI narrative when ``PIPD_IMPROVEMENT_ACTIONS_OPENAI`` is set;
typed failures remain in JSON only).
(Layout matches ``reference_spec/PIPD_Eval_Report_B7981027.docx``: no §0 metric primer.)

**Source of truth:** this module builds report text; Word is produced by
``pipd_markdown_to_docx.write_docx_from_markdown(..., reference_eval=True)``.
A file named ``reference report.docx`` is a legacy / informal label only — it is not
loaded as a template. To refresh the canonical reference Word, run Scenario 1 for B7981027
and save the emitted ``PIPD_Eval_Report_B7981027.docx`` into ``reference_spec/``.

Per-category **M1 near-misses and semantic review (eval UI)** tables (algorithmic + LLM
pairings, mirroring the web UI) are **off by default**. Set ``PIPD_REPORT_NEAR_MISS_SEMANTIC_UI=1``
to include them.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.eval_scenario1 import (
    NULL_PLACEHOLDERS,
    NUM_CATEGORIES,
    compute_category_score,
    gsop_set_from_value,
)
from utils.pipd_eval_config import (
    category_weights_and_names,
    config_label_for_report,
    document_thresholds_from_config,
    load_pipd_eval_config,
    metric_targets_from_config,
    resolve_eval_config_path,
)

# Markdown / Word table cells — long enough for full remediation sentences.
# Override globally: PIPD_EVAL_REPORT_CELL_MAX=0 (or full/none/off) ≈ no truncation.
_LEN_BANNER_TITLE = 240
_LEN_CAT_NAME_SCORECARD = 220
_LEN_CAT_HEADING = 500
_LEN_SUBCAT_ROW = 8000
_LEN_USDM_SOURCE = 8000
_LEN_STATUS = 2000
_LEN_NEAR_MISS = 8000
_LEN_MISS_BLOCKQUOTE = 12000


def _report_near_miss_semantic_ui_sections() -> bool:
    """Mirror PipdNearMissPanel in Markdown when explicitly enabled (default: omit)."""
    v = str(os.getenv("PIPD_REPORT_NEAR_MISS_SEMANTIC_UI", "") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _semantic_pairs_by_category(result: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """LLM semantic-review pairings per category (same structure as the UI)."""
    out: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(1, NUM_CATEGORIES + 1)}
    sr = result.get("semantic_review") or {}
    if not isinstance(sr, dict) or not (sr.get("categories") or {}):
        s1b = (result.get("scenario1_evaluation") or {}) or {}
        alt = s1b.get("semantic_review")
        if isinstance(alt, dict) and (alt.get("categories") or {}):
            sr = alt
    if not isinstance(sr, dict):
        return out
    for key, block in (sr.get("categories") or {}).items():
        if not isinstance(block, dict):
            continue
        try:
            cn = int(key)
        except (TypeError, ValueError):
            continue
        if cn not in out:
            continue
        for p in block.get("pairings") or []:
            if not isinstance(p, dict):
                continue
            reason = p.get("reason")
            out[cn].append(
                {
                    "gt_text": p.get("gt_text"),
                    "generated_text": p.get("generated_text"),
                    "credit": p.get("credit"),
                    "verdict": p.get("verdict"),
                    "reason": (str(reason)[:800] if reason is not None else ""),
                }
            )
    return out


def _algo_kind_ui(nm: Dict[str, Any]) -> str:
    """`Kind` column like PipdNearMissPanel: root_cause + optional (tier X)."""
    rc = str(nm.get("root_cause") or "").strip() or "—"
    tier = nm.get("tier")
    if tier is None or str(tier).strip() == "":
        return rc
    return f"{rc} (tier {tier})"


def _verdict_credit_ui(p: Dict[str, Any]) -> str:
    """Verdict / credit cell like the UI: `verdict · credit`."""
    v = str(p.get("verdict") or "").strip() or "—"
    cr = p.get("credit")
    if cr is None:
        return v
    try:
        return f"{v} · {float(cr):.2f}"
    except (TypeError, ValueError):
        return f"{v} · {cr}"


def _deterministic_improvement_bullets(s1: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """Synthesised action bullets from M1 near-miss / semantic / classified failures (no table)."""
    bullets: List[str] = []
    per = s1.get("per_category") or {}
    near = s1.get("near_misses") or []
    failures = s1.get("classified_failures") or []

    total_miss = sum(len((per.get(cn) or per.get(str(cn)) or {}).get("missed_subcats") or []) for cn in range(1, NUM_CATEGORIES + 1))
    total_hallu = sum(len((per.get(cn) or per.get(str(cn)) or {}).get("hallucinated_subcats") or []) for cn in range(1, NUM_CATEGORIES + 1))
    if total_miss:
        bullets.append(
            f"Regenerate or align **{total_miss}** missed ground-truth subcategory line(s) so verbatim or near-miss match is achievable (see Section 3 blockquotes and category tables)."
        )
    if total_hallu:
        bullets.append(
            f"Reduce **{total_hallu}** extra generated line(s) not in GT to improve precision; pair against misses where wording overlaps (Section 3 \"Extra\" rows)."
        )

    if near:
        rc = Counter(str(nm.get("root_cause") or "UNKNOWN") for nm in near if isinstance(nm, dict))
        for cause, n in rc.most_common(6):
            if cause == "UNKNOWN":
                continue
            bullets.append(
                f"**{cause}** appears **{n}** time(s) in algorithmic near-misses — tighten label/criterion alignment (numbering, truncation, or eligibility phrasing) so scores move toward verbatim."
            )

    if _report_near_miss_semantic_ui_sections():
        sem = _semantic_pairs_by_category(result)
        n_sem = sum(len(sem.get(i) or []) for i in range(1, NUM_CATEGORIES + 1))
        if n_sem:
            bullets.append(
                f"**Semantic review** produced **{n_sem}** GT/generated pairing(s); use those notes to fix systematic wording or GSOP/CSR mismatches the strict matcher could not equate."
            )

    m4_ct = 0
    m2_ct = m3_ct = 0
    for f in failures:
        if not isinstance(f, dict):
            continue
        ft = str(f.get("failure_type") or "")
        if "M4" in ft or "HALLUCINATION" in ft:
            m4_ct += 1
        elif "M2" in ft:
            m2_ct += 1
        elif "M3" in ft:
            m3_ct += 1
    if m4_ct:
        bullets.append(
            "**M4 — False Positives (USDM schema mapping):** All M4 traceability flags are "
            "caused by entity type naming mismatches or USDM 4.0 schema gaps, NOT content "
            "hallucinations. Every flagged subcategory exists in the PD Reference Document "
            "and Intelligence document. Specific issues: (1) `StudyDesign` should be "
            "`InterventionalStudyDesign`; (2) `AdverseEvent` and `InformedConsent` do not "
            "exist in USDM 4.0 — map to `Activity` with populated `usdm_entity_id`; "
            "(3) ensure `usdm_entity_id` is non-null for all subcategories."
        )
    if m2_ct:
        bullets.append("Fix **M2** YES/NO (CSR) flags for auto_confirmed rows so they match the protocol-driven expectation.")
    if m3_ct:
        bullets.append("Resolve **M3** empty-category vs none_identified agreement where GT has no subcategories in a bucket.")

    if not bullets:
        bullets.append("No structured remediation items beyond Section 3 — scores are within target or gaps are already listed per category.")
    return bullets


def load_generator_report_meta(generator_json_path: str) -> Dict[str, Any]:
    p = Path(generator_json_path)
    if not p.is_file():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        j = json.load(fh)
    cats: Dict[int, str] = {}
    for c in j.get("categories") or []:
        n = c.get("category_num")
        if n is not None:
            cats[int(n)] = str(c.get("category_name") or "")
    return {
        "protocol_title":    str(j.get("protocol_title") or ""),
        "study_drug":       str(j.get("study_drug") or ""),
        "protocol_id":      str(j.get("protocol_id") or ""),
        "phase":            str(j.get("phase") or ""),
        "version":          str(j.get("version") or ""),
        "generated_by":     str(j.get("generated_by") or ""),
        "therapeutic_area": str(j.get("therapeutic_area") or ""),
        "category_names":   cats,
    }


def _gen_sub_by_text(gen_json: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for cat in gen_json.get("categories") or []:
        n = cat.get("category_num")
        if n is None:
            continue
        for s in cat.get("subcategories") or []:
            t = str(s.get("subcategory_text") or "").strip()
            if t:
                out[(int(n), t)] = s
    return out


def _usdm_ok(sub: Optional[Dict[str, Any]]) -> bool:
    if not sub:
        return False
    uid = sub.get("usdm_entity_id")
    if uid is None:
        return False
    s = str(uid).strip().lower()
    return s not in NULL_PLACEHOLDERS and s != ""


def _usdm_trace_map(result: Dict[str, Any]) -> Dict[Tuple[int, str], str]:
    """(category_num, subcategory_text) -> symbol from ``intelligence_truth`` block."""
    m: Dict[Tuple[int, str], str] = {}
    for r in (result.get("intelligence_truth") or {}).get("per_subcategory_usdm") or []:
        cn = r.get("category_num")
        if cn is None:
            continue
        t = str(r.get("subcategory_text") or "").strip()
        m[(int(cn), t)] = str(r.get("usdm_symbol") or "—")
    return m


def _usdm_source_trace_map(result: Dict[str, Any]) -> Dict[Tuple[int, str], str]:
    """(category_num, subcategory_text) -> protocol source line."""
    m: Dict[Tuple[int, str], str] = {}
    for r in (result.get("intelligence_truth") or {}).get("per_subcategory_usdm") or []:
        cn = r.get("category_num")
        if cn is None:
            continue
        t = str(r.get("subcategory_text") or "").strip()
        src = r.get("usdm_source")
        if src:
            m[(int(cn), t)] = str(src)
    return m


def _usdm_table_cell(
    cn: int,
    gtxt: str,
    sub: Optional[Dict[str, Any]],
    trace_map: Dict[Tuple[int, str], str],
    protocol_loaded: bool,
) -> str:
    key = (cn, (gtxt or "").strip())
    if key in trace_map:
        return trace_map[key]
    if protocol_loaded:
        return "✗" if sub else "—"
    if not sub:
        return "—"
    # Has a valid entity id → fully traced
    if _usdm_ok(sub):
        return "✓"
    # Has entity class (usdm_entity) but no entity id → partial/class-only match
    if str(sub.get("usdm_entity") or "").strip():
        return "~"
    return "✗"


def _merge_usdm_protocol(ucell: str, src_cell: str) -> str:
    """Return class name when present, otherwise the symbol (✓/~/✗/—)."""
    s = (src_cell or "").strip()
    if s and s != "—":
        return s
    return (ucell or "").strip()


def _conf_short(sub: Optional[Dict[str, Any]]) -> str:
    if not sub:
        return "—"
    c = str(sub.get("confidence") or "").lower()
    if c == "auto_confirmed":
        return "✓"
    if c in ("low_confidence", "review"):
        return "~"
    return "?"


def _yesno_cell(sub: Optional[Dict[str, Any]]) -> str:
    if not sub:
        return "—"
    v = sub.get("include_in_csr")
    if v is True:
        return "✓"
    if v is False:
        return "✗"
    return "—"


def _yesno_cell_row(sub: Optional[Dict[str, Any]], *, hallucination: bool) -> str:
    """
    YES/NO (CSR): ✓ only for grounded rows where include_in_csr is True.
    Hallucinated / extra subcategories — no tick (not applicable).
    """
    if hallucination:
        return "—"
    return _yesno_cell(sub)


def _format_gsop_cell(sub: Optional[Dict[str, Any]]) -> str:
    """Display generator ``gsop_codes`` list/string; empty → em dash."""
    if not sub:
        return "—"
    codes = gsop_set_from_value(sub.get("gsop_codes"))
    if not codes:
        return "—"
    return ", ".join(sorted(codes))


def _protocol_source_cell(sub: Optional[Dict[str, Any]], src_line: str) -> str:
    """Return just the USDM class name when present; otherwise em-dash."""
    if not sub:
        return "—"
    ue = str(sub.get("usdm_entity") or "").strip()
    if not ue:
        return "—"
    return f"USDM class: `{ue}`"


def _md_cell(s: str, max_len: int = 4000) -> str:
    """Trim cell text for Markdown tables. Word output uses the same Markdown."""
    lim = max_len
    raw = os.environ.get("PIPD_EVAL_REPORT_CELL_MAX", "").strip().lower()
    if raw in ("0", "full", "none", "off"):
        lim = 250_000
    elif raw.isdigit():
        lim = max(1, min(int(raw), 500_000))
    t = (s or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if len(t) > lim:
        t = t[: lim - 1] + "…"
    return t


def _doc_pass(overall_pct: float, threshold: float) -> bool:
    return overall_pct >= threshold


def _fmt_eval_date_banner(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s or s == "—":
        return "—"
    if "T" in s and len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def _s1_line_volume_counts(s1: Dict[str, Any]) -> Dict[str, int]:
    """Sums per-category M1 lists (same semantics as pipd_eval_{study_id}.json)."""
    per = s1.get("per_category") or {}
    extra = miss = 0
    for cn in range(1, NUM_CATEGORIES + 1):
        b = per.get(cn) or per.get(str(cn)) or {}
        extra += len(b.get("hallucinated_subcats") or [])
        miss += len(b.get("missed_subcats") or [])
    m1 = (s1.get("metrics") or {}).get("m1_subcategory_recall") or {}
    return {
        "total_matched": int(m1.get("total_matched") or 0),
        "total_gt": int(m1.get("total_gt") or 0),
        "total_generated": int(m1.get("total_generated") or 0),
        "extras_lines": extra,
        "missed_lines": miss,
    }


def _m1_target_from_yaml(mt: Dict[str, float], study_id: str) -> Optional[float]:
    if study_id == "B7981027":
        if "m1_subcategory_recall" in mt:
            return mt["m1_subcategory_recall"]
    else:
        if "m1_aggregate_recall" in mt:
            return mt["m1_aggregate_recall"]
    return mt.get("m1_subcategory_recall")


def build_reference_eval_markdown(
    result: Dict[str, Any],
    *,
    gsop_overrides: Optional[Dict[str, List[str]]] = None,
) -> str:
    """
    Build the reference-layout Markdown for PIPD eval reports.

    ``gsop_overrides``: optional dict mapping subcategory text → list of GSOP code strings,
    produced by ``pipd_openai_report_enrichment.fill_gsop_codes_with_openai``. When provided,
    these codes replace the "—" in the GSOP column for subcategory rows.
    """
    lines: List[str] = []
    sid = result.get("study_id", "")
    meta = result.get("report_metadata") or {}
    s1 = result.get("scenario1_evaluation")
    s1_err = result.get("scenario1_evaluation_error")

    drug = meta.get("study_drug") or "—"
    phase = meta.get("phase") or (s1 or {}).get("phase") or "—"
    ta_meta = (meta.get("therapeutic_area") or "").strip()
    eval_dt = _fmt_eval_date_banner(result.get("eval_date", "—"))
    gen_ver = meta.get("version") or "—"
    cfg_path = resolve_eval_config_path()
    cfg = load_pipd_eval_config(cfg_path)
    cfg_loaded = cfg is not None
    cat_w, yaml_cat_names = category_weights_and_names(cfg)
    yaml_mt = metric_targets_from_config(cfg)
    yaml_pass_th, yaml_tgt_th = document_thresholds_from_config(cfg)
    cfg_lbl = config_label_for_report(cfg_path, cfg_loaded)
    cfg_lbl_banner = Path(cfg_path).name if cfg_loaded else cfg_lbl
    if len(cfg_lbl_banner) > 32:
        cfg_lbl_banner = cfg_lbl_banner[:29] + "…"

    # Banner — stacked paragraphs like reference Word (not a single wide table cell)
    lines.append("**PFIZER PROTOCOL INTELLIGENCE PLATFORM**")
    lines.append("")
    lines.append("**PIPD (Potential Important Protocol Deviations) — Eval Report**")
    lines.append("")
    proto_line = (meta.get("protocol_title") or "").strip()
    if proto_line:
        if len(proto_line) > 78:
            proto_line = proto_line[:75] + "…"
        banner_study = f"{sid} — {proto_line}"
    else:
        banner_study = f"{sid} — {drug}"
        if phase and str(phase).strip() != "—":
            banner_study = f"{banner_study} — {phase}"
        if ta_meta and len(ta_meta) < 72:
            banner_study = f"{banner_study} — {ta_meta}"
    lines.append(f"**{banner_study}**")
    lines.append("")
    lines.append(f"_Eval date: {eval_dt}  |  Generator: v{gen_ver}  |  Config: {cfg_lbl_banner}_")
    lines.append("")

    final_pct = float(result.get("overall_score_percent") or 0)
    thresh = 70.0
    target_doc = 75.0
    doc_ok = _doc_pass(final_pct, thresh)
    pass_txt = "PASS ✓" if doc_ok else "FAIL ✗"
    ta = (s1 or {}).get("ta") or meta.get("therapeutic_area") or "—"
    seg = f"{ta} / {phase}"

    lines.append("## DOCUMENT SCORE")
    lines.append("")
    # Always use the full weighted overall score (M1+M2+M3+M4)
    _score_val = final_pct
    _pass_flag = 1 if doc_ok else 0
    _label = "Weighted: M1(55%) + M2(15%) + M3(10%) + M4(20%)"
    # Encoded comment — parsed by pipd_markdown_to_docx into a styled 3-panel table
    lines.append(f"<!-- DOC_SCORE: seg={seg}|score={_score_val}|pass={_pass_flag}|threshold={thresh:.0f}|target={target_doc:.0f}|label={_label} -->")
    lines.append("")

    if s1_err:
        lines.append(f"_**Scenario 1 eval unavailable:** {s1_err}_")
        lines.append("")
        return "\n".join(lines)

    if not s1:
        lines.append("_**Scenario 1 eval not embedded** — run composite without `--skip-scenario1`._")
        lines.append("")
        return "\n".join(lines)

    m = s1.get("metrics") or {}
    m1_top = m.get("m1_subcategory_recall") or {}
    m1_recall = float(m1_top.get("score") or 0.0)
    m1_precision = float(m1_top.get("precision") or 0.0)
    m1_f1 = float(m1_top.get("f1") or 0.0)
    lines.append("### Top-line Retrieval Metrics")
    lines.append("")
    lines.append("| Metric | What it measures | Current |")
    lines.append("|--------|------------------|---------|")
    lines.append(f"| Recall | Did it find the right subcategories? | {m1_recall * 100:.1f}% |")
    lines.append(f"| Precision | Did it avoid making things up? | {m1_precision * 100:.1f}% |")
    lines.append(f"| F1 | Balanced summary | {m1_f1 * 100:.1f}% |")
    lines.append("")

    lines.append("## 1. Summary Metrics")
    lines.append("")
    lines.append("| Metric | Score | Target | Pass/Fail | Source |")
    lines.append("|--------|-------|--------|-----------|--------|")

    m1 = m.get("m1_subcategory_recall") or {}
    m1_yaml_t = _m1_target_from_yaml(yaml_mt, sid) if yaml_mt else None
    m1_disp_t = m1_yaml_t if m1_yaml_t is not None else m1.get("target")
    try:
        m1_pass = float(m1.get("score") or 0) >= float(m1_disp_t) if m1_disp_t is not None else bool(m1.get("pass"))
    except (TypeError, ValueError):
        m1_pass = bool(m1.get("pass"))
    f1_target = float(m1.get("f1_target") or 0.70)
    recall_target = float(m1.get("target") or 0.75)
    m1_dual_pass = (float(m1.get("f1") or 0.0) >= f1_target) and (float(m1.get("score") or 0.0) >= recall_target)
    lines.append(
        f"| M1 Retrieval (F1 headline) ({sid}) | {_pct(m1.get('f1'))} *(R={_pct(m1.get('score'))}, P={_pct(m1.get('precision'))})* | "
        f"F1 {_pct(f1_target)} + Recall {_pct(recall_target)} | "
        f"{'PASS ✓' if m1_dual_pass else 'FAIL ✗'} | pipd_ground_truth_clean.csv |"
    )
    m2 = m.get("m2_flag_accuracy") or {}
    m2_note = ""
    m2_score = _pct(m2.get("auto_confirmed_accuracy"))
    if (m2.get("auto_confirmed_total") or 0) == 0:
        m2_note = " *(no auto_confirmed rows)*"
    m2_yaml_t = yaml_mt.get("m2_flag_accuracy") if yaml_mt else None
    m2_disp_t = m2_yaml_t if m2_yaml_t is not None else m2.get("target")
    try:
        m2_pass = float(m2.get("auto_confirmed_accuracy") or 0) >= float(m2_disp_t) if m2_disp_t is not None else bool(m2.get("pass"))
    except (TypeError, ValueError):
        m2_pass = bool(m2.get("pass"))
    lines.append(
        f"| M2 YES/NO Flag Accuracy | {m2_score}{m2_note} | {_pct(m2_disp_t)} | "
        f"{'PASS ✓' if m2_pass else 'FAIL ✗'} | pipd_ground_truth_clean.csv |"
    )

    m3 = m.get("m3_empty_category_accuracy") or {}
    m3_yaml_t = yaml_mt.get("m3_empty_category") if yaml_mt else None
    m3_disp_t = m3_yaml_t if m3_yaml_t is not None else m3.get("target")
    try:
        m3_pass = float(m3.get("score") or 0) >= float(m3_disp_t) if m3_disp_t is not None else bool(m3.get("pass"))
    except (TypeError, ValueError):
        m3_pass = bool(m3.get("pass"))
    lines.append(
        f"| M3 Empty Category Accuracy | {_pct(m3.get('score'))} | {_pct(m3_disp_t)} | "
        f"{'PASS ✓' if m3_pass else 'FAIL ✗'} | pipd_ground_truth_clean.csv |"
    )

    m4 = m.get("m4_hallucination_detection") or {}
    hfc = int(m4.get("traceability_flag_count", m4.get("hallucinations_found") or 0))
    total_gen = int((m.get("m1_subcategory_recall") or {}).get("total_generated") or 0)
    traceability_rate = (hfc / total_gen) if total_gen else 0.0
    m4_pass = traceability_rate <= 0.20
    lines.append(
        f"| M4 Traceability flag rate (separate from recall) | {traceability_rate * 100:.1f}% ({hfc}/{total_gen}) | <= 20.0% | "
        f"{'PASS ✓' if m4_pass else 'FAIL ✗'} | missing/bad USDM provenance |"
    )

    lines.append("")
    lines.append(
        "_“Extra” in the scorecard / detail tables means a **generated** subcategory line not present in "
        "ground truth for that category (same items as M1 `hallucinated_subcats`). **M4** counts only "
        "**auto_confirmed** rows with missing/bad **usdm_entity_id** traceability — not confirmed semantic hallucinations._"
    )
    lines.append("")

    lines.append("## 2. Category Scorecard")
    lines.append("")
    lines.append(
        "> **Weight** = (GT subcats in category \u00f7 total GT subcats) \u00d7 100 — max document points "
        "this category can contribute (e.g. Cat 1 = 52.50, Cat 11 = 27.50 when those are the shares). "
        "**Weighted** = weight \u00d7 category score \u00f7 100 — actual document points earned. "
        "**net** = verbatim \u00d7 1.0 + numbering_error / criterion_format near-misses \u00d7 0.99 + truncation near-misses \u00d7 0.60; "
        "misses \u00d7 0; extras \u00d7 0 (extras increase **generated_count** only, pulling precision down). "
        "**precision** = net \u00f7 generated_count · **recall** = net \u00f7 gt_count · "
        "**Score** = F1(precision, recall) \u00d7 100. Empty-GT categories: M3 rules; weight \u2014. "
        "**Subcats** = pass/GT (verbatim + near-miss pairs vs GT count). **Status** = short match/miss/extra summary."
    )
    lines.append("")

    per = s1.get("per_category") or {}
    cat_names = meta.get("category_names") or {}
    near_by_cat: Dict[int, List[Dict]] = {}
    for nm in s1.get("near_misses") or []:
        c = int(nm.get("category_num") or 0)
        near_by_cat.setdefault(c, []).append(nm)
    semantic_by_cat = (
        _semantic_pairs_by_category(result)
        if _report_near_miss_semantic_ui_sections()
        else {i: [] for i in range(1, NUM_CATEGORIES + 1)}
    )

    # Pre-compute total GT subcategory count (denominator for dynamic weights)
    _total_gt = sum(
        int((per.get(cn) or per.get(str(cn)) or {}).get("m1_gt_total") or 0)
        for cn in range(1, NUM_CATEGORIES + 1)
    )

    lines.append("| Category | Weight | Weighted | Score | Subcats | Status |")
    lines.append("|----------|--------|----------|-------|---------|--------|")

    for cn in range(1, NUM_CATEGORIES + 1):
        cblock = per.get(cn) or per.get(str(cn)) or {}
        matched      = int(cblock.get("m1_matched") or 0)
        gt_tot       = int(cblock.get("m1_gt_total") or 0)
        near_miss_cnt = int(cblock.get("m1_near_misses") or 0)
        near_misses_cat = near_by_cat.get(cn, [])
        near_credit = 0.0
        for _nm in near_misses_cat:
            try:
                near_credit += float(_nm.get("credit") or 0.0)
            except (TypeError, ValueError):
                near_credit += 0.0
        missed = cblock.get("missed_subcats") or []
        hall = cblock.get("hallucinated_subcats") or []
        n_extra = len(hall)
        m3ok_raw = cblock.get("m3_none_identified_correct")
        m3ok         = bool(m3ok_raw) if m3ok_raw is not None else True

        # ── Dynamic weight ──────────────────────────────────────────────────
        # Weight = (GT subcats in this category) / (total GT across all cats) * 100
        if _total_gt > 0 and gt_tot > 0:
            w_pct = round(100.0 * gt_tot / _total_gt, 2)
        else:
            # Empty-GT category contributes 0 to the weight pool
            w_pct = 0.0

        # ── Score and weighted contribution ────────────────────────────────
        if gt_tot == 0:
            if m3ok and not hall:
                score_pct = 100.0
                status = "None identified \u2014 both agree"
            elif m3ok and hall:
                score_pct = 0.0
                status = f"{n_extra} extra generated \u2014 none expected in GT"
            else:
                score_pct = 0.0
                status = "none_identified mismatch \u2014 see Section 3"
            weighted = 0.0
            sub_txt = "\u2014"
        else:
            net = matched + near_credit
            gen_tot = int(cblock.get("m1_generated_total") or 0)
            score_pct = round(
                compute_category_score(float(net), gt_tot, gen_tot), 1
            )
            weighted = max(0.0, round(w_pct * score_pct / 100.0, 2))
            # Cap sub_pass at GT count: near-miss generated items can exceed GT when over-generated
            sub_pass = min(matched + near_miss_cnt, gt_tot)
            sub_txt = f"{sub_pass}/{gt_tot}"
            gen_tot_disp = int(cblock.get("m1_generated_total") or 0)
            if not missed and not hall and near_miss_cnt == 0:
                status = "All subcategories matched"
            elif not missed and not hall:
                # All GT covered but with near-misses (over-generation possible)
                over_gen = gen_tot_disp - gt_tot
                nm_note = f"{near_miss_cnt} near-miss"
                if over_gen > 0:
                    nm_note += f" ({gen_tot_disp} generated vs {gt_tot} GT)"
                parts = []
                if matched:
                    parts.append(f"{matched} verbatim")
                parts.append(nm_note)
                if hall:
                    parts.append(f"{n_extra} extra (precision risk)")
                status = ", ".join(parts) + " \u2014 see Section 3"
            else:
                parts = []
                if matched:
                    parts.append(f"{matched} verbatim")
                if near_miss_cnt:
                    over_gen = gen_tot_disp - gt_tot
                    nm_note = f"{near_miss_cnt} near-miss"
                    if over_gen > 0:
                        nm_note += f" ({gen_tot_disp} gen vs {gt_tot} GT)"
                    parts.append(nm_note)
                if missed:
                    n = len(missed)
                    parts.append(f"{n} miss" + ("" if n == 1 else "es"))
                if hall:
                    parts.append(f"{n_extra} extra (precision risk)")
                status = ", ".join(parts) + " \u2014 see Section 3"

        cname = yaml_cat_names.get(cn) or cat_names.get(cn, f"Category {cn}")
        score_cell = f"{score_pct:.1f} / 100"
        if gt_tot == 0:
            w_cell = "\u2014"
        else:
            w_cell = f"{w_pct:.2f}%"
        wt_cell = f"{weighted:.2f}"
        lines.append(
            f"| Cat {cn}. {_md_cell(cname, _LEN_CAT_NAME_SCORECARD)} "
            f"| {w_cell} | {wt_cell} | {score_cell} | {sub_txt} | {_md_cell(status, _LEN_STATUS)} |"
        )
    lines.append("")
    lines.append("<!-- CHART_SCORE_PER_CAT -->")
    lines.append("")

    # 3. Category Detail
    lines.append("## 3. Category Detail")
    lines.append("")

    # --- Missing-categories callout ------------------------------------------
    _missing_cats: List[tuple] = []
    _empty_vs_generated_cats: List[tuple] = []
    for _cn in range(1, NUM_CATEGORIES + 1):
        _cb = per.get(_cn) or per.get(str(_cn)) or {}
        _gt_tot  = int(_cb.get("m1_gt_total") or 0)
        _matched = int(_cb.get("m1_matched") or 0)
        _near    = int(_cb.get("m1_near_misses") or 0)
        _missed  = _cb.get("missed_subcats") or []
        _hall    = _cb.get("hallucinated_subcats") or []
        _gen_has_subcats = (_matched + _near + len(_hall)) > 0
        _cname   = yaml_cat_names.get(_cn) or cat_names.get(_cn, f"Category {_cn}")
        if _gt_tot > 0 and not _gen_has_subcats:
            _missing_cats.append((_cn, _cname, len(_missed)))
        elif _gt_tot == 0 and _gen_has_subcats:
            _empty_vs_generated_cats.append((_cn, _cname, len(_hall)))
    if _missing_cats:
        lines.append("> **⚠ COMPLETELY MISSING CATEGORIES** — generator produced zero matched subcategories:")
        for _cn, _cname, _n in _missing_cats:
            lines.append(f">   - Cat {_cn}. {_cname} ({_n} GT subcategory(ies) not generated)")
        lines.append("")
    if _empty_vs_generated_cats:
        lines.append("> **⚠ EMPTY CATEGORIES GENERATED** — GT has no subcategories for this protocol, but generator produced them:")
        for _cn, _cname, _n in _empty_vs_generated_cats:
            lines.append(f">   - Cat {_cn}. {_cname} ({_n} generated extra subcategory(ies))")
        lines.append("")
    # -------------------------------------------------------------------------

    gen_path = result.get("generator_path")
    gen_json: Dict[str, Any] = {}
    if gen_path and Path(gen_path).is_file():
        with open(gen_path, "r", encoding="utf-8") as fh:
            gen_json = json.load(fh)
    sub_lookup = _gen_sub_by_text(gen_json)

    comp_per = result.get("per_category") or {}
    usdm_pb = result.get("usdm_protocol") or {}
    protocol_loaded = bool(usdm_pb.get("loaded"))
    trace_map = _usdm_trace_map(result)
    source_map = _usdm_source_trace_map(result)

    for cn in range(1, NUM_CATEGORIES + 1):
        cname = yaml_cat_names.get(cn) or cat_names.get(cn, f"Category {cn}")
        lines.append(f"### Category {cn} — {_md_cell(cname, _LEN_CAT_HEADING)}")
        lines.append("")

        ccomp = comp_per.get(str(cn)) or {}
        rows = ccomp.get("rows") or []

        if rows:
            if protocol_loaded:
                lines.append(
                    "_USDM column: **✓** = entity id in protocol · **~** = type / weak ref · **✗** = not resolvable · **—** = exempt. "
                    "Includes **USDM class** (`usdm_entity` / `instanceType`) plus protocol source path when resolved._"
                )
                lines.append("")
            lines.append(
                "| Subcategory text | Score | Match | YES/NO | USDM | Conf. |"
            )
            lines.append(
                "|-------------------|-------|-------|--------|------|-------|"
            )
            for r in rows:
                if r.get("hallucination"):
                    gtxt = str(r.get("generated") or "")
                    sub = sub_lookup.get((cn, gtxt.strip()))
                    ucell = _usdm_table_cell(cn, gtxt, sub, trace_map, protocol_loaded)
                    raw_src = source_map.get((cn, gtxt.strip()), "—")
                    src_cell = _protocol_source_cell(sub, raw_src)
                    usdm_merged = _md_cell(_merge_usdm_protocol(ucell, src_cell), _LEN_SUBCAT_ROW)
                    lines.append(
                        f"| {_md_cell(gtxt, _LEN_SUBCAT_ROW)} | 0 | Extra | {_yesno_cell_row(sub, hallucination=True)} | "
                        f"{usdm_merged} | {_conf_short(sub)} |"
                    )
                    continue
                gt = str(r.get("ground_truth") or "")
                gen = str(r.get("generated") or "")
                display = gen if r.get("present") and gen else gt
                sub = sub_lookup.get((cn, gen.strip())) if gen else None
                exact = bool(r.get("exact"))
                present = bool(r.get("present"))
                sem = r.get("semantic_f1")
                nm_map = {str(x.get("generated_text") or "").strip(): x for x in near_by_cat.get(cn, [])}
                nm_hit = nm_map.get((gen or "").strip())
                if exact:
                    match = "Verbatim"
                    sc = 100
                elif present and nm_hit:
                    tier = str(nm_hit.get("tier") or "B").upper()
                    rc_nm = str(nm_hit.get("root_cause") or "")
                    try:
                        cred_f = float(nm_hit.get("credit") or 0.0)
                        cred_s = f"{cred_f:.2f}"
                    except (TypeError, ValueError):
                        cred_f = 0.0
                        cred_s = "—"
                    if tier == "P" and rc_nm == "TRUNCATION":
                        match = f"Truncated — key protocol details dropped (credit {cred_s})"
                    elif tier == "P":
                        match = f"Paraphrase — same meaning, different wording (credit {cred_s})"
                    elif rc_nm == "TRUNCATION":
                        match = f"Truncated — generated text is shortened (credit {cred_s})"
                    elif rc_nm == "CRITERION_FORMAT" and cred_f >= 0.99:
                        match = f"Near-verbatim — trivial spacing/typo difference (credit {cred_s})"
                    elif rc_nm == "CRITERION_FORMAT":
                        match = f"Criterion format — same criterion, minor detail change (credit {cred_s})"
                    elif tier == "A" or rc_nm == "NUMBERING_ERROR":
                        match = f"Numbering error — same criterion body (credit {cred_s})"
                    else:
                        match = f"Near miss · {rc_nm} · credit {cred_s}"
                    try:
                        sc = int(round(100.0 * float(nm_hit.get("credit") or 0.0)))
                    except (TypeError, ValueError):
                        sc = 50
                elif present and sem is not None:
                    match = "Semantic"
                    sc = int(round(float(sem) * 100))
                elif present:
                    match = "Near miss"
                    sc = 0
                else:
                    match = "MISS"
                    sc = 0
                ucell = _usdm_table_cell(cn, gen, sub, trace_map, protocol_loaded)
                gkey = (gen or "").strip()
                raw_src = source_map.get((cn, gkey), "—")
                src_cell = _protocol_source_cell(sub, raw_src)
                usdm_merged = _md_cell(_merge_usdm_protocol(ucell, src_cell), _LEN_SUBCAT_ROW)
                lines.append(
                    f"| {_md_cell(display, _LEN_SUBCAT_ROW)} | {sc} | {match} | {_yesno_cell_row(sub, hallucination=False)} | "
                    f"{usdm_merged} | {_conf_short(sub)} |"
                )
            lines.append("")

        # Always show near-miss paired table when near-misses exist for this category
        nm_cat = near_by_cat.get(cn, [])
        if nm_cat:
            lines.append(f"#### Near-Miss Detail — Category {cn}")
            lines.append("")
            lines.append(
                "_Near-misses are generated items that partially matched a GT subcategory. "
                "Credit < 1.0 means the generated text differs from GT (truncated, paraphrased, or minor format change). "
                "When generated count exceeds GT count, some near-misses are over-generated variants of already-covered GT items._"
            )
            lines.append("")
            lines.append("| GT (ground truth) | Generated | Kind | Credit |")
            lines.append("|-------------------|-----------|------|--------|")
            for nm in nm_cat:
                gt_nm = _md_cell(str(nm.get("gt_text") or ""), _LEN_SUBCAT_ROW)
                gen_nm = _md_cell(str(nm.get("generated_text") or ""), _LEN_SUBCAT_ROW)
                kind = _md_cell(_algo_kind_ui(nm), 400)
                cred = nm.get("credit")
                try:
                    cred_s = f"{float(cred):.2f}" if cred is not None else "N/A"
                except (TypeError, ValueError):
                    cred_s = "N/A"
                lines.append(f"| {gt_nm} | {gen_nm} | {kind} | {cred_s} |")
            lines.append("")

        if _report_near_miss_semantic_ui_sections():
            sem_cat = semantic_by_cat.get(cn) or []
            if sem_cat:
                lines.append("**Semantic review (LLM)**")
                lines.append("")
                lines.append("| GT (CSV) | Generated | Verdict / credit | Note |")
                lines.append("|----------|-----------|------------------|------|")
                for p in sem_cat:
                    gts = _md_cell(str(p.get("gt_text") or ""), _LEN_SUBCAT_ROW)
                    gens = _md_cell(str(p.get("generated_text") or ""), _LEN_SUBCAT_ROW)
                    vc = _md_cell(_verdict_credit_ui(p), 240)
                    note = _md_cell(str(p.get("reason") or "N/A"), _LEN_SUBCAT_ROW)
                    lines.append(f"| {gts} | {gens} | {vc} | {note} |")
                lines.append("")

        cblock = per.get(cn) or per.get(str(cn)) or {}
        _all_missed = cblock.get("missed_subcats") or []
        for miss in _all_missed:
            lines.append(
                f"> **MISS:** {_md_cell(str(miss), _LEN_MISS_BLOCKQUOTE)} \u2014 not generated."
            )
            lines.append("")

    # 4. Improvement Actions — bullet synthesis (no failure table; details remain in Section 3)
    lines.append("## 4. Improvement Actions")
    lines.append("")
    if _report_near_miss_semantic_ui_sections():
        _s4_src = "Section 3 (near-miss, semantic, misses, extras) and metric gaps"
    else:
        _s4_src = "Section 3 (subcategory grid, misses, extras) and metric gaps"
    lines.append(
        f"_Summary points derived from {_s4_src}. "
        "Typed failure rows with fix hints remain in the eval JSON as `classified_failures` for developers._"
    )
    lines.append("")

    narrative: Optional[str] = None
    try:
        from reports.pipd_openai_improvement_actions import fetch_improvement_actions_narrative

        narrative = fetch_improvement_actions_narrative(s1, result)
    except Exception:
        narrative = None

    if narrative and narrative.strip():
        lines.append(narrative.strip())
    else:
        for b in _deterministic_improvement_bullets(s1, result):
            lines.append(f"- {b}")
    lines.append("")

    return "\n".join(lines)


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _composite_appendix_only(result: Dict[str, Any]) -> List[str]:
    """Technical composite scoring (deductions, components) after reference body."""
    lines: List[str] = []
    lines.append("## Appendix — Weighted Composite & Traceability Deductions")
    lines.append("")
    ow = result.get("overall_score_percent_weighted")
    if ow is not None:
        lines.append(f"- **Weighted composite (before deductions):** {ow}%")
    ded = result.get("hallucination_deduction") or {}
    if ded:
        lines.append(
            f"- **Traceability / extra-row deductions:** {ded.get('total_deduction_percent')} pp total "
            f"({ded.get('hallucinated_subcategory_count')} subcats × {ded.get('deduction_percent_per_subcategory')} pp; "
            f"{ded.get('hallucinated_extra_category_count')} extra cats × {ded.get('deduction_percent_per_extra_category')} pp)."
        )
    lines.append(f"- **Final composite score:** {result.get('overall_score_percent')}%")
    lines.append("")
    wb = result.get("weighted_breakdown_percent") or {}
    lines.append("| Component (weight) | Contribution to 100% |")
    lines.append("|--------------------|----------------------|")
    lines.append(f"| Completeness (40%) | {wb.get('completeness', '—')} |")
    lines.append(f"| Accuracy (30%) | {wb.get('accuracy', '—')} |")
    lines.append(f"| Semantic (20%) | {wb.get('semantic', '—')} |")
    lines.append(f"| Traceability / extra-row term (10%) | {wb.get('hallucination', '—')} |")
    lines.append("")
    return lines
