"""
Scenario 1 report enhancement — overall score + structured improvement actions + out-of-scope.

What this module adds on top of ``build_reference_eval_markdown``:

1. **Overall score (0–100).** Per category:

       credit     = verbatim×1.0 + near_miss×0.99 + truncated×0.80 + missed×0
       denominator = max(gt_count, gt_count + extra_count)
       cat_pct    = (credit / denominator) × 100

   Extras reduce the score by inflating the denominator (precision penalty)
   rather than subtracting, so scores never go negative. The overall score is
   the average of all *active* category percentages (categories where at least
   one GT or generated item exists). Out-of-scope rows (LLM-flagged) are
   excluded from both numerator and denominator.

2. **LLM pass** (OpenAI, ``OPENAI_API_KEY`` / ``OPENAI_MODEL``; soft-fails) that
   produces a single JSON object with:
     - ``executive_summary``   (what to improve in the generator)
     - ``actions[]``           (priority / category / type / action / fix_location)
     - ``out_of_scope[]``      (category / text / rationale)
     - ``extra_classifications[]`` (per extra: "inferable" / "partial" / "full")

3. **Markdown overlay** — ``apply_to_markdown`` inserts the new sections into
   the existing Scenario 1 Markdown:
     - "Summary - What to Improve" right after the DOC_SCORE panel
     - Section 4 "Improvement Actions" body replaced with the structured table
     - New section "Out of scope - not penalised" at the end

Gating:
    PIPD_SCENARIO1_IMPROVEMENT=0  -> skip entirely (default is ON)
    OPENAI_API_KEY missing        -> deterministic fallbacks (no LLM text)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.eval_scenario1 import NUM_CATEGORIES

TIER_VERBATIM = 1.0
TIER_NEAR_MISS = 0.99
TIER_TRUNCATED = 0.80
TIER_MISSED = 0.0
EXTRA_INFERABLE = 0.0
EXTRA_PARTIAL = -0.5
EXTRA_FULL = -1.0


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _nm_tier_for(nm: Dict[str, Any]) -> float:
    """Map a near-miss record to the user-specified tier multiplier."""
    tier = str(nm.get("tier") or "").strip().upper()
    rc = str(nm.get("root_cause") or "").strip().upper()
    if tier == "A" or rc in ("NUMBERING_ERROR", "CRITERION_FORMAT"):
        return TIER_NEAR_MISS
    if tier == "P":
        return TIER_NEAR_MISS
    return TIER_TRUNCATED


def _near_miss_gen_lookup(s1: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for nm in s1.get("near_misses") or []:
        if not isinstance(nm, dict):
            continue
        cn = nm.get("category_num")
        if cn is None:
            continue
        gen = str(nm.get("generated_text") or "").strip()
        if gen:
            out[(int(cn), gen)] = nm
    return out


def _near_miss_gt_lookup(s1: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for nm in s1.get("near_misses") or []:
        if not isinstance(nm, dict):
            continue
        cn = nm.get("category_num")
        if cn is None:
            continue
        gt = str(nm.get("gt_text") or "").strip()
        if gt:
            out[(int(cn), gt)] = nm
    return out


def _usdm_trace_by_text(result: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """(cat, subcat_text) -> {usdm_symbol, usdm_source, usdm_entity}."""
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for r in (result.get("intelligence_truth") or {}).get("per_subcategory_usdm") or []:
        if not isinstance(r, dict):
            continue
        cn = r.get("category_num")
        if cn is None:
            continue
        t = str(r.get("subcategory_text") or "").strip()
        if not t:
            continue
        out[(int(cn), t)] = {
            "usdm_symbol": r.get("usdm_symbol"),
            "usdm_source": r.get("usdm_source"),
            "usdm_entity": r.get("usdm_entity"),
            "usdm_entity_id": r.get("usdm_entity_id"),
        }
    return out


def _category_names(result: Dict[str, Any]) -> Dict[int, str]:
    meta = result.get("report_metadata") or {}
    names = dict(meta.get("category_names") or {})
    try:
        from utils.pipd_eval_config import (
            category_weights_and_names,
            load_pipd_eval_config,
            resolve_eval_config_path,
        )
        cfg = load_pipd_eval_config(resolve_eval_config_path())
        _cw, yaml_names = category_weights_and_names(cfg)
        for k, v in (yaml_names or {}).items():
            names.setdefault(int(k), str(v))
    except Exception:
        pass
    return {int(k): str(v) for k, v in names.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _build_category_items(
    s1: Dict[str, Any],
) -> Dict[int, Dict[str, List[Dict[str, Any]]]]:
    """
    Returns per-category:
        {"gt": [ {text, tier (float)}, ... ], "extras": [ {text}, ... ]}

    Tier assignment for GT items:
      - matched_subcats (verbatim)              -> 1.00
      - near-miss with gt_text (tier mapping)   -> 0.99 or 0.80
      - missed_subcats                           -> 0
    """
    out: Dict[int, Dict[str, List[Dict[str, Any]]]] = {
        cn: {"gt": [], "extras": []} for cn in range(1, NUM_CATEGORIES + 1)
    }
    per = s1.get("per_category") or {}
    nm_by_gt = _near_miss_gt_lookup(s1)

    for cn in range(1, NUM_CATEGORIES + 1):
        block = per.get(cn) or per.get(str(cn)) or {}
        matched = block.get("matched_subcats") or []
        missed = block.get("missed_subcats") or []
        hall = block.get("hallucinated_subcats") or []

        for t in matched:
            txt = str(t or "").strip()
            if not txt:
                continue
            out[cn]["gt"].append({"text": txt, "tier": TIER_VERBATIM, "match": "verbatim"})

        for t in missed:
            txt = str(t or "").strip()
            if not txt:
                continue
            nm = nm_by_gt.get((cn, txt))
            if nm is not None:
                tier = _nm_tier_for(nm)
                label = "near_miss" if tier == TIER_NEAR_MISS else "truncated"
                out[cn]["gt"].append(
                    {
                        "text": txt,
                        "tier": tier,
                        "match": label,
                        "generated_text": str(nm.get("generated_text") or ""),
                        "root_cause": nm.get("root_cause"),
                    }
                )
            else:
                out[cn]["gt"].append({"text": txt, "tier": TIER_MISSED, "match": "missed"})

        for t in hall:
            txt = str(t or "").strip()
            if txt:
                out[cn]["extras"].append({"text": txt})

    return out


def _compute_weighted_scores(
    items_by_cat: Dict[int, Dict[str, List[Dict[str, Any]]]],
    oos_keys: Optional[set] = None,
    extra_classes: Optional[Dict[Tuple[int, str], str]] = None,
) -> Dict[str, Any]:
    """
    Compute per-category and overall weighted scores.

    Per-category score (0–100):
        credit = verbatim×1.0 + near_miss×0.99 + truncated×0.80 + missed×0
        denominator = max(gt_count, gt_count + extra_count)
        category_pct = (credit / denominator) × 100   if denominator > 0 else 100

    Extras reduce the score by inflating the denominator (precision penalty)
    instead of subtracting, so scores never go negative.

    Overall = mean of category_pct across categories that have at least one
    GT item or one generated item (empty-on-both-sides categories are excluded).
    """
    oos_keys = oos_keys or set()

    n_cats = NUM_CATEGORIES
    per_cat: List[Dict[str, Any]] = []
    active_scores: List[float] = []

    for cn in range(1, n_cats + 1):
        bucket = items_by_cat.get(cn) or {"gt": [], "extras": []}
        gt_items = [it for it in bucket["gt"] if (cn, it["text"]) not in oos_keys]
        extras = [it for it in bucket["extras"] if (cn, it["text"]) not in oos_keys]
        gt_count = len(gt_items)
        extra_count = len(extras)

        verbatim = near = truncated = missed = 0
        credit = 0.0
        for it in gt_items:
            credit += it["tier"]
            m = it.get("match")
            if m == "verbatim":
                verbatim += 1
            elif m == "near_miss":
                near += 1
            elif m == "truncated":
                truncated += 1
            else:
                missed += 1

        denom = max(gt_count, gt_count + extra_count)
        if denom > 0:
            cat_pct = round(100.0 * credit / denom, 2)
        elif gt_count == 0 and extra_count == 0:
            cat_pct = 100.0
        else:
            cat_pct = 0.0

        has_activity = gt_count > 0 or extra_count > 0
        if has_activity:
            active_scores.append(cat_pct)

        per_cat.append(
            {
                "category_num": cn,
                "gt_count": gt_count,
                "extra_count": extra_count,
                "verbatim": verbatim,
                "near_miss": near,
                "truncated": truncated,
                "missed": missed,
                "score": cat_pct,
            }
        )

    overall = round(sum(active_scores) / len(active_scores), 2) if active_scores else 0.0

    return {
        "active_categories": len(active_scores),
        "per_category": per_cat,
        "overall": round(overall, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM context + call
# ─────────────────────────────────────────────────────────────────────────────

_LLM_CACHE: Dict[str, Dict[str, Any]] = {}


def _build_llm_context(
    s1: Dict[str, Any],
    result: Dict[str, Any],
    items_by_cat: Dict[int, Dict[str, List[Dict[str, Any]]]],
) -> Dict[str, Any]:
    cat_names = _category_names(result)
    trace = _usdm_trace_by_text(result)
    nm_by_gt = _near_miss_gt_lookup(s1)

    misses: List[Dict[str, Any]] = []
    near_rows: List[Dict[str, Any]] = []
    extras: List[Dict[str, Any]] = []

    for cn, bucket in items_by_cat.items():
        cname = cat_names.get(cn, f"Category {cn}")
        for it in bucket["gt"]:
            t = it["text"]
            tr = trace.get((cn, t)) or {}
            if it["match"] == "missed":
                misses.append(
                    {
                        "category_num": cn,
                        "category_name": cname,
                        "gt_text": t,
                        "usdm_symbol": tr.get("usdm_symbol"),
                        "usdm_entity": tr.get("usdm_entity"),
                        "usdm_source": (str(tr.get("usdm_source") or "")[:300] or None),
                    }
                )
            elif it["match"] in ("near_miss", "truncated"):
                nm = nm_by_gt.get((cn, t)) or {}
                near_rows.append(
                    {
                        "category_num": cn,
                        "category_name": cname,
                        "gt_text": t,
                        "generated_text": str(nm.get("generated_text") or ""),
                        "root_cause": nm.get("root_cause"),
                        "tier": it["match"],
                    }
                )
        for it in bucket["extras"]:
            t = it["text"]
            tr = trace.get((cn, t)) or {}
            extras.append(
                {
                    "category_num": cn,
                    "category_name": cname,
                    "generated_text": t,
                    "usdm_symbol": tr.get("usdm_symbol"),
                    "usdm_entity": tr.get("usdm_entity"),
                    "usdm_source": (str(tr.get("usdm_source") or "")[:300] or None),
                }
            )

    return {
        "study_id": result.get("study_id"),
        "category_names": {str(k): v for k, v in cat_names.items()},
        "misses": misses,
        "near_misses": near_rows,
        "extras": extras,
    }


def _parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        i = s.find("{")
        j = s.rfind("}")
        if i >= 0 and j > i:
            s = s[i : j + 1]
    try:
        return json.loads(s)
    except Exception:
        return None


def _call_llm(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return {}, "OPENAI_API_KEY not set"

    cache_key = json.dumps(payload, sort_keys=True, default=str)
    if cache_key in _LLM_CACHE:
        return _LLM_CACHE[cache_key], None

    try:
        from reports.pipd_composite_report import _openai_chat
    except Exception as exc:
        return {}, f"openai client unavailable: {exc}"

    system = (
        "You are reviewing a clinical-protocol PIPD generator's evaluation output. "
        "You receive the misses, near-misses, and extras with optional USDM trace "
        "(usdm_symbol / usdm_entity / usdm_source). You MUST return ONE JSON object with:\n"
        "  executive_summary: 2-4 sentences, plain prose, saying what the generator "
        "should improve next (no score numbers, no generic advice).\n"
        "  actions: array of {priority: HIGH|MEDIUM|LOW, category: 'Cat <num>', "
        "type: 'miss'|'near_miss'|'extra', action: one concrete sentence, "
        "fix_location: file-or-function hint e.g. 'usdm_extractor.py -> extract_cat10()' "
        "or 'normalization_utils.py'. Prefer real-looking module names inferred from "
        "the PIPD pipeline (usdm_extractor.py, normalization_utils.py, "
        "pipd_subcategory_patterns.yaml, confidence_assignment.py, extract_catN()).\n"
        "  out_of_scope: array of {category_num: int, text: str, rationale: str} - "
        "rows that the generator cannot reasonably predict from the protocol text "
        "(e.g. IT/systems issues, internet-outage deviations, process-only artifacts).\n"
        "  extra_classifications: array of {category_num: int, text: str, "
        "class: 'inferable'|'partial'|'full'} - 'inferable' when the extra is in USDM "
        "or directly derivable, 'partial' when USDM hints at it but not completely, "
        "'full' when it is a pure hallucination.\n"
        "Return valid JSON only. No prose outside the JSON."
    )
    ctx_json = json.dumps(payload, ensure_ascii=False, default=str)
    if len(ctx_json) > 60_000:
        ctx_json = ctx_json[:60_000] + "\n... [truncated]"
    user = "Evaluation context:\n\n" + ctx_json

    text, err = _openai_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=int(os.getenv("PIPD_SCENARIO1_IMPROVEMENT_MAX_TOKENS", "2500")),
    )
    if err:
        return {}, err
    parsed = _parse_json_from_text(text or "")
    if not isinstance(parsed, dict):
        return {}, "LLM response was not JSON"
    _LLM_CACHE[cache_key] = parsed
    return parsed, None


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic fallbacks (when LLM unavailable)
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_actions(
    items_by_cat: Dict[int, Dict[str, List[Dict[str, Any]]]],
    cat_names: Dict[int, str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cn, bucket in items_by_cat.items():
        cname = cat_names.get(cn, f"Category {cn}")
        missed = [it for it in bucket["gt"] if it["match"] == "missed"]
        nears = [it for it in bucket["gt"] if it["match"] in ("near_miss", "truncated")]
        extras = bucket["extras"]
        if missed:
            n = len(missed)
            out.append(
                {
                    "priority": "HIGH",
                    "category": f"Cat {cn}",
                    "type": "miss",
                    "action": (
                        f"{n} GT subcategory line(s) in {cname} not generated; "
                        "align extractor coverage and re-run."
                    ),
                    "fix_location": f"extract_cat{cn}() / usdm_extractor.py",
                }
            )
        if nears:
            out.append(
                {
                    "priority": "MEDIUM",
                    "category": f"Cat {cn}",
                    "type": "near_miss",
                    "action": (
                        f"{len(nears)} near-miss line(s) in {cname} - tighten wording "
                        "(numbering / truncation / paraphrase) so strict matcher scores verbatim."
                    ),
                    "fix_location": "pipd_subcategory_patterns.yaml / normalization_utils.py",
                }
            )
        if extras:
            out.append(
                {
                    "priority": "MEDIUM",
                    "category": f"Cat {cn}",
                    "type": "extra",
                    "action": (
                        f"{len(extras)} extra generated line(s) in {cname} with no GT - "
                        "filter via post-generation validation or tighten subcategory predicates."
                    ),
                    "fix_location": "post_gen_validation.py",
                }
            )
    return out


def _fallback_summary(score: Dict[str, Any]) -> str:
    overall = score.get("overall", 0.0)
    per = score.get("per_category") or []
    weak = sorted(
        [r for r in per if r.get("gt_count", 0) > 0 or r.get("extra_count", 0) > 0],
        key=lambda r: r.get("score", 0),
    )[:3]
    weak_names = ", ".join(f"Cat {r['category_num']} ({r['score']:.0f}%)" for r in weak)
    return (
        f"Scenario 1 score {overall:.1f} / 100. "
        + (f"Weakest categories: {weak_names}. " if weak_names else "")
        + "Focus the next generator iteration on missed GT items and reducing extras."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────

def _render_summary_block(
    score: Dict[str, Any],
    summary_text: str,
    cat_names: Dict[int, str],
) -> List[str]:
    lines: List[str] = []
    lines.append("## Summary - What to Improve")
    lines.append("")
    overall = score.get("overall", 0.0)
    n_active = score.get("active_categories", 0)
    lines.append(
        f"**Overall score (Scenario 1): {overall:.1f} / 100** "
        f"_(averaged across {n_active} active categories)._"
    )
    lines.append("")
    lines.append(summary_text.strip() or _fallback_summary(score))
    lines.append("")
    return lines


_PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _normalize_action(a: Dict[str, Any]) -> Dict[str, Any]:
    pr = str(a.get("priority") or "MEDIUM").strip().upper()
    if pr not in _PRIORITY_ORDER:
        pr = "MEDIUM"
    cat_raw = str(a.get("category") or "").strip()
    if not cat_raw.lower().startswith("cat"):
        cn = a.get("category_num")
        cat_raw = f"Cat {cn}" if cn is not None else "Cat -"
    typ = str(a.get("type") or "miss").strip().lower()
    if typ not in ("miss", "near_miss", "extra"):
        if typ in ("missed",):
            typ = "miss"
        elif typ in ("hallucination", "extras"):
            typ = "extra"
        elif typ in ("near-miss", "nearmiss", "inferred"):
            typ = "near_miss"
        else:
            typ = "miss"
    return {
        "priority": pr,
        "category": cat_raw,
        "type": typ,
        "action": str(a.get("action") or "-").strip(),
        "fix_location": str(a.get("fix_location") or "-").strip(),
    }


def _render_actions_section(actions: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    lines.append("## 4. Improvement Actions")
    lines.append("")
    lines.append("Actions derived from eval scoring. Prioritised for generator developer.")
    lines.append("")

    normed = [_normalize_action(a) for a in actions]

    def _cat_num(a: Dict[str, Any]) -> int:
        m = re.search(r"(\d+)", a.get("category") or "")
        return int(m.group(1)) if m else 999

    normed.sort(key=lambda a: (_cat_num(a), _PRIORITY_ORDER.get(a["priority"], 1)))

    if not normed:
        lines.append("_No improvement actions - Scenario 1 is within target._")
        lines.append("")
        return lines

    lines.append("| Priority | Category | Type | Action | Fix location |")
    lines.append("|----------|----------|------|--------|--------------|")
    for a in normed:
        lines.append(
            f"| {a['priority']} | {a['category']} | {a['type']} | {a['action']} | {a['fix_location']} |"
        )
    lines.append("")
    return lines


def _render_oos_section(oos: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    lines.append("## 5. Out of scope - not penalised")
    lines.append("")
    if not oos:
        lines.append("_No out-of-scope rows detected._")
        lines.append("")
        return lines
    lines.append(
        f"{len(oos)} row(s) excluded from scoring. "
        "Generator is not expected to predict these from protocol text."
    )
    lines.append("")
    lines.append("| Category | Text | Rationale |")
    lines.append("|----------|------|-----------|")
    for row in oos:
        cn = row.get("category_num")
        cat = f"Cat {cn}" if cn is not None else "-"
        txt = str(row.get("text") or "-").replace("|", "\\|")[:400]
        why = str(row.get("rationale") or "-").replace("|", "\\|")[:400]
        lines.append(f"| {cat} | {txt} | {why} |")
    lines.append("")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_improvement_payload(
    s1: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute everything needed to render the overlay (usable without Markdown)."""
    items_by_cat = _build_category_items(s1)
    cat_names = _category_names(result)

    if _env_on("PIPD_SCENARIO1_IMPROVEMENT_LLM", True):
        payload = _build_llm_context(s1, result, items_by_cat)
        llm, llm_err = _call_llm(payload)
    else:
        llm, llm_err = {}, "LLM disabled by env"

    oos_raw = llm.get("out_of_scope") or []
    oos_keys: set = set()
    oos_rows: List[Dict[str, Any]] = []
    for row in oos_raw if isinstance(oos_raw, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            cn = int(row.get("category_num"))
        except (TypeError, ValueError):
            continue
        txt = str(row.get("text") or "").strip()
        if not txt:
            continue
        oos_keys.add((cn, txt))
        oos_rows.append(
            {
                "category_num": cn,
                "text": txt,
                "rationale": str(row.get("rationale") or "").strip(),
            }
        )

    extra_classes: Dict[Tuple[int, str], str] = {}
    for row in (llm.get("extra_classifications") or []) if isinstance(llm.get("extra_classifications"), list) else []:
        if not isinstance(row, dict):
            continue
        try:
            cn = int(row.get("category_num"))
        except (TypeError, ValueError):
            continue
        txt = str(row.get("text") or "").strip()
        cls = str(row.get("class") or "").strip().lower()
        if not txt or cls not in ("inferable", "partial", "full"):
            continue
        extra_classes[(cn, txt)] = cls

    score = _compute_weighted_scores(items_by_cat, oos_keys=oos_keys, extra_classes=extra_classes)

    actions_raw = llm.get("actions") if isinstance(llm.get("actions"), list) else []
    if not actions_raw:
        actions_raw = _fallback_actions(items_by_cat, cat_names)

    summary = str(llm.get("executive_summary") or "").strip() or _fallback_summary(score)

    return {
        "score": score,
        "summary": summary,
        "actions": actions_raw,
        "out_of_scope": oos_rows,
        "extra_classifications": [
            {"category_num": k[0], "text": k[1], "class": v}
            for k, v in extra_classes.items()
        ],
        "llm_error": llm_err,
    }


_DOC_SCORE_RE = re.compile(r"<!--\s*DOC_SCORE:.*?-->\s*\n", re.DOTALL)
_SECTION4_RE = re.compile(
    r"##\s*4\.\s*Improvement Actions.*?(?=(^##\s|\Z))",
    re.DOTALL | re.MULTILINE,
)


def apply_to_markdown(
    md: str,
    s1: Dict[str, Any],
    result: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Overlay Summary / weighted table / structured §4 / Out-of-scope section on
    the markdown produced by ``build_reference_eval_markdown``.

    Returns (new_markdown, payload). ``payload`` is also stored on ``result``
    under ``scenario1_improvement`` for downstream consumers (JSON / YAML).
    """
    if not _env_on("PIPD_SCENARIO1_IMPROVEMENT", True):
        return md, {}

    payload = build_improvement_payload(s1, result)
    result["scenario1_improvement"] = payload
    try:
        overall = float(payload["score"]["overall"])
        result["overall_score_percent"] = round(overall, 1)
    except Exception:
        pass

    cat_names = _category_names(result)
    summary_lines = _render_summary_block(payload["score"], payload["summary"], cat_names)
    actions_lines = _render_actions_section(payload["actions"])
    oos_lines = _render_oos_section(payload["out_of_scope"])

    new_md = md

    # 1) Insert summary after DOC_SCORE comment line (falls back to prepending
    #    before "## 1." if no DOC_SCORE marker).
    summary_block = "\n".join(summary_lines) + "\n"
    m = _DOC_SCORE_RE.search(new_md)
    if m:
        insert_at = m.end()
        new_md = new_md[:insert_at] + summary_block + new_md[insert_at:]
    else:
        anchor = "\n## 1. Summary Metrics"
        idx = new_md.find(anchor)
        if idx >= 0:
            new_md = new_md[:idx] + "\n" + summary_block + new_md[idx:]
        else:
            new_md = summary_block + new_md

    # 2) Replace or append §4 Improvement Actions with structured table.
    actions_block = "\n".join(actions_lines) + "\n"
    if _SECTION4_RE.search(new_md):
        new_md = _SECTION4_RE.sub(actions_block, new_md, count=1)
    else:
        new_md = new_md.rstrip() + "\n\n" + actions_block

    # 3) Append §5 Out of scope at the very end.
    oos_block = "\n".join(oos_lines) + "\n"
    new_md = new_md.rstrip() + "\n\n" + oos_block

    # Also drop the now-stale DOC_SCORE panel's baked-in percent so the
    # consumers (Markdown-to-docx panel) pick up the new weighted score.
    def _rewrite_doc_score(m: "re.Match[str]") -> str:
        text = m.group(0)
        try:
            overall = float(payload["score"]["overall"])
            text = re.sub(r"score=[^|]+", f"score={overall:.1f}", text, count=1)
        except Exception:
            pass
        return text

    new_md = _DOC_SCORE_RE.sub(_rewrite_doc_score, new_md, count=1)
    return new_md, payload
