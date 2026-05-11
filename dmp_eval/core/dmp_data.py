"""Load DMP generator JSON and ground-truth rows for one study."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_dmp_json(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def infer_study_id(dmp: Dict[str, Any], filepath: str | Path) -> str:
    sid = dmp.get("study_folder") or dmp.get("study_id")
    if sid:
        return str(sid).strip().upper()
    stem = Path(filepath).stem
    for pref in ("_DMP",):
        if pref in stem.upper():
            i = stem.upper().find(pref)
            if i >= 8:
                cand = stem[i - 8 : i]
                if len(cand) == 8 and cand[0].upper() in "BC":
                    return cand.upper()
    raise ValueError(f"Could not infer study id from DMP JSON {filepath}")


def load_dmp_gt_record(gt_path: str | Path, study_id: str) -> Dict[str, Any]:
    raw = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("dmp_ground_truth_clean.json must be a list of study records")
    sid = study_id.strip().upper()
    for rec in raw:
        if str(rec.get("study_folder", "")).strip().upper() == sid:
            return rec
    raise ValueError(f"No ground-truth record for study_folder={sid}")


def load_sds_rows(
    csv_path: str | Path,
    study_id: str,
) -> List[Dict[str, Any]]:
    """All rows for study (no split filter — single scenario)."""
    sid = study_id.strip().upper()
    rows: List[Dict[str, Any]] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            if str(r.get("study_folder", "")).strip().upper() == sid:
                rows.append(dict(r))
    return rows


def generation_fallback_used(dmp: Dict[str, Any]) -> bool:
    meta = dmp.get("generation_metadata")
    if isinstance(meta, dict) and bool(meta.get("fallback_used")):
        return True
    return bool(dmp.get("fallback_used"))


def s5_other_systems(dmp: Dict[str, Any]) -> Dict[str, Any]:
    s5 = dmp.get("S5_systems_tools") or dmp.get("S5_systems") or {}
    inner = s5.get("S5_2_other_systems") or {}
    out = dict(inner) if isinstance(inner, dict) else {}

    # DIVA is often described in S5.1 free text rather than as a structured S5.2 key.
    # Create a synthetic structured key so S5 scoring can validate it explicitly.
    s51 = s5.get("S5_1_data_reporting_sources") or {}
    s51_text = str((s51.get("text") if isinstance(s51, dict) else "") or "")
    if "DIVA" in s51_text.upper() and "diva_application" not in out:
        out["diva_application"] = {
            "value": "Data Integrity and Validation Application (DIVA)",
            "applicable": True,
            "confidence": "AUTO_YES",
            "source": "S5_1_data_reporting_sources text contains DIVA reference",
        }
    return out


def tier_from_rationale(rationale: str) -> str:
    t = (rationale or "").lower()
    if "minimal" in t:
        return "Minimal"
    if "primary endpoint" in t or ("critical process" in t and "secondary" not in t):
        return "Critical"
    if "critical" in t and "secondary" not in t and "safety" not in t:
        return "Critical"
    if "safety" in t and "critical" not in t:
        return "Supportive"
    return "Supportive"


def expected_s8_confidence_from_layer(layer: int | str) -> str:
    try:
        n = int(layer)
    except (TypeError, ValueError):
        return "medium"
    if n <= 2:
        return "high"
    return "medium"


def norm_s5_confidence(val: Any) -> str:
    s = str(val or "").upper().replace(" ", "_")
    if "AUTO" in s and "YES" in s:
        return "auto_yes"
    if "REVIEW" in s:
        return "review"
    if "OMIT" in s:
        return "omit"
    return "review"


def gen_s8_confidence_tier(mod: Dict[str, Any]) -> str:
    c = str(mod.get("confidence") or "").upper()
    if c == "AUTO_YES":
        return "high"
    if c == "REVIEW":
        return "medium"
    return expected_s8_confidence_from_layer(mod.get("layer", 2))


def normalize_s8_source_tag(mod: Dict[str, Any]) -> Optional[str]:
    src = mod.get("source")
    if src is None or (isinstance(src, str) and not str(src).strip()):
        return None
    s = str(src).strip()
    layer = mod.get("layer")
    if layer is not None:
        try:
            li = int(layer)
            if li == 1 and "USDM" in s.upper():
                return "usdm_endpoint:primary"
            if li == 2:
                return f"yaml_l2_standard:{s[:40].replace(' ', '_')}"
            return f"yaml_l3_conditional:{s[:40].replace(' ', '_')}"
        except (TypeError, ValueError):
            pass
    return s[:120].replace(" ", "_").lower()


def s11_gen_sections(dmp: Dict[str, Any]) -> Dict[str, Any]:
    s11 = dmp.get("S11_data_review_validation") or {}
    r4 = s11.get("S11_4_reconciliation") or {}
    sec = r4.get("sections") or {}
    return sec if isinstance(sec, dict) else {}
