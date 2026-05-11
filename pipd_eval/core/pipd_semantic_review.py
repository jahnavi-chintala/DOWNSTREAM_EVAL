"""
Optional LLM semantic review for PIPD Scenario 1: pairs missed GT lines with extra
generated lines in the same category and scores semantic equivalence / truncation.

Does not change M1 unless ``apply_to_m1`` is True (caller passes
``semantic_review_affects_m1`` from ``run_eval``).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reports.pipd_composite_report import _openai_chat

# Bind PIPD's eval_scenario1 constants / classifier at module import time.
# Doing this at the top (eagerly, at server startup) ensures we capture this
# product's ``eval_scenario1`` before other products register modules with the
# same name in ``sys.modules``. Importing lazily inside a function would
# resolve to whichever product happens to own ``sys.modules['eval_scenario1']``
# at request time, which breaks in the unified hub.
from core.eval_scenario1 import NUM_CATEGORIES, TARGETS, classify_failures

# Default credits when model returns verdict without explicit credit.
# Must stay in sync with eval_scenario1 constants (SEMANTIC_CREDIT, TRUNCATION_CREDIT).
_VERDICT_DEFAULT_CREDIT = {
    "semantic_equivalent": 0.85,
    "truncated": 0.60,
    "unrelated": 0.0,
}

_MAX_LINES_PER_SIDE = 14
_MAX_LINE_CHARS = 900
_MAX_USDM_CHARS = 12000


def _truncate(s: str, n: int) -> str:
    t = (s or "").replace("\r", " ").replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def load_usdm_snippet(usdm_path: Optional[str], *, max_chars: int = _MAX_USDM_CHARS) -> str:
    """First ``max_chars`` chars of USDM JSON for model context (protocol grounding)."""
    if not usdm_path:
        return ""
    p = Path(usdm_path)
    if not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return raw[:max_chars] + ("\n...[USDM truncated]\n" if len(raw) > max_chars else "")


def _parse_json_response(raw: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not raw:
        return None, "empty model response"
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"


def review_category_miss_extra_pairs(
    *,
    study_id: str,
    category_num: int,
    category_name: str,
    missed: List[str],
    extras: List[str],
    usdm_snippet: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    One OpenAI call: propose pairings between missed GT lines and extra generated lines.

    Returns ({ "pairings": [...] }, error_or_none).
    """
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None, "OPENAI_API_KEY not set"

    missed = [m for m in missed if (m or "").strip()][: _MAX_LINES_PER_SIDE]
    extras = [e for e in extras if (e or "").strip()][: _MAX_LINES_PER_SIDE]
    if not missed or not extras:
        return {"pairings": [], "skipped": "no_miss_or_no_extra"}, None

    gt_block = "\n".join(f"  [{i}] {_truncate(m, _MAX_LINE_CHARS)}" for i, m in enumerate(missed))
    ex_block = "\n".join(f"  [{i}] {_truncate(e, _MAX_LINE_CHARS)}" for i, e in enumerate(extras))
    usdm_part = (
        f"\n\n### USDM protocol excerpt (truncated)\n```\n{_truncate(usdm_snippet, _MAX_USDM_CHARS)}\n```\n"
        if usdm_snippet.strip()
        else "\n\n*(No USDM file provided.)*\n"
    )

    system = (
        "You align PIPD deviation lines for QA. Ground-truth (missed) lines are what the protocol "
        "expects; generated (extra) lines are from the model but not verbatim in GT.\n"
        "Pair a missed line with an extra line ONLY if they describe the **same clinical deviation** "
        "(same root issue). Use USDM excerpt only as supporting context, not as a second ground truth.\n"
        "Verdict:\n"
        "- semantic_equivalent: same deviation, different wording (e.g. label vs sentence).\n"
        "- truncated: same deviation but generated text is clearly shorter or drops detail vs GT.\n"
        "- unrelated: do not pair.\n"
        "Each missed index and each extra index may appear in **at most one** pairing.\n"
        "Output **JSON only** (no markdown fences):\n"
        '{"pairings":[{"gt_idx":0,"extra_idx":1,"verdict":"semantic_equivalent|truncated|unrelated",'
        '"credit":0.85,"reason":"one sentence"}]}\n'
        "Set credit to 0.85 for semantic_equivalent, 0.60 for truncated, 0 for unrelated (omit unrelated pairs).\n"
        "If no valid pairs, return {\"pairings\":[]}."
    )
    user = (
        f"Study: {study_id}\nCategory {category_num} — {category_name}\n\n"
        f"### Missed GT lines (indices 0..{len(missed)-1})\n{gt_block}\n\n"
        f"### Extra generated lines (indices 0..{len(extras)-1})\n{ex_block}\n"
        f"{usdm_part}"
    )

    text, err = _openai_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2500,
    )
    if err:
        return None, err
    data, perr = _parse_json_response(text)
    if perr:
        return None, perr or "parse failed"
    if not isinstance(data, dict):
        return None, "model returned non-object JSON"
    pairings = data.get("pairings")
    if pairings is None:
        return {"pairings": []}, None
    if not isinstance(pairings, list):
        return None, "pairings must be a list"
    return {"pairings": pairings}, None


def _near_miss_credit_for_category(results: Dict[str, Any], cat_num: int) -> float:
    total = 0.0
    for nm in results.get("near_misses") or []:
        if int(nm.get("category_num") or 0) != int(cat_num):
            continue
        try:
            total += float(nm.get("credit") or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def run_semantic_review_for_results(
    results: Dict[str, Any],
    *,
    usdm_json_path: Optional[str] = None,
    category_names: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """
    Run per-category semantic review where both misses and extras exist.
    Returns a dict suitable for ``results[\"semantic_review\"]`` (never mutates M1).
    """
    study_id = str(results.get("study_id") or "")
    per = results.get("per_category") or {}
    usdm_snippet = load_usdm_snippet(usdm_json_path)
    out_categories: Dict[str, Any] = {}
    errors: List[str] = []

    for cn in range(1, 12):
        block = per.get(cn) or per.get(str(cn)) or {}
        missed = list(block.get("missed_subcats") or [])
        extras = list(block.get("hallucinated_subcats") or [])
        cname = (category_names or {}).get(cn) or f"Category {cn}"
        if not missed or not extras:
            out_categories[str(cn)] = {"pairings": [], "skipped": "no_miss_or_no_extra"}
            continue
        data, err = review_category_miss_extra_pairs(
            study_id=study_id,
            category_num=cn,
            category_name=cname,
            missed=missed,
            extras=extras,
            usdm_snippet=usdm_snippet,
        )
        if err:
            out_categories[str(cn)] = {"pairings": [], "error": err}
            errors.append(f"Cat {cn}: {err}")
            continue
        raw_pairings = (data or {}).get("pairings") or []
        normalized: List[Dict[str, Any]] = []
        used_gt: set = set()
        used_ex: set = set()
        for p in raw_pairings:
            if not isinstance(p, dict):
                continue
            try:
                gi = int(p.get("gt_idx"))
                ei = int(p.get("extra_idx"))
            except (TypeError, ValueError):
                continue
            if gi < 0 or gi >= len(missed) or ei < 0 or ei >= len(extras):
                continue
            if gi in used_gt or ei in used_ex:
                continue
            verdict = str(p.get("verdict") or "unrelated").strip().lower()
            if verdict == "unrelated":
                continue
            try:
                credit = float(p.get("credit"))
            except (TypeError, ValueError):
                credit = float(_VERDICT_DEFAULT_CREDIT.get(verdict, 0.0))
            if credit <= 0:
                continue
            gt_line = missed[gi]
            gen_line = extras[ei]
            normalized.append(
                {
                    "gt_idx": gi,
                    "extra_idx": ei,
                    "gt_text": gt_line,
                    "generated_text": gen_line,
                    "verdict": verdict,
                    "credit": round(min(1.0, max(0.0, credit)), 4),
                    "reason": str(p.get("reason") or "")[:500],
                }
            )
            used_gt.add(gi)
            used_ex.add(ei)
        out_categories[str(cn)] = {"pairings": normalized}

    return {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "usdm_snippet_used": bool(usdm_snippet.strip()),
        "categories": out_categories,
        "errors": errors,
    }


def apply_semantic_review_to_m1(results: Dict[str, Any], semantic_block: Dict[str, Any]) -> None:
    """
    Mutate ``results`` in place: add semantic credits, adjust missed/extra lists,
    recompute per-category M1 recall and study-level m1_subcategory_recall metrics.

    Only uses pairings under ``semantic_block[\"categories\"][str(cat)][\"pairings\"]``.
    """
    per = results.get("per_category") or {}
    cats_sr = (semantic_block or {}).get("categories") or {}

    def _put_cat(num: int, block: Dict[str, Any]) -> None:
        if num in per:
            per[num] = block
        else:
            per[str(num)] = block

    for cn in range(1, NUM_CATEGORIES + 1):
        block = per.get(cn) or per.get(str(cn))
        if not block:
            continue
        sr = cats_sr.get(str(cn)) or {}
        pairings: List[Dict[str, Any]] = list(sr.get("pairings") or [])
        if not pairings:
            block["m1_semantic_review_credit"] = 0.0
            _put_cat(cn, block)
            continue

        missed = list(block.get("missed_subcats") or [])
        hall = list(block.get("hallucinated_subcats") or [])
        sem_credit = 0.0
        for p in pairings:
            gt_t = p.get("gt_text")
            gen_t = p.get("generated_text")
            cr = float(p.get("credit") or 0.0)
            if cr <= 0 or not gt_t or not gen_t:
                continue
            if gt_t in missed and gen_t in hall:
                sem_credit += cr
                missed = [x for x in missed if x != gt_t]
                hall = [x for x in hall if x != gen_t]
        block["missed_subcats"] = missed
        block["hallucinated_subcats"] = hall
        block["m1_semantic_review_credit"] = round(sem_credit, 4)

        gt_tot = int(block.get("m1_gt_total") or 0)
        matched_n = int(block.get("m1_matched") or 0)
        nm_cred = _near_miss_credit_for_category(results, cn)
        net = float(matched_n) + float(nm_cred) + float(block["m1_semantic_review_credit"])
        if gt_tot > 0:
            block["m1_recall"] = net / float(gt_tot)
        _put_cat(cn, block)

    results["per_category"] = per

    # Recompute study-level M1 aggregates
    study_credit = 0.0
    study_matched = 0
    study_gt_total = 0
    study_gen_total = 0
    for cn in range(1, NUM_CATEGORIES + 1):
        b = per.get(cn) or per.get(str(cn)) or {}
        gt_tot = int(b.get("m1_gt_total") or 0)
        study_matched += int(b.get("m1_matched") or 0)
        study_gt_total += gt_tot
        study_gen_total += int(b.get("m1_generated_total") or 0)
        nm_cred = _near_miss_credit_for_category(results, cn)
        sem_cred = float(b.get("m1_semantic_review_credit") or 0.0)
        study_credit += float(b.get("m1_matched") or 0) + nm_cred + sem_cred

    study_recall = study_credit / study_gt_total if study_gt_total else 1.0
    study_precision = study_credit / study_gen_total if study_gen_total else 1.0
    study_f1 = (
        (2 * study_precision * study_recall / (study_precision + study_recall))
        if (study_precision + study_recall)
        else 0.0
    )

    m1_target = TARGETS["m1_recall_aggregate"]
    m1_f1_target = TARGETS["m1_f1"]
    m1_pass = (study_recall >= m1_target) and (study_f1 >= m1_f1_target)

    m = results.setdefault("metrics", {})
    m1 = m.setdefault("m1_subcategory_recall", {})
    m1["score"] = study_recall
    m1["precision"] = study_precision
    m1["f1"] = study_f1
    m1["total_credit"] = study_credit
    m1["pass"] = m1_pass

    sr = results.setdefault("semantic_review", {})
    if isinstance(sr, dict):
        sr["applied_to_m1"] = True

    # Overall pass: recompute from existing metric passes
    m2_pass = (m.get("m2_flag_accuracy") or {}).get("pass", False)
    m3_pass = (m.get("m3_empty_category_accuracy") or {}).get("pass", False)
    m4_pass = (m.get("m4_hallucination_detection") or {}).get("pass", False)
    m5_pass = (m.get("m5_severity_match") or {}).get("pass", False)
    m6_pass = (m.get("m6_gsop_coverage") or {}).get("pass", False)
    all_pass = m1_pass and m2_pass and m3_pass and m4_pass and m5_pass and m6_pass
    results["overall_pass"] = all_pass
    results["go_no_go"] = "GO" if all_pass else "NO-GO"

    # Recompute the weighted overall score so the headline UI number
    # reflects the post-semantic-review M1.
    m4_blk = m.get("m4_hallucination_detection") or {}
    m5_blk = m.get("m5_severity_match") or {}
    m6_blk = m.get("m6_gsop_coverage") or {}
    m2_blk = m.get("m2_flag_accuracy") or {}
    m3_blk = m.get("m3_empty_category_accuracy") or {}
    h_found = int(m4_blk.get("hallucinations_found") or 0)
    m4_score = 1.0 if h_found == 0 else max(0.0, 1.0 - 0.2 * h_found)
    m6_score = m6_blk.get("score")  # may be None
    pairs = [
        ("m1", 0.50, float(study_f1)),
        ("m2", 0.15, float(m2_blk.get("auto_confirmed_accuracy") or 0.0)),
        ("m3", 0.10, float(m3_blk.get("score") or 0.0)),
        ("m4", 0.15, float(m4_score)),
        ("m5", 0.05, float(m5_blk.get("score") or 0.0)),
        ("m6", 0.05, m6_score),
    ]
    active = [(k, w, v) for (k, w, v) in pairs if v is not None]
    wsum = sum(w for _k, w, _v in active) or 1.0
    overall_01 = sum(w * float(v) for _k, w, v in active) / wsum
    results["overall_score_percent"] = round(100.0 * overall_01, 1)
    results["overall_score_0_1"] = round(overall_01, 6)

    if "classified_failures" in results:
        results["classified_failures"] = classify_failures(results)
