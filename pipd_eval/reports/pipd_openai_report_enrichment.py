"""
Append a short, source-backed Markdown section using OpenAI + USDM + ground-truth excerpts.

Fills narrative/context fields that are not spelled out in PIPD/eval JSON but can be
grounded in the protocol JSON and CSV. Does **not** change scores or metric tables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def build_report_source_context(
    result: Dict[str, Any],
    ground_truth_csv_path: str,
    *,
    max_total_chars: int = 48_000,
    gt_fraction: float = 0.45,
) -> str:
    """
    Pack truncated ground-truth CSV + USDM JSON + compact ``intelligence_truth`` / metadata
    for model context (same facts the report uses, plus raw excerpts).
    """
    study_id = str(result.get("study_id") or "").strip()
    max_gt = int(max_total_chars * gt_fraction)
    max_usdm = int(max_total_chars * (1.0 - gt_fraction) - 2000)
    parts: list[str] = []

    # Ground truth (study-filtered when possible)
    try:
        import pandas as pd

        df = pd.read_csv(ground_truth_csv_path, dtype=str)
        if "study_folder" in df.columns and study_id:
            df = df[df["study_folder"].str.strip() == study_id]
        gt_txt = df.to_csv(index=False)
        if len(gt_txt) > max_gt:
            gt_txt = gt_txt[:max_gt] + "\n...[ground_truth_csv truncated]\n"
        parts.append(
            "### Ground truth CSV (filtered to study when `study_folder` column exists)\n\n"
            f"```\n{gt_txt}\n```"
        )
    except Exception as exc:
        parts.append(f"### Ground truth CSV\n\n*(Could not load: {exc})*")

    # USDM JSON excerpt
    usdm_pb = result.get("usdm_protocol") or {}
    path = usdm_pb.get("path")
    if usdm_pb.get("loaded") and path:
        try:
            p = Path(str(path))
            raw = p.read_text(encoding="utf-8")
            if len(raw) > max_usdm:
                raw = (
                    raw[: max_usdm // 2]
                    + "\n\n...[USDM middle omitted]...\n\n"
                    + raw[-(max_usdm // 2) :]
                )
            parts.append(f"### USDM protocol JSON (truncated)\n\n```json\n{raw}\n```")
        except Exception as exc:
            parts.append(f"### USDM protocol JSON\n\n*(Could not read `{path}`: {exc})*")
    else:
        msg = usdm_pb.get("message") or "Not loaded."
        parts.append(f"### USDM protocol JSON\n\n*({msg})*")

    # Embedded intelligence block (no full per-row dump)
    it = result.get("intelligence_truth") or {}
    if it:
        slim: Dict[str, Any] = {k: v for k, v in it.items() if k != "per_subcategory_usdm"}
        rows = it.get("per_subcategory_usdm") or []
        slim["per_subcategory_usdm_rowcount"] = len(rows)
        slim["per_subcategory_usdm_sample"] = rows[:20]
        js = json.dumps(slim, indent=2, default=str)
        if len(js) > 9000:
            js = js[:9000] + "\n…"
        parts.append(f"### intelligence_truth (from evaluator, truncated)\n\n```json\n{js}\n```")

    meta = result.get("report_metadata") or {}
    if meta:
        mj = json.dumps(meta, indent=2, default=str)
        if len(mj) > 3500:
            mj = mj[:3500] + "\n…"
        parts.append(f"### report_metadata (PIPD generator JSON)\n\n```json\n{mj}\n```")

    out = "\n\n".join(parts)
    if len(out) > max_total_chars:
        out = out[:max_total_chars] + "\n...[context bundle truncated]\n"
    return out


def append_usdm_gt_sourced_section(
    markdown_body: str,
    result: Dict[str, Any],
    ground_truth_csv_path: str,
) -> Tuple[str, Optional[str]]:
    """
    Call OpenAI to produce a **single** new Markdown section grounded in USDM + GT excerpts,
    and append it to ``markdown_body``. Does not send the full report to the model (keeps
    metrics immune); only study id + source context.

    Returns (extended_markdown, error_or_none).
    """
    from reports.pipd_composite_report import _openai_chat

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return markdown_body, "OPENAI_API_KEY not set"

    ctx = build_report_source_context(result, ground_truth_csv_path)
    study_id = result.get("study_id", "")

    system = (
        "You write ONE Markdown section for a PIPD evaluation report. "
        "Use **only** facts supported by SOURCE CONTEXT (USDM JSON excerpt and ground-truth CSV excerpt). "
        "If something is not in the excerpt, say it is not available in the excerpt — do not invent.\n\n"
        "The section must start with exactly this heading on its own line:\n"
        "## Protocol & ground-truth sourced fields (AI, excerpt-based)\n\n"
        "Then add subsections ### From USDM excerpt and ### From ground-truth CSV as appropriate.\n"
        "Use bullets and short prose (target under 35 lines). Mention study identifiers, protocol title, "
        "or phase **only** if they appear verbatim or clearly in the USDM excerpt. "
        "Summarize CSV columns or CSR/rationale patterns only when visible in the CSV text.\n"
        "Do **not** state M1–M6 scores or PASS/FAIL — those belong in the main report.\n"
        "Output **only** the new section Markdown, with no ``` fences and no preamble."
    )
    user = f"Study id: `{study_id}`\n\n{ctx}"
    if len(user) > 100_000:
        user = user[:100_000] + "\n...[user message truncated]\n"

    max_tok = int(os.environ.get("OPENAI_ENRICH_MAX_TOKENS", "4500"))
    text, err = _openai_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tok,
    )
    if err:
        return markdown_body, err
    section = (text or "").strip()
    if not section:
        return markdown_body, "Empty OpenAI enrichment response"
    if section.startswith("```"):
        section = section.removeprefix("```markdown").removeprefix("```md").removeprefix("```").strip()
        if section.endswith("```"):
            section = section[:-3].strip()

    return markdown_body.rstrip() + "\n\n" + section + "\n", None


def fill_gsop_codes_with_openai(
    subcategory_texts: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """
    Ask OpenAI to suggest GSOP deviation codes for a list of subcategory deviations.

    ``subcategory_texts`` is a list of dicts with keys:
      - ``text``: the subcategory deviation text
      - ``category_num``: int category number
      - ``category_name``: str category name

    Returns a dict mapping ``text`` → list of GSOP code strings (e.g. ["GE-001", "GE-003"]).
    On error or missing API key, returns an empty dict.
    """
    from reports.pipd_composite_report import _openai_chat

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return {}
    if not subcategory_texts:
        return {}

    items_json = json.dumps(
        [
            {
                "id": i,
                "category_num": d.get("category_num"),
                "category_name": d.get("category_name", ""),
                "text": d.get("text", ""),
            }
            for i, d in enumerate(subcategory_texts)
        ],
        indent=2,
    )

    system = (
        "You are an expert in clinical trial protocol deviations and Pfizer GSOP (Good Study Operations "
        "Practices) classification. For each deviation subcategory text provided, assign the most appropriate "
        "Pfizer GSOP deviation code(s).\n\n"
        "GSOP codes use the format: 2–3 uppercase letters followed by 2 digits, e.g. CT40, INV02, INV04, "
        "IP01, VS03, SR02, IC01, CM04, LAB02, RAND01, DISC01.\n"
        "Common prefixes: CT (Clinical Trial/general), INV (Investigator/staff), IP (Investigational Product), "
        "VS (Visit Schedule), SR (Safety Reporting), IC (Informed Consent), CM (Concomitant Medications), "
        "LAB (Labs/assessments), RAND (Randomization), DISC (Discontinuation).\n\n"
        "Return ONLY a JSON array. Each element must have:\n"
        '  { "id": <same id as input>, "gsop_codes": ["CODE1", "CODE2"] }\n\n'
        "- Provide 1–3 codes per item based on the deviation type and category.\n"
        "- Output ONLY the raw JSON array, no markdown fences, no commentary."
    )
    user = f"Deviation subcategories:\n\n{items_json}"
    if len(user) > 60_000:
        user = user[:60_000] + "\n...[truncated]\n"

    max_tok = int(os.environ.get("OPENAI_ENRICH_MAX_TOKENS", "4500"))
    text, err = _openai_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tok,
    )
    if err or not text:
        return {}

    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    result: Dict[str, List[str]] = {}
    for entry in parsed:
        idx = entry.get("id")
        if idx is None or idx >= len(subcategory_texts):
            continue
        orig_text = subcategory_texts[idx].get("text", "")
        codes = entry.get("gsop_codes") or []
        if isinstance(codes, list):
            result[orig_text] = [str(c) for c in codes if c]
    return result
