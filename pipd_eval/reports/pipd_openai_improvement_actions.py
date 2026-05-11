"""
Optional OpenAI narrative for §4 Improvement Actions (reference eval report).

Enable with ``PIPD_IMPROVEMENT_ACTIONS_OPENAI=1`` (or ``true``) and ``OPENAI_API_KEY``.
The model is given structured **ground truth vs generated** pairs for missed subcategories
and near-misses so it can write a concise summary instead of repeating generic templates.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def build_improvement_actions_context(
    s1: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Facts-only payload for the improvement-actions prompt (no scores)."""
    study_id = str(s1.get("study_id") or "")
    per = s1.get("per_category") or {}
    near = s1.get("near_misses") or []
    failures = s1.get("classified_failures") or []

    sem_llm: List[Dict[str, Any]] = []
    sr = ((result or {}).get("semantic_review") or {}) if result else (
        s1.get("semantic_review") or {}
    )
    if isinstance(sr, dict):
        for key, block in (sr.get("categories") or {}).items():
            if not isinstance(block, dict):
                continue
            for p in block.get("pairings") or []:
                if not isinstance(p, dict):
                    continue
                sem_llm.append(
                    {
                        "category_num": key,
                        "gt_text": str(p.get("gt_text") or "").strip(),
                        "generated_text": str(p.get("generated_text") or "").strip(),
                        "verdict": p.get("verdict"),
                        "credit": p.get("credit"),
                        "note": (str(p.get("reason") or "")[:500]),
                    }
                )

    missing: List[Dict[str, Any]] = []
    extras: List[Dict[str, Any]] = []
    for f in failures:
        if not isinstance(f, dict):
            continue
        ft = str(f.get("failure_type") or "")
        if ft == "M1_MISSING_SUBCAT":
            missing.append(
                {
                    "category_num": f.get("category_num"),
                    "ground_truth_text": str(f.get("example") or "").strip(),
                }
            )
        elif ft == "M1_HALLUCINATED":
            extras.append(
                {
                    "category_num": f.get("category_num"),
                    "generated_text": str(f.get("example") or "").strip(),
                }
            )

    near_pairs: List[Dict[str, Any]] = []
    for nm in near:
        if not isinstance(nm, dict):
            continue
        near_pairs.append(
            {
                "category_num": nm.get("category_num"),
                "generated_text": str(nm.get("generated_text") or "").strip(),
                "ground_truth_text": str(nm.get("gt_text") or "").strip(),
                "tier": str(nm.get("tier") or "").strip(),
                "credit": nm.get("credit"),
            }
        )

    other: List[Dict[str, Any]] = []
    for f in failures:
        if not isinstance(f, dict):
            continue
        ft = str(f.get("failure_type") or "")
        if ft in ("M1_MISSING_SUBCAT", "M1_HALLUCINATED"):
            continue
        other.append(
            {
                "failure_type": ft,
                "category_num": f.get("category_num"),
                "detail": str(f.get("example") or "").strip(),
            }
        )

    # Per-category GT vs gen counts (helps the model prioritise without inventing)
    cat_summary: List[Dict[str, Any]] = []
    for cn in range(1, 12):
        b = per.get(cn) or per.get(str(cn)) or {}
        if not isinstance(b, dict):
            continue
        cat_summary.append(
            {
                "category_num": cn,
                "m1_gt_total": int(b.get("m1_gt_total") or 0),
                "m1_matched": int(b.get("m1_matched") or 0),
                "m1_near_misses": int(b.get("m1_near_misses") or 0),
                "missed_count": len(b.get("missed_subcats") or []),
                "hallucinated_count": len(b.get("hallucinated_subcats") or []),
            }
        )

    return {
        "study_id": study_id,
        "category_subcategory_counts": cat_summary,
        "missing_ground_truth_lines": missing,
        "near_miss_generated_vs_ground_truth": near_pairs,
        "extra_generated_not_in_ground_truth": extras,
        "other_typed_failures": other,
        "semantic_llm_pairings": sem_llm,
    }


def fetch_improvement_actions_narrative(
    s1: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Returns Markdown body (no outer heading) or None if skipped / API error.
    Respects ``PIPD_IMPROVEMENT_ACTIONS_OPENAI`` and ``OPENAI_API_KEY``.
    Pass ``result`` (composite / upload result) so semantic-review pairings are included.
    """
    flag = os.getenv("PIPD_IMPROVEMENT_ACTIONS_OPENAI", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None

    ctx = build_improvement_actions_context(s1, result)
    if (
        not ctx["missing_ground_truth_lines"]
        and not ctx["near_miss_generated_vs_ground_truth"]
        and not ctx["extra_generated_not_in_ground_truth"]
        and not ctx["other_typed_failures"]
        and not (ctx.get("semantic_llm_pairings") or [])
    ):
        return None

    payload = json.dumps(ctx, indent=2, ensure_ascii=False, default=str)
    if len(payload) > 48_000:
        payload = payload[:48_000] + "\n… [truncated]"

    # Lazy import avoids circular import with pipd_composite_report
    from reports.pipd_composite_report import _openai_chat

    system = (
        "You write short, actionable remediation summaries for developers of a PIPD "
        "(Potential Important Protocol Deviations) generator. "
        "Use ONLY the JSON facts: ground-truth lines that were missing from generated output, "
        "pairs where generated text was a near-miss versus GT (including PARAPHRASE / tier P when present), "
        "semantic_llm_pairings (LLM-judged GT vs generated with verdict/credit), "
        "and extras not in GT. "
        "When a miss and an extra look like the same deviation in different wording, say that explicitly "
        "(contrast the two strings; do not give a generic 'add training data' line). "
        "For misses: state clearly that GT required the line and it was not produced. "
        "For near-misses: briefly contrast generated vs GT wording and what to tighten. "
        "Do not invent protocol content or counts not in the JSON. "
        "Output Markdown: start with a short paragraph, then optional bullet lists by category. "
        "Avoid repeating the same generic sentence for every item; synthesise themes. "
        "Do not include a top-level # heading."
    )
    user = (
        "Summarise improvement priorities from this evaluation context:\n\n" + payload
    )
    text, err = _openai_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=int(os.getenv("PIPD_IMPROVEMENT_ACTIONS_MAX_TOKENS", "1200")),
    )
    if err:
        return f"*(OpenAI improvement summary unavailable: {err})*"
    return (text or "").strip() or None
