"""M2 — S6.2 vendor rows vs SDS CSV or GT JSON fallback."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from dmp_data import generation_fallback_used
from semantic_matcher import SemanticMatcher, verbatim_semantic_attribute_score, weighted_average


def _is_adjudication_row(data_type: str, vendor: str) -> bool:
    dt = (data_type or "").lower()
    v = (vendor or "").lower()
    return "adjudication" in dt or (v == "clario" and "adjudication" in dt)


def _normalize_tier(t: str) -> str:
    s = (t or "").strip()
    if not s:
        return ""
    cap = s[:1].upper() + s[1:].lower()
    if cap.lower() in ("critical", "supportive", "minimal"):
        return cap if cap in ("Critical", "Supportive", "Minimal") else s
    # Title Case first word
    for cand in ("Critical", "Supportive", "Minimal"):
        if cand.lower() == s.lower():
            return cand
    return s


def score_s6(
    dmp: Dict[str, Any],
    gt_rec: Dict[str, Any],
    sds_rows: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    matcher: SemanticMatcher,
    study_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    attrs = cfg.get("attributes") or {}
    fallback = generation_fallback_used(dmp)

    if fallback:
        gt_vendors: List[Dict[str, Any]] = list(gt_rec.get("S6_esource") or [])
    else:
        gt_vendors = []
        for r in sds_rows:
            dt = (r.get("data_type") or "").strip()
            vn = (r.get("vendor_name") or "").strip()
            if _is_adjudication_row(dt, vn):
                continue
            if not dt and not vn:
                continue
            gt_vendors.append(
                {
                    "data_type": dt,
                    "vendor": vn,
                    "tier": _normalize_tier(r.get("data_review_tier") or ""),
                }
            )

    s6 = dmp.get("S6_data_flow") or {}
    s62 = s6.get("S6_2_esource_edata") or {}
    gen_list = list(s62.get("vendors") or []) if isinstance(s62, dict) else []
    parent_source = (s62.get("source") or s6.get("source") or "") if isinstance(s62, dict) else ""

    a_v = attrs.get("s6_vendor_name") or {}
    a_dt = attrs.get("s6_data_type") or {}
    a_tier = attrs.get("s6_data_review_tier") or {}
    a_src = attrs.get("s6_source_tag") or {}

    used_gen: set[int] = set()
    rows: List[Dict[str, Any]] = []

    def score_pair(gt: Dict[str, Any], g: Dict[str, Any]) -> Tuple[float, str, Dict[str, Any]]:
        gv = str(gt.get("vendor") or "")
        ggv = str(g.get("vendor") or "")
        gdt = str(gt.get("data_type") or "")
        gdg = str(g.get("data_type") or "")
        tt = _normalize_tier(str(gt.get("tier") or ""))
        tg = _normalize_tier(str(g.get("tier") or ""))

        sv, ml1, d1 = verbatim_semantic_attribute_score(gv, ggv, a_v, matcher)
        sdt, ml2, d2 = verbatim_semantic_attribute_score(gdt, gdg, a_dt, matcher)
        tier_ok = 1.0 if (not tt and not tg) or (tt == tg) else 0.0
        tier_detail = {
            "score": tier_ok,
            "match_type": "exact",
            "generated": tg or None,
            "ground_truth": tt or None,
        }
        src_ok = bool(parent_source or g.get("source"))
        src_score = 1.0 if src_ok else 0.0
        w = [
            float(a_v.get("weight", 0.4)),
            float(a_dt.get("weight", 0.25)),
            float(a_tier.get("weight", 0.25)),
            float(a_src.get("weight", 0.1)),
        ]
        comp = weighted_average(w, [sv, sdt, tier_ok, src_score])
        total = round(100.0 * comp, 2)
        ms = "verbatim" if comp >= 0.99 else ("near_miss" if comp >= 0.75 else "mismatch")
        detail_attrs = {
            "s6_vendor_name": {"score": round(sv, 2), "match_type": ml1, **d1},
            "s6_data_type": {"score": round(sdt, 2), "match_type": ml2, **d2, "generated": gdg, "ground_truth": gdt},
            "s6_data_review_tier": tier_detail,
            "s6_source_tag": {
                "score": src_score,
                "match_type": "boolean",
                "generated": f"sds_csv:{study_id}" if src_ok else None,
                "valid": src_ok,
            },
        }
        return total, ms, detail_attrs

    # Greedy match GT -> Gen
    for i, gt in enumerate(gt_vendors):
        best_j = -1
        best_sc = -1.0
        best_pack: Tuple[float, str, Dict[str, Any]] = (0.0, "mismatch", {})
        for j, g in enumerate(gen_list):
            if j in used_gen:
                continue
            total, ms, det = score_pair(gt, g)
            if total > best_sc:
                best_sc = total
                best_j = j
                best_pack = (total, ms, det)
        if best_j >= 0 and best_sc >= 0:
            used_gen.add(best_j)
            g = gen_list[best_j]
            rows.append(
                {
                    "ground_truth_vendor": gt.get("vendor"),
                    "generated_vendor": g.get("vendor"),
                    "item_score": best_pack[0],
                    "match_status": best_pack[1],
                    "attributes": best_pack[2],
                    "_source_tag_valid": bool(best_pack[2].get("s6_source_tag", {}).get("valid")),
                }
            )
        else:
            rows.append(
                {
                    "ground_truth_vendor": gt.get("vendor"),
                    "generated_vendor": None,
                    "item_score": 0.0,
                    "match_status": "miss",
                    "attributes": {},
                    "_source_tag_valid": True,
                }
            )

    # Extra gen rows (hallucinations / extra)
    for j, g in enumerate(gen_list):
        if j in used_gen:
            continue
        if _is_adjudication_row(str(g.get("data_type")), str(g.get("vendor"))):
            # Explicitly out of scope for S6.2 vendor scoring.
            continue
        rows.append(
            {
                "ground_truth_vendor": None,
                "generated_vendor": g.get("vendor"),
                "item_score": 0.0,
                "match_status": "extra",
                "note": "Unmatched vendor row — possible hallucination or GT gap",
                "_source_tag_valid": bool(parent_source or g.get("source")),
            }
        )

    n_gt = len(gt_vendors)
    n_gen = len(gen_list)
    denom = max(n_gt, n_gen, 1)
    sec = sum(float(r["item_score"]) for r in rows if isinstance(r.get("item_score"), (int, float))) / denom

    meta = {
        "generated_count": n_gen,
        "ground_truth_count": n_gt,
        "matched": len(used_gen),
        "score": round(min(100.0, sec), 2),
        "fallback_used": fallback,
    }
    return rows, meta
