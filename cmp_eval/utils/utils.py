"""
utils.py — Shared utilities for CMP Eval Pipeline
Levenshtein distance, semantic similarity fallback, threshold parsing.
"""

import re
import math
from typing import Any, Optional


# ─── Levenshtein Distance ─────────────────────────────────────────────────────

def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    s1, s2 = s1.lower().strip(), s2.lower().strip()
    if s1 == s2:
        return 0
    m, n = len(s1), len(s2)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            if s1[i - 1] == s2[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


# ─── Token Overlap (Jaccard) Similarity ────────────────────────────────────────

def jaccard_similarity(s1: str, s2: str) -> float:
    """Token-level Jaccard similarity — used as semantic proxy."""
    tokens1 = set(re.findall(r'\w+', s1.lower()))
    tokens2 = set(re.findall(r'\w+', s2.lower()))
    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union)


# ─── KRI label normalisation (CMP) ───────────────────────────────────────────

def normalize_cmp_kri_label(label: str) -> str:
    """
    Strip optional ``ePRO <instrument> `` prefix and trim so generated labels align
    with plain GT text (e.g. ``ePRO EORTC Open to Open`` → ``Open to Open``).
    """
    s = (label or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?i)^epro\s+[A-Za-z0-9\-]+\s+", "", s)
    return s.strip()


# ─── KRI Label Match Scoring ──────────────────────────────────────────────────

def score_label_match(generated: str, ground_truth: str, config: dict) -> tuple[float, str]:
    """
    Score a KRI label match using verbatim → Levenshtein → Jaccard cascade.
    Returns (score 0.0-1.0, match_type string).
    """
    if not generated or not ground_truth:
        return 0.0, "null"

    gen = normalize_cmp_kri_label(generated).strip().lower()
    gt = normalize_cmp_kri_label(ground_truth).strip().lower()

    # Verbatim
    if gen == gt:
        return 1.0, "verbatim"

    # Levenshtein near-miss
    lev_cfg = config.get("verbatim_fallback", {})
    lev_max = lev_cfg.get("levenshtein_max", 5)
    lev_score = lev_cfg.get("levenshtein_score", 0.80)
    dist = levenshtein_distance(gen, gt)
    if dist <= lev_max:
        return lev_score, f"levenshtein_d{dist}"

    # Jaccard / semantic fallback
    sem_cfg = config.get("semantic_fallback", {})
    jac = jaccard_similarity(gen, gt)
    if jac >= sem_cfg.get("high_threshold", 0.85):
        return sem_cfg.get("high_score", 0.60), "semantic_high"
    if jac >= sem_cfg.get("medium_threshold", 0.70):
        return sem_cfg.get("medium_score", 0.30), "semantic_medium"

    return sem_cfg.get("low_score", 0.00), "no_match"


def best_label_match(generated_label: str, gt_labels: list[str], config: dict) -> tuple[float, str, str]:
    """
    Find the best matching ground truth label for a generated label.
    Returns (score, match_type, matched_gt_label).
    """
    best_score, best_type, best_gt = 0.0, "no_match", ""
    for gt in gt_labels:
        score, mtype = score_label_match(generated_label, gt, config)
        if score > best_score:
            best_score, best_type, best_gt = score, mtype, gt
    return best_score, best_type, best_gt


# ─── Threshold Parsing ────────────────────────────────────────────────────────

_THRESHOLD_PATTERNS = [
    # ">=10% and <25%"  → 10.0
    r'>=\s*([\d.]+)\s*%',
    r'≥\s*([\d.]+)\s*%',
    # ">10% and <25%" → 10.0
    r'>\s*([\d.]+)\s*%',
    # plain "10"
    r'^([\d.]+)$',
    # "10.0"
    r'([\d.]+)',
]


def parse_threshold_value(raw: any) -> Optional[float]:
    """
    Parse a threshold value from various string/numeric representations.
    Returns float (the lower bound of moderate range) or None.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and math.isnan(raw):
            return None
        return float(raw)
    text = str(raw).strip()
    if not text or text.lower() in ('n/a', 'na', 'nan', 'null', 'none', ''):
        return None
    for pat in _THRESHOLD_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def score_threshold(generated_raw: any, gt_raw: any, bands: dict) -> float:
    """
    Score a threshold value against ground truth using tolerance bands.
    Returns 0.0 - 1.0.
    """
    gen_val = parse_threshold_value(generated_raw)
    gt_val = parse_threshold_value(gt_raw)

    # Both null → both agree on no threshold (e.g. relative-score-only KRI)
    if gen_val is None and gt_val is None:
        return 1.0

    # One null, one not → mismatch
    if gen_val is None or gt_val is None:
        return 0.0

    if gt_val == 0:
        # Avoid divide-by-zero — use absolute difference
        diff_pct = abs(gen_val - gt_val)
    else:
        diff_pct = abs(gen_val - gt_val) / abs(gt_val) * 100

    exact_pct = bands.get("exact_pct", 5)
    near_pct = bands.get("near_pct", 15)

    if diff_pct <= exact_pct:
        return bands.get("exact_score", 1.0)
    if diff_pct <= near_pct:
        return bands.get("near_score", 0.5)
    return bands.get("beyond_score", 0.0)


# ─── Tier Scoring ─────────────────────────────────────────────────────────────

def score_tier(generated: any, ground_truth: any, tier_scores: dict) -> float:
    """Score confidence tier match."""
    if generated is None or ground_truth is None:
        return 0.0
    try:
        gen_tier = int(generated)
        gt_tier = int(ground_truth)
    except (TypeError, ValueError):
        return 0.0
    if gen_tier == gt_tier:
        return tier_scores.get("exact_match", 1.0)
    if abs(gen_tier - gt_tier) == 1:
        return tier_scores.get("adjacent_tier", 0.5)
    return tier_scores.get("beyond_adjacent", 0.0)


# ─── IQMP Risk ID Validation ──────────────────────────────────────────────────

IQMP_ABSENT_LOWER = frozenset(
    {"null", "none", "nan", "n/a", "-", "—", "placeholder", "tbd"}
)


def iqmp_value_absent(value: Any) -> bool:
    """True when the generator emitted no substantive IQMP/ASRP id.

    Mirrors PIPD/Risk Profile placeholder handling: treat ``N/A``, ``tbd``,
    em-dash blanks, etc. as **absent**, not as a conflicting asserted id for M4.
    """
    if value is None:
        return True
    try:
        v = float(value)
        if v != v:
            return True
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return True
    return s.lower() in IQMP_ABSENT_LOWER


def gt_iqmp_is_blank(gt_id: any) -> bool:
    """True when GT has no IQMP code to compare (blank / NaN / placeholder)."""
    if gt_id is None:
        return True
    try:
        v = float(gt_id)
        if v != v:  # NaN
            return True
    except (TypeError, ValueError):
        pass
    s = str(gt_id).strip()
    if not s or s.lower() in ("nan", "none", "null", "-", "n/a", "—", ""):
        return True
    return False


def validate_iqmp_risk_id(generated_id: any, gt_id: any) -> float:
    """
    Score IQMP / ASRP risk ID match.
    Handles SR-XXXXX codes, VR-XXXXX codes, and free-text IDs.
    """
    blank = gt_iqmp_is_blank(gt_id)
    _ID_RE = re.compile(r"[A-Z]{2}-\d{4,}", re.IGNORECASE)

    if iqmp_value_absent(generated_id):
        return 1.0 if blank else 0.0

    gen_str = str(generated_id).strip()
    if blank:
        return 1.0 if _ID_RE.search(gen_str) else 0.5

    gt_str = str(gt_id).strip()

    if gen_str.upper() == gt_str.upper():
        return 1.0

    gt_codes = set(c.upper() for c in _ID_RE.findall(gt_str))
    gen_codes = set(c.upper() for c in _ID_RE.findall(gen_str))

    if gen_codes and gt_codes:
        if gen_codes == gt_codes:
            return 1.0
        if gen_codes & gt_codes:
            return 0.5
        return 0.0

    if not gt_codes and not gen_codes:
        jac = jaccard_similarity(gen_str, gt_str)
        return 1.0 if jac >= 0.8 else (0.5 if jac >= 0.5 else 0.0)

    return 0.0

# ─── Jaccard for forms/variables ──────────────────────────────────────────────

def score_forms_variables(generated: list, ground_truth_text: str) -> float:
    """
    Score form/variable coverage using token Jaccard.
    generated: list of variable strings from JSON
    ground_truth_text: raw text from CSV
    """
    if not generated and not ground_truth_text:
        return 1.0
    gen_tokens = set()
    for item in (generated or []):
        gen_tokens.update(re.findall(r'[A-Z][A-Z0-9_]{2,}', str(item)))
    gt_tokens = set(re.findall(r'[A-Z][A-Z0-9_]{2,}', str(ground_truth_text or '')))
    if not gt_tokens:
        return 1.0 if not gen_tokens else 0.5
    if not gen_tokens:
        return 0.0
    return len(gen_tokens & gt_tokens) / len(gt_tokens)


# ─── Weight Exact Match ────────────────────────────────────────────────────────

def _normalise_weight_value(raw: str) -> str:
    """
    Normalize a single weight token (High / Moderate / Low).

    Strict mode: multi-token strings such as '2 (High), 1 (Moderate)' are
    returned as-is and will NOT match a single canonical GT value.
    The generator must supply exactly one weight tier.
    """
    s = str(raw).strip()
    for canon in ("high", "moderate", "low"):
        if s.lower() == canon:
            return canon.capitalize()
    return s  # non-canonical / multi-value → kept as-is → will not match GT


def score_weight(generated: any, ground_truth: any, vocab: list) -> float:
    """Exact match on controlled vocabulary, with multi-token normalization."""
    if not generated or not ground_truth:
        return 0.0
    gen = _normalise_weight_value(str(generated))
    gt = _normalise_weight_value(str(ground_truth))
    if gen == gt:
        return 1.0
    if gen.lower() == gt.lower():
        return 1.0
    return 0.0
