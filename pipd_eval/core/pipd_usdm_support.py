"""
USDM protocol JSON support: resolve file under ``data/``, collect entity ``id`` values
and ``instanceType`` strings, and compute intelligence / truth summaries for reports.

Used by composite + reference eval report and ``pipd_intelligence_truth_report.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.eval_scenario1 import NULL_PLACEHOLDERS, PROVENANCE_EXEMPT_CONFIDENCE, load_generator_json
from core.pipd_usdm_provenance import (
    index_usdm_nodes_by_id,
    index_usdm_nodes_by_instance_type,
    resolve_usdm_source_for_subcategory,
)

_PKG_DIR = Path(__file__).resolve().parent


def collect_usdm_ids(obj: Any, out: Optional[Set[str]] = None) -> Set[str]:
    """Recursively collect every string ``id`` from a USDM JSON tree."""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "id" and isinstance(v, str) and v.strip():
                out.add(v.strip())
            else:
                collect_usdm_ids(v, out)
    elif isinstance(obj, list):
        for x in obj:
            collect_usdm_ids(x, out)
    return out


def collect_instance_types(obj: Any, out: Optional[Set[str]] = None) -> Set[str]:
    """Recursively collect every ``instanceType`` string (USDM class names)."""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        it = obj.get("instanceType")
        if isinstance(it, str) and it.strip():
            out.add(it.strip())
        for v in obj.values():
            collect_instance_types(v, out)
    elif isinstance(obj, list):
        for x in obj:
            collect_instance_types(x, out)
    return out


def _study_id_in_usdm(data: Any, study_id: str) -> bool:
    """Return True if ``study_id`` appears under a studyIdentifiers[].text-style path."""
    if isinstance(data, dict):
        if data.get("instanceType") == "StudyIdentifier":
            t = data.get("text")
            if isinstance(t, str) and study_id in t:
                return True
        for v in data.values():
            if _study_id_in_usdm(v, study_id):
                return True
    elif isinstance(data, list):
        for x in data:
            if _study_id_in_usdm(x, study_id):
                return True
    return False


def resolve_usdm_protocol_path(study_id: str, data_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Resolve USDM protocol JSON path.

    Order:
      1. ``PIPD_USDM_JSON`` environment variable (absolute or relative to cwd / package).
      2. ``data_dir / f\"usdm_protocol_{study_id}.json\"``
      3. Any ``*.json`` under ``data_dir`` (excluding ``*_PIPD.json``) that contains
         this ``study_id`` in ``StudyIdentifier`` text fields.
    """
    explicit = os.environ.get("PIPD_USDM_JSON", "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            c = Path.cwd() / p
            if c.is_file():
                return c.resolve()
            q = _PKG_DIR / p
            if q.is_file():
                return q.resolve()
        return p if p.is_file() else None

    dd = data_dir or (_PKG_DIR / "data")
    if not dd.is_dir():
        return None

    direct = dd / f"usdm_protocol_{study_id}.json"
    if direct.is_file():
        return direct.resolve()

    for path in sorted(dd.glob("*.json")):
        name = path.name.upper()
        if "_PIPD.JSON" in name or name.endswith("_PIPD.JSON"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if _study_id_in_usdm(data, study_id):
            return path.resolve()
    return None


def _is_null_id(val: Any) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in NULL_PLACEHOLDERS or s == ""


def usdm_trace_for_subcategory(
    sub: Dict[str, Any],
    usdm_ids: Set[str],
    instance_types: Set[str],
) -> Dict[str, Any]:
    """
    Classify one subcategory row against USDM protocol data.

    Returns keys: symbol (str), detail (str), id_in_protocol (bool|None), type_in_protocol (bool|None).
    """
    conf = str(sub.get("confidence") or "")
    exempt = conf in PROVENANCE_EXEMPT_CONFIDENCE

    uid = sub.get("usdm_entity_id")
    utype = sub.get("usdm_entity")
    utype_s = str(utype).strip() if utype is not None else ""

    id_ok: Optional[bool] = None
    if not _is_null_id(uid):
        sid = str(uid).strip()
        id_ok = sid in usdm_ids

    type_ok: Optional[bool] = None
    if utype_s:
        type_ok = utype_s in instance_types

    if exempt:
        if id_ok is True:
            return {"symbol": "✓", "detail": "id in protocol", "id_in_protocol": True, "type_in_protocol": type_ok}
        if id_ok is False:
            return {"symbol": "✗", "detail": "id not in protocol", "id_in_protocol": False, "type_in_protocol": type_ok}
        if type_ok is True:
            return {"symbol": "~", "detail": "type in protocol (exempt conf.)", "id_in_protocol": None, "type_in_protocol": True}
        return {"symbol": "—", "detail": "exempt / no id", "id_in_protocol": None, "type_in_protocol": type_ok}

    # Non-exempt: must have resolvable protocol reference
    if id_ok is True:
        return {"symbol": "✓", "detail": "id in protocol", "id_in_protocol": True, "type_in_protocol": type_ok}
    if id_ok is False:
        return {"symbol": "✗", "detail": "id not in protocol", "id_in_protocol": False, "type_in_protocol": type_ok}
    if type_ok is True:
        return {"symbol": "~", "detail": "type only in protocol (no id)", "id_in_protocol": None, "type_in_protocol": True}
    return {"symbol": "✗", "detail": "no resolvable USDM ref", "id_in_protocol": None, "type_in_protocol": False}


def build_usdm_and_truth_block(
    generator_json_path: str,
    study_id: str,
    composite_result: Dict[str, Any],
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Build ``usdm_protocol`` + ``intelligence_truth`` blocks for embedding in composite JSON / reports.

    ``intelligence_truth`` summarises how much of the generator (intelligence) output is supported by
    Actual PIPD (GT) and by the USDM protocol file when available.
    """
    out: Dict[str, Any] = {"usdm_protocol": {}, "intelligence_truth": {}}

    usdm_path = resolve_usdm_protocol_path(study_id, data_dir=data_dir)
    if not usdm_path:
        out["usdm_protocol"] = {
            "loaded": False,
            "path": None,
            "message": "No USDM JSON resolved. Set PIPD_USDM_JSON or add data/usdm_protocol_{study_id}.json "
            "or a protocol JSON under data/ whose StudyIdentifier text matches the study.",
        }
        _truth_without_usdm(out, composite_result)
        return out

    try:
        with open(usdm_path, encoding="utf-8") as fh:
            usdm_root = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        out["usdm_protocol"] = {"loaded": False, "path": str(usdm_path), "message": str(exc)}
        _truth_without_usdm(out, composite_result)
        return out

    usdm_ids = collect_usdm_ids(usdm_root)
    inst_types = collect_instance_types(usdm_root)
    by_id = index_usdm_nodes_by_id(usdm_root)
    type_index = index_usdm_nodes_by_instance_type(usdm_root)
    out["usdm_protocol"] = {
        "loaded": True,
        "path": str(usdm_path),
        "distinct_id_count": len(usdm_ids),
        "distinct_instance_type_count": len(inst_types),
    }

    gen = load_generator_json(generator_json_path)
    flat_subs: List[Dict[str, Any]] = []
    for cat in gen.get("categories") or []:
        cn = cat.get("category_num")
        for s in cat.get("subcategories") or []:
            row = dict(s)
            row["_category_num"] = int(cn) if cn is not None else None
            flat_subs.append(row)

    trace_rows: List[Dict[str, Any]] = []
    need_prov = 0
    need_prov_ok = 0
    for sub in flat_subs:
        tr = usdm_trace_for_subcategory(sub, usdm_ids, inst_types)
        conf = str(sub.get("confidence") or "")
        if conf not in PROVENANCE_EXEMPT_CONFIDENCE:
            need_prov += 1
            if tr["symbol"] == "✓" or (tr["symbol"] == "~" and tr.get("type_in_protocol")):
                need_prov_ok += 1
        pipd_txt = str(sub.get("subcategory_text") or "")
        src_line, src_meth, _eval_entity_id, _eval_entity_type = resolve_usdm_source_for_subcategory(
            sub, pipd_txt, by_id, type_index
        )
        trace_rows.append(
            {
                "category_num": sub.get("_category_num"),
                "subcategory_text": sub.get("subcategory_text"),
                "confidence": conf,
                "usdm_entity_id": sub.get("usdm_entity_id"),
                "usdm_entity": sub.get("usdm_entity"),
                "usdm_symbol": tr["symbol"],
                "usdm_detail": tr["detail"],
                "usdm_source": src_line,
                "usdm_source_method": src_meth,
            }
        )

    trace_pct = 100.0 * need_prov_ok / need_prov if need_prov else 100.0

    s1 = composite_result.get("scenario1_evaluation") or {}
    m1 = (s1.get("metrics") or {}).get("m1_subcategory_recall") or {}
    recall = float(m1.get("score") or 0.0)
    recall_pct = recall * 100.0

    combined = 0.5 * recall_pct + 0.5 * trace_pct

    per = composite_result.get("per_category") or {}
    matched_gt = 0
    total_gt = 0
    extra_gen = 0
    for _ck, block in per.items():
        for r in block.get("rows") or []:
            if r.get("hallucination"):
                extra_gen += 1
            elif r.get("ground_truth"):
                total_gt += 1
                if r.get("present"):
                    matched_gt += 1

    out["intelligence_truth"] = {
        "summary": (
            "Ground-truth alignment uses Scenario 1 subcategory recall (Actual PIPD matched to generated). "
            "Protocol traceability is the share of non-exempt subcategories with a USDM entity id present in "
            "the protocol JSON or, if no id, a usdm_entity type that appears in the protocol. "
            "Combined truth index is the simple average of those two percentages."
        ),
        "ground_truth_recall_0_1": recall,
        "ground_truth_recall_percent": round(recall_pct, 2),
        "protocol_traceability_percent": round(trace_pct, 2),
        "combined_truth_index_percent": round(combined, 2),
        "non_exempt_subcategories": need_prov,
        "non_exempt_with_protocol_ref": need_prov_ok,
        "per_subcategory_usdm": trace_rows,
        "composite_gt_rows_matched": matched_gt,
        "composite_gt_rows_total": total_gt,
        "composite_extra_generated_rows": extra_gen,
    }
    return out


def _truth_without_usdm(out: Dict[str, Any], composite_result: Dict[str, Any]) -> None:
    s1 = composite_result.get("scenario1_evaluation") or {}
    m1 = (s1.get("metrics") or {}).get("m1_subcategory_recall") or {}
    recall = float(m1.get("score") or 0.0)
    recall_pct = recall * 100.0
    out["intelligence_truth"] = {
        "summary": "USDM protocol file not loaded; only ground-truth recall is available below.",
        "ground_truth_recall_0_1": recall,
        "ground_truth_recall_percent": round(recall_pct, 2),
        "protocol_traceability_percent": None,
        "combined_truth_index_percent": round(recall_pct, 2),
        "non_exempt_subcategories": None,
        "non_exempt_with_protocol_ref": None,
        "per_subcategory_usdm": [],
    }


def build_intelligence_truth_markdown(study_id: str, block: Dict[str, Any]) -> str:
    """Standalone Markdown document for stakeholders."""
    lines: List[str] = []
    usdm = block.get("usdm_protocol") or {}
    truth = block.get("intelligence_truth") or {}

    lines.append(f"# Intelligence output — truth verification — `{study_id}`")
    lines.append("")
    lines.append(
        "_This report treats the **PIPD generator JSON** as the intelligence output. "
        "It checks each subcategory against **ground truth** (via embedded composite/Scenario 1 recall) "
        "and against the **USDM protocol JSON** when available._"
    )
    lines.append("")

    if usdm.get("loaded"):
        lines.append("## USDM protocol")
        lines.append("")
        lines.append(f"- **File:** `{usdm.get('path')}`")
        lines.append(f"- **Distinct USDM `id` values:** {usdm.get('distinct_id_count')}")
        lines.append(f"- **Distinct `instanceType` values:** {usdm.get('distinct_instance_type_count')}")
        lines.append("")
    else:
        lines.append("## USDM protocol")
        lines.append("")
        lines.append(f"_{usdm.get('message', 'Not loaded')}_")
        lines.append("")

    lines.append("## How much is true?")
    lines.append("")
    lines.append(f"- **Ground-truth alignment (recall):** {truth.get('ground_truth_recall_percent', '—')}%")
    tp = truth.get("protocol_traceability_percent")
    lines.append(
        f"- **Protocol traceability (non-exempt rows):** {tp if tp is not None else '—'}%"
        + (
            f" ({truth.get('non_exempt_with_protocol_ref')}/{truth.get('non_exempt_subcategories')} rows)"
            if truth.get("non_exempt_subcategories") is not None
            else ""
        )
    )
    lines.append(f"- **Combined truth index** (average of the two when both exist): **{truth.get('combined_truth_index_percent', '—')}%**")
    lines.append("")
    if truth.get("summary"):
        lines.append(f"_{truth['summary']}_")
        lines.append("")

    rows = truth.get("per_subcategory_usdm") or []
    if rows:
        lines.append("## Per-subcategory USDM traceability")
        lines.append("")
        lines.append(
            "_**USDM source** shows where in the protocol JSON the line is grounded "
            "(id lookup, lexical match on `usdm_entity` type, or AI-assisted if enabled)._"
        )
        lines.append("")
        lines.append("| Cat | Conf. | Sym | Protocol source (USDM) | Deviation (truncated) |")
        lines.append("|-----|-------|-----|-------------------------|-------------------------|")
        for r in rows:
            txt = str(r.get("subcategory_text") or "").replace("\n", " ").replace("|", "\\|")
            if len(txt) > 70:
                txt = txt[:69] + "…"
            sym = r.get("usdm_symbol", "—")
            src = str(r.get("usdm_source") or "—").replace("\n", " ").replace("|", "\\|")
            if len(src) > 100:
                src = src[:99] + "…"
            lines.append(
                f"| {r.get('category_num', '—')} | {r.get('confidence', '—')} | {sym} | {src} | {txt} |"
            )
        lines.append("")

    return "\n".join(lines)
