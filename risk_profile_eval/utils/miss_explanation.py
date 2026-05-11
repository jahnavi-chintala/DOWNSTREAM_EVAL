"""
Risk Profile Scenario 1 — Miss Explanation Generator.

For every missed risk (GT risk name that the generator did not emit),
classify WHY using the Field Provenance Policy (`miss_policy.yaml`) plus an
index of the uploaded USDM JSON. The index reuses
``risk_usdm_tracing.UsdmIndex`` which is already the authoritative implementation
for this product.

Emits two artifacts next to the standard eval output:
* ``<stem>_miss_explanation.json``
* ``<stem>_miss_explanation.md`` (colored with emoji badges)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from core.risk_usdm_tracing import UsdmIndex, build_usdm_index, load_usdm


_ID_FIELDS = ("id", "uuid")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "") if len(t) > 2}


def _name_hits(idx: UsdmIndex, required_type: str, text: str, limit: int = 3
               ) -> Tuple[int, List[Dict[str, str]]]:
    names = idx._names_by_type.get(required_type) or []
    if not names:
        return 0, []
    needle = (text or "").strip().lower()
    n_tokens = _tokens(text)
    hits = 0
    scored: List[Tuple[int, str, str]] = []
    for low, original, node in names:
        nt = _tokens(original)
        shared = len(n_tokens & nt)
        contains = bool(needle) and (needle in low or low in needle)
        if shared >= 2 or contains:
            hits += 1
        score = (10 if contains else 0) + shared
        nid = ""
        for f in _ID_FIELDS:
            if node.get(f):
                nid = str(node[f])
                break
        scored.append((score, original[:120], nid))
    scored.sort(key=lambda x: -x[0])
    cands = [{"id": nid, "name": nm} for s, nm, nid in scored[:limit] if s > 0]
    return hits, cands


VERDICT_CLASS_ABSENT = "USDM_CLASS_ABSENT"
VERDICT_CONTENT_ABSENT = "USDM_CONTENT_ABSENT"
VERDICT_CONTEXT_MISSING = "USDM_CONTEXT_MISSING"
VERDICT_PRESENT_GEN_MISSED = "USDM_PRESENT_GEN_MISSED"
VERDICT_OUT_OF_SCOPE = "GT_OUT_OF_SCOPE"
VERDICT_NO_USDM = "NO_USDM_UPLOADED"

_CLASS_DIRECT = "DIRECT"
_CLASS_STRUCTURAL = "STRUCTURAL"
_CLASS_INFERRED = "INFERRED"
_CLASS_COMPUTED = "COMPUTED"

_SIMPLE_REASON = {
    VERDICT_CLASS_ABSENT: (
        "The required USDM class is not present in the uploaded protocol, "
        "so the generator had no source node to draw from."
    ),
    VERDICT_CONTENT_ABSENT: (
        "The USDM class exists but no node mentions this specific item, "
        "so the generator could not source it from the protocol."
    ),
    VERDICT_CONTEXT_MISSING: (
        "This field is inferred from several USDM anchors; none of the "
        "required anchor classes are present in the protocol."
    ),
    VERDICT_PRESENT_GEN_MISSED: (
        "The concept exists in the protocol — this is a generator miss."
    ),
    VERDICT_OUT_OF_SCOPE: (
        "This field is computed / editorial; USDM is not expected to carry it."
    ),
    VERDICT_NO_USDM: (
        "No USDM JSON was uploaded; traceability cannot be verified."
    ),
}


def _load_policy(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _policy_for(field: str, policy: Dict[str, Any]
                ) -> Tuple[str, Optional[str], List[str]]:
    fields = (policy or {}).get("fields") or {}
    default_class = (policy or {}).get("default_class") or _CLASS_INFERRED
    default_anchors = list((policy or {}).get("default_anchors") or [])
    entry = fields.get(field) or {}
    cls = str(entry.get("class") or default_class).upper()
    req = entry.get("required_type")
    anchors = list(entry.get("anchors") or [])
    if not anchors:
        anchors = default_anchors
    return cls, req, anchors


def _classify(field_name: str, gt_text: str, idx: Optional[UsdmIndex],
              policy: Dict[str, Any]) -> Dict[str, Any]:
    cls, required_type, anchors = _policy_for(field_name, policy)
    base = {
        "section": "m1_risks",
        "field": field_name,
        "gt_text": gt_text,
        "policy_class": cls,
        "required_type": required_type,
        "anchors": anchors,
        "evidence": {},
        "candidates": [],
        "verdict": "",
        "reason": "",
    }
    if idx is None or idx.empty:
        base["verdict"] = VERDICT_OUT_OF_SCOPE if cls == _CLASS_COMPUTED else VERDICT_NO_USDM
        base["reason"] = _SIMPLE_REASON[base["verdict"]]
        return base
    if cls == _CLASS_COMPUTED:
        base["verdict"] = VERDICT_OUT_OF_SCOPE
        base["reason"] = _SIMPLE_REASON[VERDICT_OUT_OF_SCOPE]
        return base
    if cls == _CLASS_DIRECT:
        if not required_type or required_type not in idx.by_type:
            base["verdict"] = VERDICT_CLASS_ABSENT
            base["evidence"] = {"type_present": False, "types_available": idx.types_available()[:8]}
            base["reason"] = _SIMPLE_REASON[VERDICT_CLASS_ABSENT]
            return base
        hits, cands = _name_hits(idx, required_type, gt_text)
        base["evidence"] = {
            "type_present": True, "name_hits": hits,
            "type_node_count": len(idx.by_type.get(required_type, [])),
        }
        base["candidates"] = cands
        if hits == 0:
            base["verdict"] = VERDICT_CONTENT_ABSENT
            base["reason"] = _SIMPLE_REASON[VERDICT_CONTENT_ABSENT]
        else:
            base["verdict"] = VERDICT_PRESENT_GEN_MISSED
            base["reason"] = _SIMPLE_REASON[VERDICT_PRESENT_GEN_MISSED]
        return base
    if cls == _CLASS_STRUCTURAL:
        if not required_type or required_type not in idx.by_type:
            base["verdict"] = VERDICT_CLASS_ABSENT
            base["evidence"] = {"type_present": False, "types_available": idx.types_available()[:8]}
            base["reason"] = _SIMPLE_REASON[VERDICT_CLASS_ABSENT]
        else:
            base["verdict"] = VERDICT_PRESENT_GEN_MISSED
            base["evidence"] = {"type_present": True,
                                "type_node_count": len(idx.by_type.get(required_type, []))}
            base["reason"] = _SIMPLE_REASON[VERDICT_PRESENT_GEN_MISSED]
        return base
    anchor_presence = {a: (a in idx.by_type) for a in anchors}
    any_present = any(anchor_presence.values())
    base["evidence"] = {"anchors_present": anchor_presence, "any_anchor_present": any_present}
    if not any_present:
        base["verdict"] = VERDICT_CONTEXT_MISSING
        base["reason"] = _SIMPLE_REASON[VERDICT_CONTEXT_MISSING]
    else:
        base["verdict"] = VERDICT_PRESENT_GEN_MISSED
        base["reason"] = _SIMPLE_REASON[VERDICT_PRESENT_GEN_MISSED]
    return base


def _iter_risk_misses(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    metrics = (report.get("metrics") or {})
    m1 = metrics.get("m1_risk_name_recall") or report.get("m1_risk_name_recall") or {}
    for name in m1.get("missed_names") or []:
        if str(name or "").strip():
            out.append({"field": "risk_name", "gt_text": str(name)})
    return out


_VERDICT_BADGE = {
    VERDICT_CLASS_ABSENT:       ("🟥", "USDM class missing"),
    VERDICT_CONTENT_ABSENT:     ("🟥", "USDM content missing"),
    VERDICT_CONTEXT_MISSING:    ("🟨", "USDM context missing"),
    VERDICT_PRESENT_GEN_MISSED: ("🟦", "Generator missed"),
    VERDICT_OUT_OF_SCOPE:       ("⬜", "Out of USDM scope"),
    VERDICT_NO_USDM:            ("⬛", "No USDM uploaded"),
}


def _render_markdown(payload: Dict[str, Any]) -> str:
    summary = payload["summary"]
    items = payload["items"]
    total = summary["total_misses"]
    lines: List[str] = []
    lines.append(f"# Risk Profile Miss Explanation — {payload['study_id']}")
    lines.append("")
    lines.append(f"**Product:** `{payload['product']}`  |  **Scenario:** `1`  "
                 f"|  **USDM:** `{payload.get('usdm_protocol_json_path') or '(none)'}`")
    lines.append("")
    lines.append("## Attribution summary")
    lines.append("")
    lines.append(f"- Total misses: **{total}**")
    lines.append(f"- 🟦 Generator-attributable: **{summary['USDM_PRESENT_GEN_MISSED']}**"
                 f" ({summary['attributable_to_model_pct']}%)")
    lines.append(f"- 🟥 USDM data gap: "
                 f"**{summary['USDM_CLASS_ABSENT'] + summary['USDM_CONTENT_ABSENT']}**"
                 f" ({summary['attributable_to_usdm_pct']}%)")
    lines.append(f"- 🟨 USDM context missing: **{summary['USDM_CONTEXT_MISSING']}**")
    lines.append(f"- ⬜ Out of USDM scope: **{summary['GT_OUT_OF_SCOPE']}**"
                 f" ({summary['out_of_scope_pct']}%)")
    if summary.get("NO_USDM_UPLOADED"):
        lines.append(f"- ⬛ Not verifiable: **{summary['NO_USDM_UPLOADED']}**")
    lines.append("")
    lines.append("## Legend")
    lines.append("")
    lines.append("| Badge | Meaning | Actionable by |")
    lines.append("|---|---|---|")
    lines.append("| 🟦 | Protocol had the concept — generator missed it | Model team |")
    lines.append("| 🟥 | USDM protocol did not carry the class or the content | Protocol / data team |")
    lines.append("| 🟨 | Inferred field; none of the needed USDM anchors present | Protocol / data team |")
    lines.append("| ⬜ | Computed / editorial — never expected from USDM | No action |")
    lines.append("| ⬛ | No USDM JSON uploaded; traceability cannot be verified | Re-run with USDM |")
    lines.append("")
    lines.append("## Per-miss details")
    lines.append("")
    lines.append("| # | Field | GT Text | Policy | Verdict | Reason |")
    lines.append("|---|---|---|---|---|---|")
    for i, it in enumerate(items, 1):
        badge, label = _VERDICT_BADGE.get(it["verdict"], ("", it["verdict"]))
        gt_text = (it.get("gt_text") or "").replace("|", "\\|")
        if len(gt_text) > 80:
            gt_text = gt_text[:77] + "..."
        reason = (it.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| {i} | `{it['field']}` | {gt_text} | "
            f"`{it['policy_class']}` | {badge} **{label}** | {reason} |"
        )
    lines.append("")
    return "\n".join(lines)


_PKG = Path(__file__).resolve().parent
_DEFAULT_POLICY = _PKG.parent / "config" / "miss_policy.yaml"


def build_miss_explanation(
    report: Dict[str, Any],
    usdm_json_path: Optional[str | Path],
    *,
    policy_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    policy = _load_policy(Path(policy_path) if policy_path else _DEFAULT_POLICY)
    usdm = load_usdm(usdm_json_path) if usdm_json_path else None
    idx = build_usdm_index(usdm) if usdm is not None else UsdmIndex()

    misses = _iter_risk_misses(report)
    items = [_classify(m["field"], m["gt_text"], idx, policy) for m in misses]

    counts = {
        VERDICT_CLASS_ABSENT: 0,
        VERDICT_CONTENT_ABSENT: 0,
        VERDICT_CONTEXT_MISSING: 0,
        VERDICT_PRESENT_GEN_MISSED: 0,
        VERDICT_OUT_OF_SCOPE: 0,
        VERDICT_NO_USDM: 0,
    }
    for it in items:
        counts[it["verdict"]] = counts.get(it["verdict"], 0) + 1
    total = len(items) or 1
    model_pct = round(100.0 * counts[VERDICT_PRESENT_GEN_MISSED] / total, 1)
    usdm_pct = round(
        100.0 * (counts[VERDICT_CLASS_ABSENT] + counts[VERDICT_CONTENT_ABSENT]
                 + counts[VERDICT_CONTEXT_MISSING]) / total, 1)
    oos_pct = round(100.0 * counts[VERDICT_OUT_OF_SCOPE] / total, 1)

    study_id = (report.get("study_id")
                or (report.get("eval_metadata") or {}).get("study_id")
                or "")
    return {
        "product": "RISK_PROFILE",
        "scenario": 1,
        "study_id": str(study_id),
        "usdm_protocol_json_path": str(usdm_json_path) if usdm_json_path else None,
        "usdm_types_available": idx.types_available(),
        "summary": {
            "total_misses": len(items),
            **counts,
            "attributable_to_model_pct": model_pct,
            "attributable_to_usdm_pct": usdm_pct,
            "out_of_scope_pct": oos_pct,
        },
        "items": items,
    }


def write_miss_explanation(
    report: Dict[str, Any],
    usdm_json_path: Optional[str | Path],
    out_dir: str | Path,
    stem: str,
    *,
    policy_path: Optional[str | Path] = None,
) -> Dict[str, Path]:
    payload = build_miss_explanation(report, usdm_json_path, policy_path=policy_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_p = out / f"{stem}_miss_explanation.json"
    md_p = out / f"{stem}_miss_explanation.md"
    json_p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_p.write_text(_render_markdown(payload), encoding="utf-8")
    return {"json": json_p, "md": md_p}
