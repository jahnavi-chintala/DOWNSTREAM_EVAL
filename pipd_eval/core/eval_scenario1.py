"""
eval_scenario1.py
-----------------
PIPD Eval Framework – Scenario 1: Ground truth is AVAILABLE (verify studies).

Computes 4 metrics per the D2 PIPD Eval Framework Design spec:
  M1 - Subcategory Recall        (CRITICAL  – target >= 85% on B7981027, >= 75% aggregate)
  M2 - YES/NO Flag Accuracy      (CRITICAL  – target >= 90% on auto_confirmed)
  M3 - Empty Category Accuracy   (HIGH      – target >= 80%)
  M4 - Traceability Flags        (CRITICAL  – target = 0 unresolved provenance flags)

Ground truth source: pipd_ground_truth_clean.csv  (split=verify rows ONLY)

Usage (standalone):
    python3 eval_scenario1.py \\
        --generator_json B7981027_PIPD.json \\
        --ground_truth pipd_ground_truth_clean.csv \\
        --study_id B7981027 \\
        --output_json B7981027_s1_results.json
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import os
import re
import argparse
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd                        # CSV reading, row filtering, groupby
from Levenshtein import distance as lev    # Fast Levenshtein edit-distance

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VERIFY_STUDIES: List[str] = [
    "B7981027", "C4891023", "C1071003", "C1071005",
    "C3651021", "C4591081", "C3671059", "C5091017",
]

NUM_CATEGORIES: int = 11          # PIPD always has exactly 11 deviation categories
NEAR_MISS_THRESHOLD: int = 25     # Max edit distance for truncation-tier near misses
NUMBERING_EDIT_MAX: int = 5       # Numbering-error band: edit dist 1–5, same body after digits stripped
NUMBERING_CREDIT: float = 0.99    # Wrong criterion index, or same index / different surface (Tier A)
TRUNCATION_EDIT_MIN: int = 6      # Truncation band lower bound (inclusive)
TRUNCATION_EDIT_MAX: int = 25     # Truncation band upper bound (inclusive)
TRUNCATION_CREDIT: float = 0.60   # kept for candidate identification only; final credit is dynamic
PARAPHRASE_CREDIT: float = 0.75   # kept for candidate identification only; final credit is dynamic
TRUNCATION_PARAPHRASE_THRESHOLD: float = 0.70  # candidate-selection heuristic only

ELIGIBILITY_CORE_JACC_MIN: float = 0.35  # min token Jaccard on bracket / stripped incl–excl core

# Per-metric pass/fail targets from the design spec
TARGETS: Dict[str, float] = {
    "m1_recall_b7981027":            0.85,
    "m1_recall_aggregate":           0.75,
    "m1_f1":                         0.70,
    "m2_auto_confirmed_accuracy":    0.90,
    "m3_empty_category_accuracy":    0.80,
    "m4_hallucinations":             0.00,   # zero tolerance
}

# usdm_entity_id values treated as null/missing traceability indicators
NULL_PLACEHOLDERS = {"", "null", "none", "n/a", "placeholder", "tbd"}

# Confidence levels that do NOT require provenance (exempt from M4 check)
PROVENANCE_EXEMPT_CONFIDENCE = {"review", "low_confidence", "category_10"}


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def filter_verify_split_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    When the CSV includes a ``split`` column, keep only ``verify`` rows.
    When ``split`` is absent (legacy / simplified ground-truth files), keep all rows.
    """
    if "split" not in df.columns:
        return df
    mask = df["split"].str.strip().str.lower() == "verify"
    return df[mask]


def load_ground_truth(csv_path: str, study_id: str) -> pd.DataFrame:
    """
    Load ground-truth rows for one study from pipd_ground_truth_clean.csv.

    Filters to split='verify' when a ``split`` column exists.  If the column is
    absent, all rows are kept (legacy CSVs).  Train rows must never appear in
    verify-filtered data when ``split`` is present.

    Args:
        csv_path  : Filesystem path to pipd_ground_truth_clean.csv
        study_id  : Study identifier string, e.g. 'B7981027'

    Returns:
        pandas DataFrame with columns: study_folder, therapeutic_area, phase,
        category_num, category_name, subcategory_text, include_in_csr,
        rationale_if_no, none_identified, split
    """
    df = pd.read_csv(csv_path, dtype=str)             # read everything as str to avoid coercion
    df = filter_verify_split_rows(df)
    study_df = df[df["study_folder"].str.strip() == study_id].copy()

    # Cast typed columns back to their correct types
    if "category_num" in study_df.columns:
        study_df["category_num"] = pd.to_numeric(study_df["category_num"], errors="coerce")
    if "none_identified" in study_df.columns:
        study_df["none_identified"] = study_df["none_identified"].str.strip().str.upper().map(
            {"TRUE": True, "FALSE": False, "YES": True, "NO": False}
        ).fillna(False)
    if "include_in_csr" in study_df.columns:
        study_df["include_in_csr"] = study_df["include_in_csr"].str.strip().str.upper()

    return study_df


def load_generator_json(json_path: str) -> Dict[str, Any]:
    """
    Load and parse the generator's output JSON file for a study.

    The JSON is expected to follow the canonical PIPD schema:
      {
        "categories": [
          { "category_num": 1,
            "none_identified": false,
            "subcategories": [
              { "subcategory_text": "...",
                "include_in_csr": true,
                "confidence": "auto_confirmed",
                "usdm_entity": "...",
                "usdm_entity_id": "...",
                "benchmark": { "segment": { "rate": 0.87 } }
              }, ...
            ]
          }, ...
        ]
      }

    Args:
        json_path : Filesystem path to {study_id}_PIPD.json

    Returns:
        Parsed dictionary
    """
    with open(json_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# DATA EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_subcategories_by_category(generator_json: Dict) -> Dict[int, List[Dict]]:
    """
    Restructure the generator JSON into a dict keyed by category_num.

    Args:
        generator_json : Full parsed generator JSON

    Returns:
        { 1: [subcat_dict, ...], 2: [...], ... }
    """
    result: Dict[int, List[Dict]] = {}
    for cat in generator_json.get("categories", []):
        num = cat.get("category_num")
        if num is not None:
            result[int(num)] = cat.get("subcategories", [])
    return result


def get_none_identified_by_category(generator_json: Dict) -> Dict[int, bool]:
    """
    Extract the none_identified flag per category from the generator JSON.

    Args:
        generator_json : Full parsed generator JSON

    Returns:
        { 1: False, 2: False, 5: True, ... }
    """
    result: Dict[int, bool] = {}
    for cat in generator_json.get("categories", []):
        num = cat.get("category_num")
        if num is not None:
            result[int(num)] = bool(cat.get("none_identified", False))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 1 – SUBCATEGORY RECALL
# ─────────────────────────────────────────────────────────────────────────────

def compute_subcategory_recall(
    generated_texts: List[str],
    gt_texts: List[str],
) -> Tuple[float, List[str], List[str], List[str]]:
    """
    Compute M1 per category: exact-string subcategory recall.

    Matching rule: exact string equality.  Near-miss credit (including cases where
    content is identical but phrasing differs) is handled downstream by the LLM
    reclassifier (_llm_reclassify_near_misses).

    Formula:
      recall = |generated ∩ ground_truth| / |ground_truth|

    Returns:
        (recall_score,
         matched      – in both GT and generated,
         missed       – in GT but NOT in generated,
         hallucinated – in generated but NOT in GT)
    """
    gen_set = set(generated_texts)
    gt_set  = set(gt_texts)

    matched      = sorted(gt_set & gen_set)
    missed       = sorted(gt_set - gen_set)
    hallucinated = sorted(gen_set - gt_set)

    recall = len(matched) / len(gt_set) if gt_set else 1.0
    return recall, matched, missed, hallucinated


def _bracket_contents(s: str) -> List[str]:
    """Return text inside [...] segments (common for inclusion-criterion lines)."""
    return re.findall(r"\[([^\]]+)\]", str(s or ""))


def _extract_inclusion_criterion_number(s: str) -> Optional[int]:
    """Parse criterion index from inclusion/exclusion, Incl N, or Excl N style text."""
    t = str(s or "")
    if not t.strip():
        return None
    m = re.search(r"\b(?:inclusion|exclusion)\s+criteria?\s*(\d+)\b", t, flags=re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\bincl\.?\s*(\d+)\b", t, flags=re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\bexcl\.?\s*(\d+)\b", t, flags=re.I)
    if m:
        return int(m.group(1))
    return None


def _strip_eligibility_prefix(s: str) -> str:
    """Strip Incl/criteria preamble when there is no [...] bracket."""
    t = str(s or "").strip()
    if not t:
        return ""
    t = re.sub(r"^phase\s+\d+\s+only\s*-\s*", "", t, flags=re.I)
    t = re.sub(r"^(subject|participant|patient|pts?)\s+", "", t, flags=re.I)
    t = re.sub(
        r"^(?:incl\.?\s*\d+|inclusion\s+criteria?\s*\d+|exclusion\s+criteria?\s*\d+)\s*(?:not\s+met|met)?\s*[:\s.-]*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(
        r"^(?:did\s+not\s+)?meet\s+(?:the\s+)?(?:inclusion|exclusion)\s+criteria?\s*\d*\s*[:\s.-]*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(
        r"^did\s+not\s+meet\s+(?:the\s+)?(?:inclusion|exclusion)\s+criteria?\s*\d*\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^not\s+meet\s+", "", t, flags=re.I)
    # GT shorthand not matched above: "Excl 5 met:" / "Incl 2 not met:"
    t = re.sub(
        r"^(?:excl|incl)\.?\s*\d+\s*(?:not\s+met|met)\s*[:\s.-]+",
        "",
        t,
        flags=re.I,
    )
    return t.strip()


def _looks_like_eligibility_line(s: str) -> bool:
    sl = (s or "").lower()
    return any(
        k in sl
        for k in ("inclusion", "incl", "exclusion", "excl", "criteria")
    )


def _eligibility_core_text(s: str) -> str:
    """Prefer longest bracket interior; otherwise text after stripping criterion preamble."""
    br = _bracket_contents(s)
    if br:
        return max(br, key=len).strip()
    return _strip_eligibility_prefix(s)


def _nm_norm(s: str) -> str:
    """Lowercase, strip subject prefix, collapse punctuation to spaces."""
    t = str(s or "").strip().lower()
    t = re.sub(r"^(participant|subject|patient|pts?)\s+", "", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _strip_digits_nm(s: str) -> str:
    return re.sub(r"\d+", "", s)


def _token_set_nm(s: str) -> Set[str]:
    return {tok for tok in _nm_norm(s).split() if tok}


def _negation_mismatch_nm(a: str, b: str) -> bool:
    a_n = _nm_norm(a)
    b_n = _nm_norm(b)
    a_neg = bool(re.search(r"\b(no|not|without|none|never)\b", a_n))
    b_neg = bool(re.search(r"\b(no|not|without|none|never)\b", b_n))
    if a_neg == b_neg:
        return False
    if ("copy available" in a_n and "copy available" in b_n) or ("copy" in a_n and "copy" in b_n):
        return True
    return False


def _prefix_truncation_normalized(na: str, nb: str) -> bool:
    """One string is the other plus a space-separated continuation."""
    if not na or not nb or na == nb:
        return False
    if len(na) <= len(nb) and nb.startswith(na) and len(nb) > len(na):
        return nb[len(na)] == " "
    if len(nb) <= len(na) and na.startswith(nb) and len(na) > len(nb):
        return na[len(nb)] == " "
    return False


def _try_eligibility_classify(cand: str, gt: str) -> Optional[Tuple[str, float, int]]:
    if not _looks_like_eligibility_line(cand) or not _looks_like_eligibility_line(gt):
        return None
    raw_c = _eligibility_core_text(cand)
    raw_g = _eligibility_core_text(gt)
    if not raw_c or not raw_g or len(raw_c) < 3 or len(raw_g) < 3:
        return None
    if _negation_mismatch_nm(raw_c, raw_g):
        return None
    cc_tok = _token_set_nm(raw_c)
    cg_tok = _token_set_nm(raw_g)
    if not cc_tok or not cg_tok:
        return None
    inter = len(cc_tok & cg_tok)
    union = len(cc_tok | cg_tok) or 1
    j_core = inter / union
    overlap_coef = inter / min(len(cc_tok), len(cg_tok)) if cc_tok and cg_tok else 0
    if not (
        j_core >= ELIGIBILITY_CORE_JACC_MIN
        or (overlap_coef >= 0.55 and inter >= 4)
    ):
        return None
    nc = _extract_inclusion_criterion_number(cand)
    ng = _extract_inclusion_criterion_number(gt)
    core_c = _nm_norm(raw_c)
    core_g = _nm_norm(raw_g)
    if not core_c or not core_g:
        return None
    dist_core = lev(core_c, core_g)
    if nc is not None and ng is not None and nc != ng:
        return ("NUMBERING_ERROR", NUMBERING_CREDIT, dist_core)
    # Same criterion index, same topical core — apply dynamic scoring based on
    # how much protocol detail is preserved in the generated text
    if nc is not None and ng is not None and nc == ng:
        # If generated core is a clean prefix of GT core → definite trailing truncation
        # (e.g. whole clause dropped from end).  Must check BEFORE len_ratio so that
        # eligibility lines are not mis-labelled CRITERION_FORMAT.
        if _prefix_truncation_normalized(core_c, core_g):
            return ("TRUNCATION", TRUNCATION_CREDIT, dist_core)
        len_ratio = len(core_c) / max(len(core_g), 1)
        if len_ratio >= 0.85:
            return ("CRITERION_FORMAT", NUMBERING_CREDIT, dist_core)
        elif len_ratio >= 0.60:
            # Moderate truncation — check if key protocol terms are lost
            if _protocol_keywords_missing(gt, cand):
                return ("TRUNCATION", TRUNCATION_CREDIT, dist_core)
            # Mild rewording without keyword loss
            dyn_credit = 0.60 + (len_ratio - 0.60) * (0.39 / 0.25)
            return ("CRITERION_FORMAT", round(min(0.99, dyn_credit), 2), dist_core)
        else:
            # Heavy truncation — significant detail dropped
            return ("TRUNCATION", TRUNCATION_CREDIT, dist_core)
    return ("TRUNCATION", TRUNCATION_CREDIT, dist_core)


def _classify_near_miss_detailed(
    generated_text: str,
    ground_truth_text: str,
    threshold: int = NEAR_MISS_THRESHOLD,
) -> Optional[Tuple[str, float, int]]:
    c_norm = _nm_norm(generated_text)
    g_norm = _nm_norm(ground_truth_text)
    if not c_norm or not g_norm:
        return None
    if _negation_mismatch_nm(generated_text, ground_truth_text):
        return None
    if c_norm == g_norm:
        return None

    elig = _try_eligibility_classify(generated_text, ground_truth_text)
    if elig is not None:
        return elig

    dist = lev(c_norm, g_norm)

    if _prefix_truncation_normalized(c_norm, g_norm):
        return ("TRUNCATION", TRUNCATION_CREDIT, dist)

    max_len = max(len(c_norm), len(g_norm), 1)
    rel_dist = dist / max_len
    c_tok = _token_set_nm(generated_text)
    g_tok = _token_set_nm(ground_truth_text)
    inter = len(c_tok & g_tok)
    union = len(c_tok | g_tok) or 1
    jacc = inter / union

    body_c = _strip_digits_nm(c_norm)
    body_g = _strip_digits_nm(g_norm)
    if (
        1 <= dist <= NUMBERING_EDIT_MAX
        and body_c == body_g
        and body_c.strip()
    ):
        return ("NUMBERING_ERROR", NUMBERING_CREDIT, dist)  # digit mismatch only, Tier A

    if TRUNCATION_EDIT_MIN <= dist <= min(TRUNCATION_EDIT_MAX, threshold):
        if jacc >= 0.35 or rel_dist <= 0.55:
            return ("TRUNCATION", TRUNCATION_CREDIT, dist)

    return None


def classify_near_miss(
    generated_text: str,
    ground_truth_text: str,
) -> Optional[Tuple[str, float]]:
    """Return (NUMBERING_ERROR|CRITERION_FORMAT|TRUNCATION, credit) or None."""
    d = _classify_near_miss_detailed(generated_text, ground_truth_text)
    return None if d is None else (d[0], d[1])


def precision_recall_f1_score(
    net: float,
    gt_count: int,
    generated_count: int,
) -> Tuple[float, float, float]:
    """
    precision = net / generated_count (extras increase generated_count, not net).
    recall    = net / gt_count (misses do not add to net).
    F1        = 2·P·R / (P+R) when P+R > 0, else 0.
    """
    if gt_count <= 0:
        return (0.0, 0.0, 0.0)
    r = net / float(gt_count)
    if generated_count <= 0:
        return (0.0, r, 0.0)
    p = net / float(generated_count)
    if p + r <= 0:
        return (p, r, 0.0)
    f1 = 2.0 * p * r / (p + r)
    return (p, r, f1)


def compute_category_score(
    net: float,
    gt_count: int,
    generated_count: int,
) -> float:
    """
    Category score = F1(precision, recall) × 100.

    **net** (per category) = verbatim matches × 1.0
      + numbering_error / criterion_format near-misses × 0.99
      + truncation near-misses × 0.60
      (misses and extras contribute 0 to net; extras only increase generated_count).
    """
    _, _, f1 = precision_recall_f1_score(net, gt_count, generated_count)
    return 100.0 * f1


def find_near_misses(
    candidates: List[str],
    gt_texts: List[str],
    threshold: int = NEAR_MISS_THRESHOLD,
) -> List[Dict]:
    """
    Greedy one-to-one pairing of hallucinated vs GT lines for partial M1 credit.
    Each item: generated_text, gt_text, distance, tier, credit, root_cause.
    """
    pairs: List[Tuple[int, str, str, str, float, str]] = []
    for cand in candidates:
        if not cand:
            continue
        if not _nm_norm(cand):
            continue
        for gt in gt_texts:
            if not _nm_norm(gt):
                continue
            d = _classify_near_miss_detailed(cand, gt, threshold)
            if d is None:
                continue
            tag, credit, dist = d
            tier = "A" if tag in ("NUMBERING_ERROR", "CRITERION_FORMAT") else "B"
            pairs.append((dist, cand, gt, tier, credit, tag))

    near_misses: List[Dict] = []
    used_cand: Set[str] = set()
    used_gt: Set[str] = set()
    for dist, cand, gt, tier, credit, tag in sorted(
        pairs, key=lambda x: (x[0], len(x[1]), len(x[2]))
    ):
        if cand in used_cand or gt in used_gt:
            continue
        near_misses.append(
            {
                "generated_text": cand,
                "gt_text": gt,
                "distance": dist,
                "tier": tier,
                "credit": credit,
                "root_cause": tag,
            }
        )
        used_cand.add(cand)
        used_gt.add(gt)
    return near_misses


def _paraphrase_pair_score(m: str, e: str) -> Tuple[float, float]:
    """Return (sequence ratio, token Jaccard) for miss vs extra."""
    ml = (m or "").lower().strip()
    el = (e or "").lower().strip()
    if not ml or not el:
        return 0.0, 0.0
    seq = SequenceMatcher(None, ml, el).ratio()
    c_tok = _token_set_nm(m)
    e_tok = _token_set_nm(e)
    inter = len(c_tok & e_tok)
    union = len(c_tok | e_tok) or 1
    j = inter / union
    return seq, j


def _classify_paraphrase_or_truncation(gt_text: str, gen_text: str) -> Tuple[str, float]:
    """
    Deterministic rule-based classification of a matched (missed GT, extra generated) pair.

    Rules (applied in order):
      1. Length ratio < TRUNCATION_PARAPHRASE_THRESHOLD (0.70)  → TRUNCATION (0.60)
      2. Protocol keywords missing from generated text            → TRUNCATION (0.60)
      3. Otherwise                                               → PARAPHRASE (0.75)

    No LLM is involved — result is fully reproducible.
    """
    len_ratio = len(gen_text.strip()) / max(len(gt_text.strip()), 1)
    if len_ratio < TRUNCATION_PARAPHRASE_THRESHOLD:
        return "TRUNCATION", TRUNCATION_CREDIT
    if _protocol_keywords_missing(gt_text, gen_text):
        return "TRUNCATION", TRUNCATION_CREDIT
    return "PARAPHRASE", PARAPHRASE_CREDIT


def find_paraphrase_pairs(
    missed: List[str],
    extras: List[str],
) -> Tuple[List[Dict], List[str], List[str]]:
    """
    After strict ``find_near_misses`` (edit-distance / eligibility rules), pair
    remaining **missed GT lines** with remaining **extra generated lines** when
    both clearly describe the same deviation.

    Classification is fully deterministic (no LLM):
    - PARAPHRASE (0.75): same deviation, generated length ≥ 70% of GT, no keyword loss
    - TRUNCATION (0.60): generated is significantly shorter OR drops key protocol terms
    """
    if not missed or not extras:
        return [], list(missed), list(extras)

    # Score all candidate pairs by similarity
    scored: List[Tuple[float, str, str]] = []
    for m in missed:
        for e in extras:
            if _negation_mismatch_nm(m, e):
                continue
            seq, j = _paraphrase_pair_score(m, e)
            ok = (
                (seq >= 0.50 and j >= 0.12)
                or (seq >= 0.42 and j >= 0.16)
                or (seq >= 0.58)
                or (seq >= 0.34 and j >= 0.22)
            )
            if not ok:
                continue
            combined = 0.55 * seq + 0.45 * j
            scored.append((combined, m, e))

    scored.sort(reverse=True, key=lambda x: x[0])

    out: List[Dict] = []
    used_m: Set[str] = set()
    used_e: Set[str] = set()

    for _comb, m, e in scored:
        if m in used_m or e in used_e:
            continue
        used_m.add(m)
        used_e.add(e)

        tag, credit = _classify_paraphrase_or_truncation(m, e)
        cn = _nm_norm(m)
        ce = _nm_norm(e)
        dist = lev(cn, ce) if cn and ce else 0
        out.append({
            "generated_text": e,
            "gt_text": m,
            "distance": dist,
            "tier": "P",
            "credit": credit,
            "root_cause": tag,
        })

    missed_left = [t for t in missed if t not in used_m]
    extras_left = [t for t in extras if t not in used_e]
    return out, missed_left, extras_left


def _compute_dynamic_near_miss_credit(
    gt_text: str,
    gen_text: str,
) -> Tuple[float, str, str]:
    """
    Compute a continuous credit [0.10, 1.0] for a near-miss pair based on
    four dimensions of clinical information preservation:

      (35%) Clinical-term preservation  — specific drug names, diseases, lab tests
      (30%) Numeric-threshold preservation — values with clinical units (30 days, 12 months…)
      (25%) Content-token recall         — % of non-trivial GT words present in generated
      (10%) Length ratio                 — rough completeness proxy

    Returns (credit, tag, explanation) where:
      NEAR_VERBATIM  ≥ 0.92  content identical, only form/grammar differs
      MINOR_LOSS     ≥ 0.75  trivial non-critical detail dropped
      MODERATE_LOSS  ≥ 0.50  clinically relevant detail missing/simplified
      MAJOR_LOSS     < 0.50  critical information lost or replaced with vague substitute
    """
    gt_norm  = _nm_norm(gt_text)
    gen_norm = _nm_norm(gen_text)

    if not gt_norm or not gen_norm:
        return 0.10, "MAJOR_LOSS", "empty text"

    # ── 1. Content-token recall ──────────────────────────────────────────────
    STOPWORDS = {
        "a", "an", "the", "of", "in", "or", "and", "to", "is", "for", "with",
        "by", "at", "from", "be", "that", "this", "any", "as", "on", "are",
        "has", "have", "not", "no", "did", "does", "do", "its", "it", "which",
    }
    gt_tok  = {t for t in gt_norm.split()  if t not in STOPWORDS and len(t) > 2}
    gen_tok = {t for t in gen_norm.split() if t not in STOPWORDS and len(t) > 2}
    token_recall = len(gt_tok & gen_tok) / max(len(gt_tok), 1)

    # ── 2. Length ratio ──────────────────────────────────────────────────────
    len_ratio = min(1.0, len(gen_norm) / max(len(gt_norm), 1))

    # ── 3. Clinical-term preservation ────────────────────────────────────────
    # Named entities that carry specific clinical meaning
    crit_pattern = re.compile(
        r"\b(?:COVID-19|SARS-CoV-2|ibuzatrelvir|nirmatrelvir|ritonavir|paxlovid|"
        r"WOCBP|eGFR|GFR|AST|ALT|ULN|bilirubin|creatinine|CrCl|dialysis|"
        r"hospitalization|suicidal|contraception|convalescent|covalescent|mAbs|"
        r"ibuzat|randomization|screening|hepatic|renal|ibuz)\b",
        re.IGNORECASE,
    )
    gt_crit  = {m.lower() for m in crit_pattern.findall(gt_text)}
    gen_crit = {m.lower() for m in crit_pattern.findall(gen_text)}
    missing_crit = gt_crit - gen_crit
    crit_preservation = 1.0 - (len(missing_crit) / max(len(gt_crit), 1)) if gt_crit else 1.0

    # ── 4. Numeric-threshold preservation ────────────────────────────────────
    # Values that appear with clinical units — dropping one changes clinical meaning
    num_pattern = re.compile(
        r"\d+\.?\d*\s*(?:days?|months?|years?|hours?|half.lives?|%|mg|kg|uln|g\/dl|×|x\s*uln)",
        re.IGNORECASE,
    )
    gt_nums  = num_pattern.findall(gt_text.lower())
    gen_lower = gen_text.lower()
    missing_nums = 0
    for nv in gt_nums:
        num_only = re.search(r"\d+\.?\d*", nv)
        if num_only and num_only.group() not in gen_lower:
            missing_nums += 1
    num_preservation = 1.0 - (missing_nums / max(len(gt_nums), 1)) if gt_nums else 1.0

    # ── 5. Weighted combination ──────────────────────────────────────────────
    score = (
        0.35 * crit_preservation
        + 0.30 * num_preservation
        + 0.25 * token_recall
        + 0.10 * len_ratio
    )
    score = round(max(0.10, min(1.0, score)), 2)

    # ── 6. Label and human-readable explanation ──────────────────────────────
    loss_parts: List[str] = []
    if missing_crit:
        loss_parts.append(f"clinical term(s) lost: {', '.join(sorted(missing_crit))}")
    if missing_nums:
        total_nums = len(gt_nums)
        loss_parts.append(f"{missing_nums}/{total_nums} numeric threshold(s) dropped")
    if token_recall < 0.85:
        loss_parts.append(f"{round((1 - token_recall) * 100)}% content words absent")
    if len_ratio < 0.75:
        loss_parts.append(f"text {round((1 - len_ratio) * 100)}% shorter than GT")

    if not loss_parts:
        tag         = "NEAR_VERBATIM"
        explanation = "only grammar / articles / punctuation differ"
    elif score >= 0.92:
        tag         = "NEAR_VERBATIM"
        explanation = "; ".join(loss_parts)
    elif score >= 0.75:
        tag         = "MINOR_LOSS"
        explanation = "; ".join(loss_parts)
    elif score >= 0.50:
        tag         = "MODERATE_LOSS"
        explanation = "; ".join(loss_parts)
    else:
        tag         = "MAJOR_LOSS"
        explanation = "; ".join(loss_parts)

    return score, tag, explanation


def _llm_reclassify_near_misses(pairs: List[Dict]) -> List[Dict]:
    """
    Send all near-miss pairs to the LLM for content-based reclassification.

    The LLM decides for each pair whether the difference is purely grammatical /
    formatting (→ verbatim-equivalent, 1.0 credit) or whether real clinical content
    was lost, and how critical that loss is:

      verbatim_equivalent  1.00  same content, only form/grammar/pluralisation differs
      minor_loss           0.85  trivial non-critical detail removed (parenthetical, minor qualifier)
      moderate_loss        0.60  clinically relevant detail missing but criterion recognisable
      major_loss           0.30  critical info lost (specific drug name → "medication",
                                  "COVID-19" → "disease", numeric threshold dropped, etc.)

    Falls back to the existing deterministic credits if OPENAI_API_KEY is not set or
    if the API call fails.
    """
    if not pairs:
        return pairs

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return pairs  # LLM not configured — keep deterministic credits

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    _CREDIT = {
        "verbatim_equivalent": 1.00,
        "minor_loss":          0.85,
        "moderate_loss":       0.60,
        "major_loss":          0.30,
    }
    _TIER = {
        "verbatim_equivalent": "A",
        "minor_loss":          "A",
        "moderate_loss":       "B",
        "major_loss":          "B",
    }
    _TAG = {
        "verbatim_equivalent": "VERBATIM_EQUIVALENT",
        "minor_loss":          "MINOR_LOSS",
        "moderate_loss":       "MODERATE_LOSS",
        "major_loss":          "MAJOR_LOSS",
    }

    items = [
        {
            "id":  i,
            "gt":  (p.get("gt_text") or "")[:600],
            "gen": (p.get("generated_text") or "")[:600],
        }
        for i, p in enumerate(pairs)
    ]

    system_msg = (
        "You are a clinical trial protocol QA evaluator reviewing near-miss pairs in a "
        "PIPD (Protocol Inclusion/Exclusion and Protocol Deviations) document.\n\n"
        "For each pair, determine whether the core CLINICAL CONTENT is preserved or lost.\n\n"
        "VERDICT OPTIONS:\n"
        '  "verbatim_equivalent" (1.0) — The content is identical; only the grammatical form '
        "differs: articles removed, pluralisation, minor punctuation, minor rephrasing of the "
        "same facts.  No clinical information is gained or lost.\n"
        '  "minor_loss" (0.85) — A non-critical detail is missing (e.g., a parenthetical '
        "clarification, a minor qualifier) but the core clinical criterion is completely "
        "understood.\n"
        '  "moderate_loss" (0.60) — A clinically relevant detail is missing or simplified, '
        "reducing specificity but leaving the general criterion recognisable. Examples: a "
        "specific timeframe generalised, a specific lab test category dropped.\n"
        '  "major_loss" (0.30) — Critical specific information is lost or replaced with a '
        "vague substitute. Examples: specific drug name → \"medication\", \"COVID-19\" → "
        "\"disease\", a required numeric threshold dropped, a distinct medical condition "
        "replaced with a broader category, an entire clinical clause removed.\n\n"
        "Return ONLY a JSON array — no markdown fences, no extra text:\n"
        '[{"id": 0, "verdict": "...", "reason": "one sentence"}, ...]'
    )
    user_msg = "Classify these near-miss pairs:\n\n" + json.dumps(items, indent=2)

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg},
        ],
        "max_tokens": 50 * len(pairs) + 200,
        "temperature": 0,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )

    import time as _time

    def _do_call() -> List[Dict]:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        raw = out["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```\s*$", "", raw.rstrip())
        results = json.loads(raw)
        lookup = {r["id"]: r for r in results if isinstance(r, dict)}
        updated = []
        for i, p in enumerate(pairs):
            entry = lookup.get(i)
            if not entry:
                updated.append(p)
                continue
            verdict = entry.get("verdict", "")
            if verdict not in _CREDIT:
                updated.append(p)
                continue
            p2 = dict(p)
            p2["credit"]      = _CREDIT[verdict]
            p2["tier"]        = _TIER[verdict]
            p2["root_cause"]  = _TAG[verdict]
            p2["llm_verdict"] = verdict
            p2["llm_reason"]  = entry.get("reason", "")
            updated.append(p2)
        return updated

    last_exc: Optional[Exception] = None
    for attempt, wait in enumerate([0, 8, 20, 40]):
        if wait:
            _time.sleep(wait)
        try:
            return _do_call()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                continue   # retry after backoff
            break          # non-rate-limit HTTP error — don't retry
        except Exception as exc:
            last_exc = exc
            break

    # LLM unavailable — fall back to dynamic content-preservation scoring
    updated = []
    for p in pairs:
        credit, tag, explanation = _compute_dynamic_near_miss_credit(
            p.get("gt_text", ""), p.get("generated_text", "")
        )
        p2 = dict(p)
        p2["credit"]      = credit
        p2["root_cause"]  = tag
        p2["tier"]        = "A" if tag in ("NEAR_VERBATIM", "MINOR_LOSS") else "B"
        p2["dynamic_explanation"] = explanation
        p2["llm_error"]   = str(last_exc)
        updated.append(p2)
    return updated


def _protocol_keywords_missing(gt_text: str, gen_text: str) -> bool:
    """
    Check if key protocol-specific terms from GT are missing in generated text.
    Returns True if significant keywords are lost (should be classified as truncation).
    """
    gt_lower = gt_text.lower()
    gen_lower = gen_text.lower()

    # Extract protocol-significant tokens from GT
    # These are numeric values, drug names, specific clinical terms
    import re as _re

    # Numeric values with units (e.g., "30 days", "5 half-lives", "3 ULN", "75%")
    numeric_patterns = _re.findall(r'\d+\.?\d*\s*(?:%|days?|hours?|months?|years?|mg|kg|ml|uln|m[lL])', gt_lower)

    # Drug/compound names (capitalized multi-char words not common English)
    gt_words = set(_re.findall(r'\b[A-Za-z][a-z]{3,}\b', gt_text))
    gen_words = set(_re.findall(r'\b[A-Za-z][a-z]{3,}\b', gen_text))

    # Key clinical terms that should not be dropped
    clinical_terms = _re.findall(
        r'\b(?:eGFR|AST|ALT|ULN|bilirubin|creatinine|CrCl|NP|PCR|'
        r'COVID-19|SARS-CoV-2|ibuzatrelvir|WOCBP|RAT|mAbs|'
        r'Child.Pugh|Gilbert|hospitalization|dialysis|suicidal|'
        r'contraception|randomization|screening)\b',
        gt_text, _re.IGNORECASE
    )

    # Check if numeric values from GT are preserved
    missing_numerics = 0
    for nv in numeric_patterns:
        # Extract just the number
        num = _re.search(r'\d+\.?\d*', nv)
        if num and num.group() not in gen_lower:
            missing_numerics += 1

    # Check clinical terms
    missing_clinical = 0
    for term in clinical_terms:
        if term.lower() not in gen_lower:
            missing_clinical += 1

    # Any single missing numeric-with-unit (e.g. "12 months", "30 days") signals
    # that a clinically important threshold was dropped → truncation.
    # Similarly for any missing clinical term (WOCBP, SARS-CoV-2, etc.).
    len_ratio = len(gen_text) / max(len(gt_text), 1)
    if missing_numerics >= 1 or missing_clinical >= 1:
        return True
    if len_ratio < 0.60:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 2 – YES/NO FLAG ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def compute_flag_accuracy(
    matched_texts: List[str],
    gen_subcats: List[Dict],
    gt_cat_df: pd.DataFrame,
) -> Dict:
    """
    Compute M2 (YES/NO flag accuracy) for subcategories that matched on M1.

    Scoring scope: MATCHED subcategories only.  Unmatched subcategories are
    already penalised in M1 and excluded from M2 to avoid double-counting.

    The primary target applies to confidence='auto_confirmed' subcategories;
    review/low_confidence are scored separately with a lower acceptable target.

    Args:
        matched_texts  : Exact-match subcategory texts from M1
        gen_subcats    : List of subcategory dicts from generator JSON
        gt_cat_df      : Ground truth DataFrame filtered to this category

    Returns:
        {
          overall_accuracy, auto_confirmed_accuracy,
          total_matched, correct_flags,
          auto_confirmed_total, auto_confirmed_correct,
          discrepancies: [ { subcategory_text, confidence,
                              generated_flag, gt_flag } ]
        }
    """
    # Build lookups
    gt_flag_lookup: Dict[str, str] = {
        str(row["subcategory_text"]): str(row["include_in_csr"]).strip().upper()
        for _, row in gt_cat_df.iterrows()
        if pd.notna(row.get("subcategory_text"))
    }
    gen_lookup: Dict[str, Dict] = {
        s["subcategory_text"]: s
        for s in gen_subcats
        if "subcategory_text" in s
    }

    total = correct = auto_total = auto_correct = 0
    discrepancies: List[Dict] = []

    for text in matched_texts:
        gt_flag = gt_flag_lookup.get(text)
        gen_sub = gen_lookup.get(text, {})
        gen_flag = "YES" if gen_sub.get("include_in_csr") is True else "NO"
        confidence = gen_sub.get("confidence", "")

        if gt_flag is None:
            continue   # no GT flag available – skip

        is_correct = (gen_flag == gt_flag)
        total += 1
        correct += int(is_correct)

        if not is_correct:
            discrepancies.append({
                "subcategory_text": text,
                "confidence": confidence,
                "generated_flag": gen_flag,
                "gt_flag": gt_flag,
            })

        if confidence == "auto_confirmed":
            auto_total += 1
            auto_correct += int(is_correct)

    return {
        "overall_accuracy":           correct / total if total else 1.0,
        "auto_confirmed_accuracy":    auto_correct / auto_total if auto_total else 1.0,
        "total_matched":              total,
        "correct_flags":              correct,
        "auto_confirmed_total":       auto_total,
        "auto_confirmed_correct":     auto_correct,
        "discrepancies":              discrepancies,
    }


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 3 – EMPTY CATEGORY ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def compute_empty_category_accuracy(
    gen_none_identified: Dict[int, bool],
    gt_df: pd.DataFrame,
) -> Dict:
    """
    Compute M3: did the generator correctly identify which categories are empty?

    Formula:
      score = categories where generated none_identified == GT none_identified
              ─────────────────────────────────────────────────────────────────
                                    11

    A category is 'empty' in the GT if every row for that category has
    none_identified=True (or there are no rows at all for that category).

    Args:
        gen_none_identified : Dict from generator JSON { cat_num: bool }
        gt_df               : Ground truth DataFrame for this study (all cats)

    Returns:
        {
          score, correct_flags, total_categories,
          per_category: { cat_num: { gt_none_identified, generated_none_identified, correct } },
          mismatches: [ { category_num, gt_none_identified, generated_none_identified } ]
        }
    """
    # Build GT none_identified per category
    gt_none: Dict[int, bool] = {}
    for cat_num in range(1, NUM_CATEGORIES + 1):
        cat_rows = gt_df[gt_df["category_num"] == cat_num]
        if cat_rows.empty:
            gt_none[cat_num] = True           # absent from GT → treat as empty
        else:
            # none_identified=True means no subcats; if ALL rows are True → empty
            gt_none[cat_num] = bool(cat_rows["none_identified"].all())

    correct = 0
    per_category: Dict[int, Dict] = {}
    mismatches: List[Dict] = []

    for cat_num in range(1, NUM_CATEGORIES + 1):
        gt_val  = gt_none.get(cat_num, True)
        gen_val = gen_none_identified.get(cat_num, True)
        is_ok   = (gen_val == gt_val)

        correct += int(is_ok)
        per_category[cat_num] = {
            "gt_none_identified":        gt_val,
            "generated_none_identified": gen_val,
            "correct":                   is_ok,
        }
        if not is_ok:
            mismatches.append({
                "category_num":               cat_num,
                "gt_none_identified":         gt_val,
                "generated_none_identified":  gen_val,
            })

    return {
        "score":            correct / NUM_CATEGORIES,
        "correct_flags":    correct,
        "total_categories": NUM_CATEGORIES,
        "per_category":     per_category,
        "mismatches":       mismatches,
    }


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 4 – HALLUCINATION DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def compute_hallucination_detection(
    all_subcats: List[Dict],
    usdm_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    usdm_ids: Optional[Set[str]] = None,
    instance_types: Optional[Set[str]] = None,
) -> Dict:
    """
    Compute M4: detect untraceable subcategories (aligned with intelligence-truth /
    handover protocol traceability).

    Subcategories with confidence in {review, low_confidence, category_10} are exempt.

    When protocol USDM is loaded (``usdm_ids`` + ``instance_types`` from the same
    file as ``usdm_by_id``), non-exempt rows pass if either:
      • ``usdm_entity_id`` is non-null and appears in the protocol USDM id set, or
      • ``usdm_entity_id`` is null but ``usdm_entity`` matches an ``instanceType``
        present in the protocol (type-level traceability; see ``pipd_usdm_support``).

    Non-exempt rows fail when they declare an id absent from protocol, or when
    neither a resolvable id nor a known instance type is available.

    When USDM is not loaded, behaviour is unchanged: non-exempt rows need non-null
    ``usdm_entity`` and ``usdm_entity_id`` (strict), and fabricated ids cannot be
    detected without a loaded graph.

    Target: traceability_flag_count == 0.

    Args:
        all_subcats      : Flattened subcategory dicts with ``_category_num``
        usdm_by_id       : Optional ``index_usdm_nodes_by_id(protocol_root)``
        usdm_ids         : Optional set of all string ``id`` values in protocol USDM
        instance_types   : Optional set of ``instanceType`` strings in protocol USDM

    Returns:
        Metric dict including ``traceability_flag_count``, ``flagged_subcategories``, ``pass``.
    """
    flagged: List[Dict] = []

    def is_null(val: Any) -> bool:
        return val is None or str(val).strip().lower() in NULL_PLACEHOLDERS

    protocol_sets_loaded = usdm_ids is not None and instance_types is not None
    usdm_active = usdm_by_id is not None or protocol_sets_loaded

    if protocol_sets_loaded:
        from core.pipd_usdm_support import usdm_trace_for_subcategory

        for sub in all_subcats:
            confidence = sub.get("confidence", "")
            if confidence in PROVENANCE_EXEMPT_CONFIDENCE:
                continue

            tr = usdm_trace_for_subcategory(sub, usdm_ids, instance_types)
            if tr["symbol"] != "✗":
                continue

            entity_id = sub.get("usdm_entity_id")
            entity = sub.get("usdm_entity")
            issues: List[str] = []
            if tr.get("id_in_protocol") is False:
                issues.append(
                    "usdm_entity_id not found in protocol USDM JSON "
                    "(may be fabricated or wrong file)"
                )
            else:
                et = str(entity).strip() if entity is not None else ""
                if is_null(entity):
                    issues.append("usdm_entity is null/missing – no protocol traceability")
                elif et not in instance_types:
                    issues.append(
                        "usdm_entity type not found among protocol instanceTypes "
                        "and usdm_entity_id is null/missing — no protocol traceability"
                    )
                else:
                    issues.append(
                        tr.get("detail")
                        or "no resolvable USDM reference against loaded protocol JSON"
                    )

            flagged.append({
                "category_num":     sub.get("_category_num"),
                "subcategory_text": sub.get("subcategory_text"),
                "usdm_entity_id":   entity_id,
                "usdm_entity":      entity,
                "confidence":       confidence,
                "issues":           issues,
            })
    else:
        for sub in all_subcats:
            confidence = sub.get("confidence", "")
            if confidence in PROVENANCE_EXEMPT_CONFIDENCE:
                continue

            entity_id = sub.get("usdm_entity_id")
            entity = sub.get("usdm_entity")

            issues: List[str] = []
            if is_null(entity_id):
                issues.append("usdm_entity_id is null/missing – no protocol traceability")
            if is_null(entity):
                issues.append("usdm_entity is null/missing")

            if usdm_by_id is not None and not is_null(entity_id):
                key = str(entity_id).strip()
                if key not in usdm_by_id:
                    issues.append(
                        "usdm_entity_id not found in protocol USDM JSON "
                        "(may be fabricated or wrong file)"
                    )

            if issues:
                flagged.append({
                    "category_num":     sub.get("_category_num"),
                    "subcategory_text": sub.get("subcategory_text"),
                    "usdm_entity_id":   entity_id,
                    "usdm_entity":      entity,
                    "confidence":       confidence,
                    "issues":           issues,
                })

    return {
        "traceability_flag_count": len(flagged),
        "hallucinations_found":   len(flagged),
        "flagged_subcategories":  flagged,
        "traceability_flags":     flagged,
        "pass":                   len(flagged) == 0,
        "usdm_protocol_validation_active": usdm_active,
        "note": (
            "M4 counts traceability gaps for non-exempt rows. With protocol USDM loaded, "
            "a row passes if usdm_entity_id exists in the protocol or (when id is null) "
            "usdm_entity matches an instanceType in the protocol — same rule as "
            "intelligence_truth protocol_traceability_percent. "
            "hallucinations_found is a legacy alias for traceability_flag_count."
        ),
    }


def _load_usdm_by_id_for_scenario1(
    usdm_json_path: Optional[str],
    study_id: str,
) -> Tuple[
    Optional[Dict[str, Dict[str, Any]]],
    Optional[str],
    Optional[Set[str]],
    Optional[Set[str]],
]:
    """
    Resolve and load protocol USDM JSON; return
    (id_index, resolved_path, all_usdm_ids, instance_types).

    Resolution: explicit ``usdm_json_path`` if that file exists, else
    ``pipd_usdm_support.resolve_usdm_protocol_path`` (env ``PIPD_USDM_JSON``,
    then ``data/usdm_protocol_{study_id}.json``, etc.).

    ``all_usdm_ids`` and ``instance_types`` feed M4 handover-aligned traceability
    (``usdm_trace_for_subcategory``); import of collectors is deferred to avoid
    cycles at module load.
    """
    pkg_data = Path(__file__).resolve().parent / "data"
    path: Optional[Path] = None
    if usdm_json_path and str(usdm_json_path).strip():
        p = Path(str(usdm_json_path).strip())
        if p.is_file():
            path = p.resolve()

    if path is None:
        from core.pipd_usdm_support import resolve_usdm_protocol_path

        r = resolve_usdm_protocol_path(study_id, pkg_data)
        if r is not None and r.is_file():
            path = r.resolve()

    if path is None:
        return None, None, None, None

    with open(path, encoding="utf-8") as fh:
        root = json.load(fh)
    from core.pipd_usdm_provenance import index_usdm_nodes_by_id

    # Lazy import: pipd_usdm_support imports eval_scenario1 at top level.
    from core.pipd_usdm_support import collect_instance_types, collect_usdm_ids

    return (
        index_usdm_nodes_by_id(root),
        str(path),
        collect_usdm_ids(root),
        collect_instance_types(root),
    )


# ─────────────────────────────────────────────────────────────────────────────
# METRIC 5 – CONFIDENCE TIER VS GT SEVERITY PROXY
# METRIC 6 – GSOP CODE JACCARD (MATCHED SUBCATEGORIES)
# ─────────────────────────────────────────────────────────────────────────────

def confidence_tier_index(confidence: Any) -> int:
    """Map generator confidence to 0=auto, 1=review, 2=low (aligns with pipd_eval_config tier order)."""
    c = str(confidence or "").strip().lower()
    if c == "category_10":
        return 1
    if c == "auto_confirmed":
        return 0
    if c == "review":
        return 1
    if c == "low_confidence":
        return 2
    return 1


def _rationale_nonempty(rationale_if_no: Any) -> bool:
    if rationale_if_no is None or (isinstance(rationale_if_no, float) and pd.isna(rationale_if_no)):
        return False
    return bool(str(rationale_if_no).strip())


def expected_tier_for_severity_row() -> int:
    """When GT documents CSR/rationale text for a line, expect at least review-tier confidence."""
    return 1


def tier_match_score(expected_idx: int, actual_idx: int) -> float:
    """Same spirit as YAML tier_scores: exact 1.0, one tier away 0.5, else 0.0."""
    diff = abs(int(expected_idx) - int(actual_idx))
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.5
    return 0.0


def compute_severity_match(
    matched_texts: List[str],
    gen_subcats: List[Dict],
    gt_cat_df: pd.DataFrame,
) -> Dict[str, Any]:
    gt_rat: Dict[str, Any] = {}
    for _, row in gt_cat_df.iterrows():
        t = str(row.get("subcategory_text") or "").strip()
        if t:
            gt_rat[t] = row.get("rationale_if_no")
    gen_lookup: Dict[str, Dict] = {
        s["subcategory_text"]: s
        for s in gen_subcats
        if s.get("subcategory_text")
    }
    scores: List[float] = []
    for text in matched_texts:
        if not _rationale_nonempty(gt_rat.get(text)):
            continue
        exp = expected_tier_for_severity_row()
        sub = gen_lookup.get(text, {})
        act = confidence_tier_index(sub.get("confidence"))
        scores.append(tier_match_score(exp, act))
    n = len(scores)
    return {
        "score": sum(scores) / n if n else 1.0,
        "total_matched": n,
        "row_scores": scores,
    }


def gsop_set_from_value(val: Any) -> Set[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return set()
    if isinstance(val, list):
        return {str(x).strip().upper() for x in val if str(x).strip()}
    s = str(val).strip()
    if not s:
        return set()
    return {p.strip().upper() for p in re.split(r"[;,]", s) if p.strip()}


def compute_gsop_jaccard_match(
    matched_texts: List[str],
    gen_subcats: List[Dict],
    gt_cat_df: pd.DataFrame,
) -> Dict[str, Any]:
    """
    Per matched subcategory, Jaccard(generated gsop_codes, GT gsop_codes).

    GSOP codes are meant to come from a reference SOP table (GT / USDM /
    registry), not to be freely generated by the model. When the GT CSV has
    no ``gsop_codes`` column we cannot score coverage, so M6 is marked
    ``status='no_gt_gsop_column'`` and ``score=None`` (UI treats as N/A and
    excludes it from the overall aggregate).

    A separate informational signal ``generator_side_fill_rate`` reports the
    share of matched rows where the generator emitted at least one GSOP code;
    this is purely observational and not used for pass/fail.
    """
    has_gsop_col = "gsop_codes" in gt_cat_df.columns
    gt_lookup: Dict[str, Any] = {}
    for _, row in gt_cat_df.iterrows():
        t = str(row.get("subcategory_text") or "").strip()
        if t:
            gt_lookup[t] = row.get("gsop_codes") if has_gsop_col else None
    gen_lookup: Dict[str, Dict] = {
        s["subcategory_text"]: s
        for s in gen_subcats
        if s.get("subcategory_text")
    }
    scores: List[float] = []
    gen_filled = 0
    for text in matched_texts:
        gen_s = gsop_set_from_value(gen_lookup.get(text, {}).get("gsop_codes"))
        if gen_s:
            gen_filled += 1
        if not has_gsop_col:
            continue
        gt_s = gsop_set_from_value(gt_lookup.get(text))
        if not gen_s and not gt_s:
            scores.append(1.0)
        elif not gen_s or not gt_s:
            scores.append(0.0)
        else:
            inter = len(gen_s & gt_s)
            union = len(gen_s | gt_s)
            scores.append(inter / union if union else 1.0)

    n = len(scores)
    total_matched = len(matched_texts)
    fill_rate = (gen_filled / total_matched) if total_matched else None

    if not has_gsop_col:
        return {
            "score": None,
            "status": "no_gt_gsop_column",
            "total_matched": total_matched,
            "generator_side_fill_rate": fill_rate,
            "row_scores": [],
            "gt_gsop_column_present": False,
            "note": (
                "Ground-truth CSV has no `gsop_codes` column; GSOP codes "
                "should be fetched from a SOP reference table, not generated. "
                "M6 marked N/A and excluded from the overall composite."
            ),
        }

    return {
        "score": sum(scores) / n if n else 1.0,
        "status": "ok",
        "total_matched": n,
        "generator_side_fill_rate": fill_rate,
        "row_scores": scores,
        "gt_gsop_column_present": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EVAL RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario1_eval(
    generator_json_path: str,
    ground_truth_csv_path: str,
    study_id: str,
    usdm_json_path: Optional[str] = None,
) -> Dict:
    """
    Orchestrate the full Scenario 1 evaluation for one study.

    Loads generator JSON + ground truth CSV, runs all 6 metrics at
    per-category and per-study level, and returns a comprehensive results
    dict ready for report generation or API serialisation.

    Args:
        generator_json_path  : Path to {study_id}_PIPD.json
        ground_truth_csv_path: Path to pipd_ground_truth_clean.csv
        study_id             : Study identifier e.g. 'B7981027'
        usdm_json_path       : Optional protocol USDM JSON; when set (or resolved
                               via PIPD_USDM_JSON / data/), M4 checks ids and
                               (when id is null) that usdm_entity matches a
                               protocol instanceType (handover / intelligence_truth).

    Returns:
        Full results dictionary (see below for schema)

    Raises:
        ValueError : If no ground-truth rows found for this study/split
        FileNotFoundError : If either input path does not exist
    """
    # ── Load data ─────────────────────────────────────────────────────────────
    generator_json = load_generator_json(generator_json_path)
    gt_df          = load_ground_truth(ground_truth_csv_path, study_id)

    if gt_df.empty:
        raise ValueError(
            f"No ground-truth rows found for study_id='{study_id}' with split='verify'. "
            "Check that the study is in the verify set and the CSV path is correct."
        )

    ta    = gt_df["therapeutic_area"].iloc[0] if "therapeutic_area" in gt_df.columns else "Unknown"
    phase = gt_df["phase"].iloc[0]             if "phase"             in gt_df.columns else "Unknown"

    # ── Extract generator structures ─────────────────────────────────────────
    gen_by_cat     = get_subcategories_by_category(generator_json)
    gen_none_ident = get_none_identified_by_category(generator_json)

    # Flatten all subcats for M4 (add _category_num so M4 can report location)
    all_gen_subcats: List[Dict] = []
    for cat_num, subcats in gen_by_cat.items():
        for sub in subcats:
            annotated = dict(sub)
            annotated["_category_num"] = cat_num
            all_gen_subcats.append(annotated)

    # ── M3 (whole-study level, doesn't depend on per-cat M1) ─────────────────
    m3 = compute_empty_category_accuracy(gen_none_ident, gt_df)

    # ── M4 (whole-study level) ────────────────────────────────────────────────
    usdm_by_id, usdm_resolved_path, usdm_id_set, usdm_instance_types = (
        _load_usdm_by_id_for_scenario1(usdm_json_path, study_id)
    )
    m4 = compute_hallucination_detection(
        all_gen_subcats,
        usdm_by_id=usdm_by_id,
        usdm_ids=usdm_id_set,
        instance_types=usdm_instance_types,
    )

    # ── Per-category M1 + M2 loop ─────────────────────────────────────────────
    per_cat: Dict[int, Dict] = {}
    all_near_misses: List[Dict] = []

    study_credit = 0.0
    study_matched = study_gt_total = study_gen_total = 0
    m2_auto_total = m2_auto_correct = 0

    for cat_num in range(1, NUM_CATEGORIES + 1):
        gt_cat_df    = gt_df[gt_df["category_num"] == cat_num]
        gt_texts     = (gt_cat_df["subcategory_text"].dropna().astype(str).tolist()
                        if not gt_cat_df.empty else [])
        gen_subcats  = gen_by_cat.get(cat_num, [])
        gen_texts    = [s.get("subcategory_text", "") for s in gen_subcats]

        # M1
        recall, matched, missed, hallucinated = compute_subcategory_recall(gen_texts, gt_texts)

        # Near/semantic misses: unmatched generated text vs MISSED GT only.
        # Only GT items that weren't already matched verbatim can receive
        # near-miss credit — this prevents the score from exceeding 100%.
        nm = find_near_misses(hallucinated, missed)
        for n in nm:
            n["category_num"] = cat_num
            n["study_id"]     = study_id
        all_near_misses.extend(nm)
        near_gen = {n.get("generated_text") for n in nm}
        near_gt = {n.get("gt_text") for n in nm}
        missed_adj = [t for t in missed if t not in near_gt]
        hallucinated_adj = [t for t in hallucinated if t not in near_gen]

        pm, missed_adj, hallucinated_adj = find_paraphrase_pairs(missed_adj, hallucinated_adj)
        for n in pm:
            n["category_num"] = cat_num
            n["study_id"]     = study_id
        all_near_misses.extend(pm)

        near_credit = sum(float(n.get("credit") or 0.0) for n in nm) + sum(
            float(n.get("credit") or 0.0) for n in pm
        )
        credit = len(matched) + near_credit
        # Cap credit at GT count — near-misses cannot exceed the missed items
        credit = min(credit, float(len(gt_texts)))
        recall = (credit / len(gt_texts)) if gt_texts else 1.0
        nm_total = len(nm) + len(pm)

        # M2
        m2 = compute_flag_accuracy(matched, gen_subcats, gt_cat_df)
        m2_auto_total   += m2["auto_confirmed_total"]
        m2_auto_correct += m2["auto_confirmed_correct"]

        study_credit    += credit
        study_matched   += len(matched)
        study_gt_total  += len(gt_texts)
        study_gen_total += len(gen_texts)

        per_cat[cat_num] = {
            "category_num":             cat_num,
            "m1_recall":                recall,
            "m1_matched":               len(matched),
            "m1_gt_total":              len(gt_texts),
            "m1_generated_total":       len(gen_texts),
            "m1_near_misses":           nm_total,
            "matched_subcats":          matched,
            "missed_subcats":           missed_adj,
            "hallucinated_subcats":     hallucinated_adj,
            "m2_flag_accuracy":         m2["overall_accuracy"],
            "m2_auto_confirmed_accuracy": m2["auto_confirmed_accuracy"],
            "m2_discrepancies":         m2["discrepancies"],
            "m3_none_identified_correct": m3["per_category"].get(cat_num, {}).get("correct", True),
        }

    # ── LLM reclassification of all near-miss pairs ───────────────────────────
    # Sends every candidate pair (from find_near_misses + find_paraphrase_pairs)
    # to the LLM in one batch.  The LLM decides whether the difference is purely
    # grammatical/formatting (verbatim_equivalent → 1.0) or a genuine content loss
    # graded by clinical criticality (minor 0.85 / moderate 0.60 / major 0.30).
    # Gracefully falls back to deterministic credits when OPENAI_API_KEY is absent.
    if all_near_misses:
        all_near_misses = _llm_reclassify_near_misses(all_near_misses)
        # Recalculate per-category recall and study-level credit with updated credits
        study_credit = 0.0
        for cn, cat_data in per_cat.items():
            cat_nm = [n for n in all_near_misses if n.get("category_num") == cn]
            near_credit = sum(float(n.get("credit") or 0.0) for n in cat_nm)
            gt_total    = cat_data["m1_gt_total"]
            new_credit  = min(cat_data["m1_matched"] + near_credit, float(gt_total))
            cat_data["m1_recall"] = (new_credit / gt_total) if gt_total else 1.0
            study_credit += new_credit

    # ── Study-level aggregates ────────────────────────────────────────────────
    study_recall    = study_credit / study_gt_total  if study_gt_total  else 1.0
    study_precision = study_credit / study_gen_total if study_gen_total else 1.0
    study_f1 = (2 * study_precision * study_recall / (study_precision + study_recall)
                if (study_precision + study_recall) else 0.0)
    m2_auto_acc = m2_auto_correct / m2_auto_total if m2_auto_total else 1.0

    # ── Pass / fail ───────────────────────────────────────────────────────────
    m1_target  = TARGETS["m1_recall_aggregate"]
    m1_f1_target = TARGETS["m1_f1"]

    m1_pass  = (study_recall >= m1_target) and (study_f1 >= m1_f1_target)
    m2_pass  = m2_auto_acc     >= TARGETS["m2_auto_confirmed_accuracy"]
    m3_pass  = m3["score"]     >= TARGETS["m3_empty_category_accuracy"]
    m4_pass  = m4["pass"]
    # Per-category gate: if ANY category with GT items has recall < 50%, fail overall
    _cat_fail_threshold = 0.50
    _any_cat_fail = any(
        per_cat[cn]["m1_recall"] < _cat_fail_threshold
        for cn in per_cat
        if per_cat[cn]["m1_gt_total"] > 0
    )
    all_pass = m1_pass and m2_pass and m3_pass and m4_pass and (not _any_cat_fail)

    # ── Weighted overall score (0–100) ────────────────────────────────────────
    # Weights: M1=55%, M2=15%, M3=10%, M4=20%.
    _m4_flags = int(m4.get("traceability_flag_count", m4["hallucinations_found"]))
    _m4_score = 1.0 if _m4_flags == 0 else max(
        0.0, 1.0 - 0.2 * float(_m4_flags)
    )
    _weight_pairs = [
        ("m1", 0.55, float(study_f1)),
        ("m2", 0.15, float(m2_auto_acc)),
        ("m3", 0.10, float(m3["score"]) if m3["score"] is not None else None),
        ("m4", 0.20, float(_m4_score)),
    ]
    _active = [(k, w, v) for (k, w, v) in _weight_pairs if v is not None]
    _wsum = sum(w for _k, w, _v in _active) or 1.0
    overall_score_0_1 = sum(w * v for _k, w, v in _active) / _wsum
    overall_score_percent = round(100.0 * overall_score_0_1, 1)

    return {
        "study_id":   study_id,
        "ta":         ta,
        "phase":      phase,
        "eval_date":  datetime.now().isoformat(),
        "scenario":   1,
        "metrics": {
            "m1_subcategory_recall": {
                "score":          study_recall,
                "precision":      study_precision,
                "f1":             study_f1,
                "total_matched":  round(study_credit, 4),
                "total_exact_matched": study_matched,
                "total_credit":   study_credit,
                "total_near_miss_credit": round(study_credit - study_matched, 4),
                "total_gt":       study_gt_total,
                "total_generated": study_gen_total,
                "target":         m1_target,
                "f1_target":      m1_f1_target,
                "pass":           m1_pass,
            },
            "m2_flag_accuracy": {
                "auto_confirmed_accuracy": m2_auto_acc,
                "auto_confirmed_total":    m2_auto_total,
                "auto_confirmed_correct":  m2_auto_correct,
                "target":                  TARGETS["m2_auto_confirmed_accuracy"],
                "pass":                    m2_pass,
            },
            "m3_empty_category_accuracy": {
                "score":            m3["score"],
                "correct_flags":    m3["correct_flags"],
                "total_categories": NUM_CATEGORIES,
                "mismatches":       m3["mismatches"],
                "target":           TARGETS["m3_empty_category_accuracy"],
                "pass":             m3_pass,
            },
            "m4_hallucination_detection": {
                "traceability_flag_count": m4.get("traceability_flag_count", m4["hallucinations_found"]),
                "hallucinations_found":  m4["hallucinations_found"],
                "flagged_subcategories": m4["flagged_subcategories"],
                "traceability_flags":    m4.get("traceability_flags", m4["flagged_subcategories"]),
                "target":               int(TARGETS["m4_hallucinations"]),
                "pass":                 m4_pass,
                "usdm_protocol_validation_active": m4.get(
                    "usdm_protocol_validation_active", False
                ),
                "usdm_protocol_path": usdm_resolved_path,
                "note": m4.get("note"),
            },
        },
        "per_category":  per_cat,
        "near_misses":   all_near_misses,
        "overall_pass":  all_pass,
        "go_no_go":      "GO" if all_pass else "NO-GO",
        "overall_score_percent": overall_score_percent,
        "overall_score_0_1": round(overall_score_0_1, 6),
        "overall_score_weights": {
            "m1_f1": 0.55,
            "m2_auto_confirmed_accuracy": 0.15,
            "m3_empty_category_accuracy": 0.10,
            "m4_traceability_penalty": 0.20,
            "note": (
                "m4_traceability_penalty = 1.0 when traceability_flag_count == 0, "
                "otherwise max(0, 1 - 0.2 * traceability_flag_count)."
            ),
        },
    }


def _m1_missing_failure_hints(example: str) -> Tuple[str, str]:
    """
    Pattern-based root_cause / generator_fix for M1 misses (not generic few-shot only).
    """
    ex = (example or "").strip()
    low = ex.lower()
    if re.search(r"\b(excl|incl)\s*\d+", low):
        return (
            "Inc/Excl criterion index or wording may not match the GT label for that line",
            "Align the criterion number and **exact** label text with the ground-truth CSV; "
            "wrong index targets a different GT row than the protocol deviation.",
        )
    if len(ex) > 220:
        return (
            "Long GT label not reproduced; model may be paraphrasing or omitting",
            "Map deviations to the **verbatim** GT string for that subcategory; avoid shortening or splitting.",
        )
    return (
        "GT checklist line not present in generated subcategories",
        "Map protocol/USDM signals to the canonical GT label string for this category.",
    )


def _m1_extra_failure_hints(example: str) -> Tuple[str, str]:
    ex = (example or "").strip()
    low = ex.lower()
    if len(ex) > 140:
        return (
            "Verbose or detailed line not in GT (often paraphrase of a shorter canonical label)",
            "Constrain outputs to **canonical GT labels**; strip extra protocol wording or map one deviation to one GT line.",
        )
    if "participant" in low and len(ex) > 80:
        return (
            "Possible template / narrative phrasing not in GT list",
            "Restrict generation to enumerated GT strings; avoid free-form protocol sentences.",
        )
    return (
        "Extra subcategory not in ground truth",
        "Validate generated lines against the GT label set; drop or remap non-list text.",
    )


def classify_failures(results: Dict) -> List[Dict]:
    """
    Classify every identified failure into a typed record the generator
    developer can act on directly.

    Failure types:
      M1_MISSING_SUBCAT   – GT subcat not produced by generator
      M1_HALLUCINATED     – Generator produced subcat not in GT
      M2_WRONG_FLAG       – Matched subcat has wrong YES/NO flag
      M3_EMPTY_MISMATCH   – none_identified flag wrong for a category
      M4_TRACEABILITY     – no resolvable USDM ref (loaded: bad/missing id; missing type when id null)

    Args:
        results : Output dict from run_scenario1_eval()

    Returns:
        List of { failure_type, category_num, example, root_cause, generator_fix }
    """
    failures: List[Dict] = []

    for cat_num, cat in results["per_category"].items():
        for text in cat.get("missed_subcats", []):
            rc, gf = _m1_missing_failure_hints(str(text))
            failures.append({
                "failure_type":  "M1_MISSING_SUBCAT",
                "category_num":  cat_num,
                "example":       text,
                "root_cause":    rc,
                "generator_fix": gf,
            })
        for text in cat.get("hallucinated_subcats", []):
            rc, gf = _m1_extra_failure_hints(str(text))
            failures.append({
                "failure_type":  "M1_HALLUCINATED",
                "category_num":  cat_num,
                "example":       text,
                "root_cause":    rc,
                "generator_fix": gf,
            })
        for disc in cat.get("m2_discrepancies", []):
            failures.append({
                "failure_type":  "M2_WRONG_FLAG",
                "category_num":  cat_num,
                "example":       f"GT={disc['gt_flag']} Gen={disc['generated_flag']} '{disc['subcategory_text']}'",
                "root_cause":    "Benchmark yes_rate threshold miscalibrated",
                "generator_fix": "Adjust yes_rate threshold in deviation_benchmarks.yaml",
            })
        if not cat.get("m3_none_identified_correct"):
            failures.append({
                "failure_type":  "M3_EMPTY_MISMATCH",
                "category_num":  cat_num,
                "example":       f"Category {cat_num} none_identified mismatch",
                "root_cause":    "USDM extractor missed entities for this category",
                "generator_fix": f"Fix extract_cat{cat_num}(). Log what entities were found.",
            })

    for flagged in results["metrics"]["m4_hallucination_detection"]["flagged_subcategories"]:
        issues = flagged.get("issues") or []
        joined = " ".join(str(x) for x in issues)
        not_in_usdm = "not found in protocol USDM JSON" in joined
        type_missing = "instanceTypes" in joined or "type not found" in joined.lower()
        if not_in_usdm:
            failures.append({
                "failure_type":  "M4_TRACEABILITY",
                "category_num":  flagged.get("category_num"),
                "example":       f"'{flagged['subcategory_text']}' usdm_entity_id not in protocol USDM",
                "root_cause":    "PIPD cites a USDM id that is absent from the loaded protocol JSON",
                "generator_fix": "Align ids with protocol USDM or pass the correct usdm_json / PIPD_USDM_JSON.",
            })
        elif type_missing:
            failures.append({
                "failure_type":  "M4_TRACEABILITY",
                "category_num":  flagged.get("category_num"),
                "example":       f"'{flagged['subcategory_text']}' has no protocol instanceType/id trace",
                "root_cause":    "Declared usdm_entity is absent from protocol USDM and id is unset",
                "generator_fix": "Populate usdm_entity_id from protocol USDM or set usdm_entity to a protocol instanceType.",
            })
        else:
            failures.append({
                "failure_type":  "M4_TRACEABILITY",
                "category_num":  flagged.get("category_num"),
                "example":       f"'{flagged['subcategory_text']}' has null/missing USDM provenance",
                "root_cause":    "Non-exempt row without resolvable protocol USDM (no USDM file or empty fields)",
                "generator_fix": "Fix post-gen validation. Strengthen usdm_entity_id / usdm_entity prompt instruction.",
            })

    return failures


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PIPD Eval Framework – Scenario 1 (ground truth)")
    parser.add_argument("--generator_json", required=True, help="Path to {study_id}_PIPD.json")
    parser.add_argument("--ground_truth",   required=True, help="Path to pipd_ground_truth_clean.csv")
    parser.add_argument("--study_id",       required=True, help="Study identifier e.g. B7981027")
    parser.add_argument(
        "--usdm_json",
        default=None,
        help="Optional protocol USDM JSON (M4: id in graph, or instanceType match when id is null)",
    )
    parser.add_argument("--output_json",    default=None,  help="Optional path to save results JSON")
    args = parser.parse_args()

    results = run_scenario1_eval(
        args.generator_json,
        args.ground_truth,
        args.study_id,
        usdm_json_path=args.usdm_json,
    )

    m = results["metrics"]
    print(f"\n{'='*55}")
    print(f"  Scenario 1 Eval │ {args.study_id}")
    print(f"{'='*55}")
    print(f"  M1 Recall         : {m['m1_subcategory_recall']['score']:.2%}"
          f"  ({'✓ PASS' if m['m1_subcategory_recall']['pass'] else '✗ FAIL'})")
    print(f"  M2 Flag Accuracy  : {m['m2_flag_accuracy']['auto_confirmed_accuracy']:.2%}"
          f"  ({'✓ PASS' if m['m2_flag_accuracy']['pass'] else '✗ FAIL'})")
    print(f"  M3 Empty Cat      : {m['m3_empty_category_accuracy']['score']:.2%}"
          f"  ({'✓ PASS' if m['m3_empty_category_accuracy']['pass'] else '✗ FAIL'})")
    m4_flags = m['m4_hallucination_detection'].get(
        'traceability_flag_count',
        m['m4_hallucination_detection']['hallucinations_found'],
    )
    print(f"  M4 Traceability   : {m4_flags}"
          f"  ({'✓ PASS' if m['m4_hallucination_detection']['pass'] else '✗ FAIL'})")
    print(f"{'='*55}")
    print(f"  Verdict : {results['go_no_go']}")
    print(f"{'='*55}\n")

    if args.output_json:
        Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"Results saved → {args.output_json}")
