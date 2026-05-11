"""Extract a protocol study id (e.g. B7981027, C5091017) from USDM or generator JSON."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# Pfizer-style protocol codes: B or C + 7 digits (allow embedded in longer strings)
_PROTOCOL_TOKEN = re.compile(r"\b([BC][0-9]{7})\b", re.IGNORECASE)
_STRICT = re.compile(r"^[BC][0-9]{7}$", re.IGNORECASE)


def _norm_token(s: str) -> str:
    return str(s).strip().upper()


def _from_string(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (int, float)) and val == int(val):
        val = str(int(val))
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    if _STRICT.match(s):
        return _norm_token(s)
    m = _PROTOCOL_TOKEN.search(s)
    return _norm_token(m.group(1)) if m else None


def _from_mapping(obj: Mapping[str, Any]) -> Optional[str]:
    for key in (
        "study_id",
        "studyId",
        "studyIdentifier",
        "study_identifier",
        "study_folder",
        "studyFolder",
        "protocolId",
        "protocol_id",
        "protocolIdentifier",
        "protocolNumber",
        "sponsorProtocolIdentifier",
        "sponsorStudyIdentifier",
        "sponsor_protocol_identifier",
        "businessIdentifier",
        "business_identifier",
        "shortName",
        "label",
        "name",
        "id",
        "identifier",
        "title",
    ):
        if key not in obj:
            continue
        v = obj.get(key)
        hit = _from_string(v)
        if hit:
            return hit
        if isinstance(v, dict):
            hit = _from_mapping(v)
            if hit:
                return hit
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    hit = _from_mapping(item)
                    if hit:
                        return hit
                else:
                    hit = _from_string(item)
                    if hit:
                        return hit

    st = obj.get("Study")
    if isinstance(st, dict):
        hit = _from_mapping(st)
        if hit:
            return hit
    return None


def _deep_scan(obj: Any, depth: int = 0, max_depth: int = 24) -> Optional[str]:
    if depth > max_depth:
        return None
    if isinstance(obj, str):
        return _from_string(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            hit = _deep_scan(v, depth + 1, max_depth)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = _deep_scan(item, depth + 1, max_depth)
            if hit:
                return hit
    return None


def extract_protocol_study_id(data: Any) -> Optional[str]:
    """
    Best-effort study id from a loaded JSON dict (or nested structure).
    Returns uppercase id like B7981027, or None.
    """
    if not isinstance(data, dict):
        return None

    direct = _from_mapping(data)
    if direct:
        return direct

    for wrap in (
        "protocol",
        "usdm",
        "document",
        "data",
        "payload",
        "usdmStudy",
        "study",
        "clinicalStudy",
        "studyDesign",
    ):
        inner = data.get(wrap)
        if isinstance(inner, dict):
            hit = _from_mapping(inner) or extract_protocol_study_id(inner)
            if hit:
                return hit

    studies = data.get("studies")
    if isinstance(studies, list):
        for item in studies:
            if isinstance(item, dict):
                hit = _from_mapping(item) or extract_protocol_study_id(item)
                if hit:
                    return hit

    return _deep_scan(data)
