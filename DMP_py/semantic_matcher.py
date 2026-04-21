"""
Semantic similarity for DMP eval (S5/S6/S8 string matching).

Uses sentence-transformers/all-MiniLM-L6-v2 when available (per dmp_eval_config.yaml);
falls back to normalized Levenshtein ratio.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from Levenshtein import distance as _lev
except ImportError:
    _lev = None


def _lev_ratio(a: str, b: str) -> float:
    a, b = (a or "").strip(), (b or "").strip()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a.lower() == b.lower():
        return 1.0
    if _lev is not None:
        d = _lev(a.lower(), b.lower())
        return max(0.0, 1.0 - d / max(len(a), len(b), 1))
    # difflib-like quick ratio
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class SemanticMatcher:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self._model = None
        self._enabled = bool(cfg and cfg.get("enable", True))
        self._model_name = (cfg or {}).get("model", "sentence-transformers/all-MiniLM-L6-v2")
        if self._enabled:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                self._model = SentenceTransformer(self._model_name)
            except Exception:
                self._model = None

    def cosine_pair(self, a: str, b: str) -> float:
        if self._model is None:
            return _lev_ratio(a, b)
        ta = (a or "").strip()
        tb = (b or "").strip()
        if not ta and not tb:
            return 1.0
        if not ta or not tb:
            return 0.0
        emb = self._model.encode([ta, tb], convert_to_numpy=True, show_progress_bar=False)
        na = float(max(1e-9, (emb[0] ** 2).sum() ** 0.5))
        nb = float(max(1e-9, (emb[1] ** 2).sum() ** 0.5))
        dot = float((emb[0] * emb[1]).sum())
        return max(0.0, min(1.0, dot / (na * nb)))

    def batch_cosine_matrix(self, left: List[str], right: List[str]):
        """Return len(left) x len(right) cosine matrix (or Levenshtein proxy)."""
        if not left or not right:
            return []
        if self._model is None:
            return [[_lev_ratio(a, b) for b in right] for a in left]
        e1 = self._model.encode(list(left), convert_to_numpy=True, show_progress_bar=False)
        e2 = self._model.encode(list(right), convert_to_numpy=True, show_progress_bar=False)
        out: List[List[float]] = []
        for i in range(len(left)):
            row: List[float] = []
            n1 = max(1e-9, float((e1[i] ** 2).sum() ** 0.5))
            for j in range(len(right)):
                n2 = max(1e-9, float((e2[j] ** 2).sum() ** 0.5))
                dot = float((e1[i] * e2[j]).sum())
                row.append(max(0.0, min(1.0, dot / (n1 * n2))))
            out.append(row)
        return out


def verbatim_semantic_attribute_score(
    gt_text: str,
    gen_text: str,
    attr_cfg: Dict[str, Any],
    matcher: SemanticMatcher,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    Returns (score 0-1, match_label, detail dict).
    match_label one of: verbatim, near_miss, semantic_high, semantic_med, semantic_low, mismatch
    """
    gt_s = (gt_text or "").strip()
    gen_s = (gen_text or "").strip()
    detail: Dict[str, Any] = {}

    if not gt_s and not gen_s:
        return 1.0, "verbatim", detail
    if not gt_s or not gen_s:
        return 0.0, "mismatch", detail

    if gt_s.lower() == gen_s.lower():
        return 1.0, "verbatim", detail

    lev = _lev(gt_s.lower(), gen_s.lower()) if _lev is not None else 999
    detail["levenshtein"] = int(lev)
    vm = (attr_cfg.get("verbatim_fallback") or {})
    lev_max = int(vm.get("levenshtein_max", 5))
    lev_score = float(vm.get("levenshtein_score", 0.8))
    if lev <= lev_max:
        return lev_score, "near_miss", detail

    cos = matcher.cosine_pair(gt_s, gen_s)
    detail["cosine_similarity"] = round(float(cos), 4)
    sf = (attr_cfg.get("semantic_fallback") or {})
    hi_th = float(sf.get("high_threshold", 0.85))
    hi_sc = float(sf.get("high_score", 0.6))
    med_th = float(sf.get("medium_threshold", 0.70))
    med_sc = float(sf.get("medium_score", 0.3))
    low_sc = float(sf.get("low_score", 0.0))
    if cos >= hi_th:
        return hi_sc, "semantic_high", detail
    if cos >= med_th:
        return med_sc, "semantic_med", detail
    return low_sc, "semantic_low", detail


def tier_score_fn(
    gen_val: str,
    exp_val: str,
    attr_cfg: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    tiers = [str(t).strip().lower() for t in (attr_cfg.get("tiers") or [])]
    ts = (attr_cfg.get("tier_scores") or {})
    exact = float(ts.get("exact_match", 1.0))
    adj = float(ts.get("adjacent_tier", 0.5))
    beyond = float(ts.get("beyond_adjacent", 0.0))
    g = str(gen_val or "").strip().lower()
    e = str(exp_val or "").strip().lower()
    if g == e:
        return exact, {"match_type": "tier", "generated": gen_val, "expected": exp_val}
    try:
        ig = tiers.index(g) if g in tiers else -1
        ie = tiers.index(e) if e in tiers else -1
    except ValueError:
        ig, ie = -1, -1
    if ig < 0 or ie < 0:
        return beyond, {"match_type": "tier", "generated": gen_val, "expected": exp_val}
    if abs(ig - ie) == 1:
        return adj, {"match_type": "tier", "generated": gen_val, "expected": exp_val}
    return beyond, {"match_type": "tier", "generated": gen_val, "expected": exp_val}


def weighted_average(weights: List[float], scores: List[float]) -> float:
    w = [max(0.0, x) for x in weights]
    s = [max(0.0, min(1.0, x)) for x in scores]
    tw = sum(w)
    if tw <= 0:
        return sum(s) / max(len(s), 1) if s else 0.0
    return sum(wi * si for wi, si in zip(w, s)) / tw
