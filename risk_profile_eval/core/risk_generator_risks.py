"""
Helpers for reading risk objects from D1 Risk Profile generator JSON.

Risks may appear under ``risks``, ``risks_monitored``, ``vendor_risks``,
``study_site_risks``, and ``other_domain_risks``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Tuple

RISK_LIST_KEYS: Tuple[str, ...] = (
    "risks",
    "risks_monitored",
    "vendor_risks",
    "study_site_risks",
    "other_domain_risks",
)

# Canonical labels aligned with ``risk_profile_ground_truth.csv`` ``risk_domain`` column
_JSON_KEY_TO_DOMAIN: Dict[str, str] = {
    "risks": "Study Risks",
    "risks_monitored": "Study Risks",
    "vendor_risks": "Vendor Risks",
    "study_site_risks": "Study Site Risks",
    "other_domain_risks": "Other Domain Risks",
}


def normalize_domain_label(s: str) -> str:
    """Lowercase single-spaced domain for comparison."""
    return " ".join((s or "").strip().lower().split())


def domains_compatible(gt_domain_norm: str, gen_domain_norm: str) -> bool:
    """
    True if a ground-truth risk row may pair with a generated risk for M1.

    When **both** sides carry a non-empty normalised domain, they must match
    (Study vs Vendor vs Study Site vs Other). If either side omits domain,
    pairing falls back to name-only (backward compatible with sparse CSV).
    """
    g = (gt_domain_norm or "").strip()
    h = (gen_domain_norm or "").strip()
    if g and h:
        return g == h
    return True


def infer_generated_domain(json_key: str, risk: Dict[str, Any]) -> str:
    """Prefer ``risk.risk_domain``; else infer from JSON list location."""
    raw = risk.get("risk_domain")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return _JSON_KEY_TO_DOMAIN.get(json_key, "")


def normalize_risk_name_for_match(name: str) -> str:
    """
    Normalize labels for equality: whitespace, slashes, optional ePRO instrument prefix.

    - Strips ``ePRO <instrument> `` prefixes (e.g. EORTC, EQ-5D)
    - Collapses spaces; normalises spaces around ``/`` (Data/Data vs Data / Data)
    """
    if name is None:
        return ""
    s = str(name).strip()
    if not s:
        return ""
    s = re.sub(r"(?i)^epro\s+[A-Za-z0-9\-]+\s+", "", s)
    s = " ".join(s.split())
    s = re.sub(r"\s*/\s*", "/", s)
    return s.strip()


def fingerprint_risk_name_for_m1(name: str) -> str:
    """
    M1 "verbatim" key after removing cosmetic-only differences.

    Applies ``normalize_risk_name_for_match`` first, then lowercases and strips
    spaces, commas, full stops, hyphens, and underscores. Used only for
    eval equality / Levenshtein (not for display), so e.g. ``A-B`` vs
    ``A B`` vs ``A_B`` vs ``A.B`` are treated as the same name.
    """
    s = normalize_risk_name_for_match(name or "")
    s = s.lower()
    s = re.sub(r"[,.\s\-_]+", "", s)
    return s


def get_all_risk_dicts(generator_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in RISK_LIST_KEYS:
        block = generator_json.get(key)
        if not block or not isinstance(block, list):
            continue
        for item in block:
            if isinstance(item, dict):
                out.append(item)
    return out


def iter_risk_dicts_with_keys(
    generator_json: Dict[str, Any],
) -> Iterator[Tuple[str, int, Dict[str, Any]]]:
    for key in RISK_LIST_KEYS:
        block = generator_json.get(key)
        if not block or not isinstance(block, list):
            continue
        for j, item in enumerate(block):
            if isinstance(item, dict):
                yield key, j, item
