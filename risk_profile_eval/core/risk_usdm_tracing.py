"""
risk_usdm_tracing.py
--------------------
USDM-backed tracing for the Risk Profile evaluator's M4 (Hallucination
Detection) metric.

Historical behaviour: M4 looked at the generator JSON and flagged a field
only when it was empty / null. It never consulted the protocol USDM JSON,
so a risk could claim ``usdm_drivers = [{"entity": "Endpoint", "signal":
"…", "usdm_id": "N/A"}]`` and pass the check.

This module fixes that by verifying every claimed USDM reference against
an index built from the uploaded USDM JSON:

* ``load_usdm(path)``            — load the protocol USDM JSON.
* ``build_usdm_index(root)``     — index nodes by id and by instanceType.
* ``trace_reference(ref, idx)``  — classify one ``{entity, signal, usdm_id}``
  reference as ``id_match`` / ``name_match`` / ``type_only`` / ``unresolved``.
* ``collect_risk_trace_issues(...)`` / ``collect_factor_trace_issues(...)``
  — enumerate trace issues for a whole risk or critical-factor block.

The resolver uses layered strategies:

1. **id_match** — the claimed ``usdm_id`` is an exact key in the USDM id
   index. Strongest evidence that the field was actually sourced from USDM.
2. **name_match** — the claimed ``signal`` text substring-matches the
   ``name`` / ``label`` / ``text`` of a USDM node whose ``instanceType``
   equals the claimed ``entity``. Credits generators that emit names
   instead of ids.
3. **type_only** — ``entity`` matches an ``instanceType`` present in the protocol
   USDM but there is no claimed id and no name-level match. This is acceptable
   protocol traceability for M4 (aligned with PIPD intelligence-truth: id in
   protocol *or* declared type present when id is absent).

4. **unresolved** — a concrete ``usdm_id`` was claimed but is absent from the
   protocol USDM (after optional name rescue), or there is no usable id and the
   claimed entity type is not in the USDM. Only these are counted as M4 defects
   when protocol USDM is loaded.

When no USDM is uploaded the functions can still be called: the resolver
degrades gracefully and returns ``status == "no_usdm"`` so callers can fall
back to the old "non-empty" check without crashing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Loading and indexing
# ─────────────────────────────────────────────────────────────────────────────

_NAME_FIELDS = ("name", "label", "text", "description")
_ID_FIELDS = ("id", "uuid")


def load_usdm(path: Optional[str | Path]) -> Optional[Dict[str, Any]]:
    """Load USDM JSON from disk. Returns ``None`` if path missing / invalid."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class UsdmIndex:
    """In-memory indexes of a USDM JSON tree."""

    by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_type: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # Every node's name/label/text lowercased, keyed by type, for fast
    # substring scan. Each entry: (lower_name, original_name, node).
    _names_by_type: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = field(
        default_factory=dict
    )

    @property
    def empty(self) -> bool:
        return not self.by_id and not self.by_type

    def types_available(self) -> List[str]:
        return sorted(self.by_type.keys())


def build_usdm_index(root: Any) -> UsdmIndex:
    """Walk a USDM JSON tree and index nodes by id and by instanceType.

    We treat any dict that has either a recognisable id field or an
    ``instanceType`` / ``_type`` key as a USDM node. That matches how
    the PIPD ``pipd_usdm_provenance.index_usdm_nodes_by_id`` helper
    behaves today and lets us apply the same lookup semantics here.
    """
    idx = UsdmIndex()
    if root is None:
        return idx

    stack: List[Any] = [root]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            inst = obj.get("instanceType") or obj.get("_type") or obj.get("type")
            node_id = None
            for id_field in _ID_FIELDS:
                if obj.get(id_field) and isinstance(obj[id_field], (str, int)):
                    node_id = str(obj[id_field])
                    break
            if inst or node_id:
                if node_id:
                    idx.by_id.setdefault(node_id, obj)
                if inst:
                    inst_key = str(inst).strip()
                    idx.by_type.setdefault(inst_key, []).append(obj)
                    # build name index for substring matching
                    for f in _NAME_FIELDS:
                        v = obj.get(f)
                        if isinstance(v, str) and v.strip():
                            idx._names_by_type.setdefault(inst_key, []).append(
                                (v.lower(), v, obj)
                            )
                            break
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(obj, list):
            stack.extend(obj)

    return idx


# ─────────────────────────────────────────────────────────────────────────────
# Per-reference tracing
# ─────────────────────────────────────────────────────────────────────────────

TRACE_STATUS_ID_MATCH = "id_match"
TRACE_STATUS_NAME_MATCH = "name_match"
TRACE_STATUS_TYPE_ONLY = "type_only"
TRACE_STATUS_UNRESOLVED = "unresolved"
TRACE_STATUS_NO_USDM = "no_usdm"


_WORD_SPLIT_RE = re.compile(r"[\s,;:/\\\-_()\[\]]+")


def _normalize_for_substring(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


_ID_PLACEHOLDER_LOWER = frozenset(
    {
        "n/a",
        "none",
        "null",
        "placeholder",
        "tbd",
    }
)


def _effective_claimed_usdm_id(usdm_id: str) -> Optional[str]:
    """Non-empty id string that is not a null/placeholder token, else None."""
    s = str(usdm_id).strip()
    if not s or s.lower() in _ID_PLACEHOLDER_LOWER:
        return None
    return s


def _try_name_match(entity: str, signal: str, idx: UsdmIndex, out: Dict[str, Any]) -> bool:
    """If ``entity`` + ``signal`` resolve a node by name, mutate ``out`` and return True."""
    if not entity or not signal or entity not in idx.by_type:
        return False
    candidates = idx._names_by_type.get(entity, [])
    needles = [
        _normalize_for_substring(s) for s in _signal_name_candidates(signal)
    ]
    for needle in needles:
        if not needle:
            continue
        for low, original, node in candidates:
            if needle in low or low in needle:
                node_id = None
                for f in _ID_FIELDS:
                    if node.get(f):
                        node_id = str(node[f])
                        break
                out["status"] = TRACE_STATUS_NAME_MATCH
                out["matched_node_id"] = node_id
                out["reason"] = (
                    f"Matched on name within instanceType={entity}: "
                    f"'{original[:80]}'"
                )
                return True
    return False


def _signal_name_candidates(signal: str) -> List[str]:
    """Generate progressively looser tokens to try for substring matching."""
    raw = signal.strip()
    if not raw:
        return []
    cands: List[str] = [raw]
    # If the signal has "entity: name" style, also try just the name.
    for sep in (" - ", ": ", " — ", ": ", " – "):
        if sep in raw:
            _, _, tail = raw.partition(sep)
            if tail and tail not in cands:
                cands.append(tail)
    # If comma-separated (multiple names crammed together), split.
    if "," in raw:
        for piece in raw.split(","):
            piece = piece.strip()
            if piece and piece not in cands:
                cands.append(piece)
    return cands


def trace_reference(ref: Dict[str, Any], idx: Optional[UsdmIndex]) -> Dict[str, Any]:
    """Classify one USDM reference dict against the USDM index.

    ``ref`` is expected to expose some subset of: ``entity`` /
    ``usdm_id`` (or ``id``) / ``signal`` (or ``text`` / ``name``).

    The returned dict always carries:
        * ``status``       — one of the TRACE_STATUS_* constants
        * ``entity``       — claimed instanceType (str)
        * ``signal``       — claimed name text (str)
        * ``usdm_id``      — claimed id (str, may be empty)
        * ``matched_node_id`` — USDM id we resolved to (if any)
        * ``reason``       — short human-readable explanation
        * ``candidates``   — up to 3 "nearest" USDM nodes of the same
                             ``entity`` type, each as ``{"id": str,
                             "name": str}`` (helps the user debug)
    """
    entity = str(ref.get("entity") or "").strip()
    signal = str(ref.get("signal") or ref.get("text") or ref.get("name") or "").strip()
    usdm_id = str(ref.get("usdm_id") or ref.get("id") or "").strip()

    out: Dict[str, Any] = {
        "entity": entity,
        "signal": signal,
        "usdm_id": usdm_id,
        "matched_node_id": None,
        "candidates": [],
        "status": TRACE_STATUS_UNRESOLVED,
        "reason": "",
    }

    if idx is None or idx.empty:
        out["status"] = TRACE_STATUS_NO_USDM
        out["reason"] = "No USDM JSON uploaded; cannot verify trace."
        return out

    eff_id = _effective_claimed_usdm_id(usdm_id)

    # 1) Strong id match — concrete claimed id resolves in the protocol graph.
    if eff_id is not None:
        node = idx.by_id.get(eff_id)
        if node is not None:
            out["status"] = TRACE_STATUS_ID_MATCH
            out["matched_node_id"] = eff_id
            out["reason"] = f"USDM node '{eff_id}' exists."
            return out
        # PIPD handover parity: if an id was asserted, do not downgrade to type-only pass.
        if _try_name_match(entity, signal, idx, out):
            return out
        out["reason"] = (
            f"Claimed usdm_id '{eff_id}' not found in protocol USDM "
            "(and no name-level match)."
        )
        out["candidates"] = _top_candidates(entity, signal, idx, limit=3)
        out["status"] = TRACE_STATUS_UNRESOLVED
        return out

    # 2) No id asserted — match by signal text within claimed instanceType…
    if _try_name_match(entity, signal, idx, out):
        return out

    # 3) type_only — entity type exists in protocol; id not asserted / placeholder.
    if entity and entity in idx.by_type:
        out["status"] = TRACE_STATUS_TYPE_ONLY
        out["reason"] = (
            f"instanceType={entity} exists in protocol USDM (acceptable type-level "
            f"trace when no usdm_id is asserted); no signal name matched a specific node."
        )
        # Provide up to 3 candidate names for debugging
        out["candidates"] = _top_candidates(entity, signal, idx, limit=3)
        return out

    # 4) unresolved
    if entity and entity not in idx.by_type:
        out["reason"] = (
            f"Claimed instanceType '{entity}' not present in USDM "
            f"(available: {', '.join(idx.types_available()[:8])}…)."
        )
    else:
        out["reason"] = "Reference did not match any USDM node by id or name."
    out["candidates"] = _top_candidates(entity, signal, idx, limit=3)
    return out


def _top_candidates(
    entity: str, signal: str, idx: UsdmIndex, *, limit: int = 3
) -> List[Dict[str, str]]:
    """Return up to ``limit`` plausible USDM nodes of the claimed type."""
    names = idx._names_by_type.get(entity, [])
    if not names:
        return []
    lower_sig = _normalize_for_substring(signal)
    if lower_sig:
        sig_tokens = {t for t in _WORD_SPLIT_RE.split(lower_sig) if len(t) > 2}
    else:
        sig_tokens = set()

    scored: List[Tuple[int, str, str, Dict[str, Any]]] = []
    for low, original, node in names:
        node_tokens = {t for t in _WORD_SPLIT_RE.split(low) if len(t) > 2}
        score = len(sig_tokens & node_tokens)
        scored.append((score, original, low, node))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    out: List[Dict[str, str]] = []
    for score, original, _low, node in scored[:limit]:
        nid = ""
        for f in _ID_FIELDS:
            if node.get(f):
                nid = str(node[f])
                break
        out.append({"id": nid, "name": original[:120]})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Whole-risk / whole-factor iteration helpers
# ─────────────────────────────────────────────────────────────────────────────

def iter_risk_references(risk: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(field_path, ref_dict)`` pairs for every USDM-backed claim on
    a single risk object: ``usdm_drivers[*]`` and
    ``associated_causes[*].usdm_trigger``.
    """
    for k, v in enumerate(risk.get("usdm_drivers") or []):
        if isinstance(v, dict):
            yield f"usdm_drivers[{k}]", v
    for k, cause in enumerate(risk.get("associated_causes") or []):
        if not isinstance(cause, dict):
            continue
        trig = cause.get("usdm_trigger")
        if isinstance(trig, dict):
            yield f"associated_causes[{k}].usdm_trigger", trig


def iter_factor_references(
    factor: Dict[str, Any],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(field_path, ref_dict)`` pairs for every USDM-backed claim on
    a single critical_factor object: ``usdm_sources.critical_data[*]`` and
    ``usdm_sources.critical_process[*]``.
    """
    sources = factor.get("usdm_sources")
    if not isinstance(sources, dict):
        return
    for bucket in ("critical_data", "critical_process"):
        items = sources.get(bucket)
        # The generator sometimes emits a dict instead of a list of dicts.
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for k, entry in enumerate(items):
            if isinstance(entry, dict):
                yield f"usdm_sources.{bucket}[{k}]", entry


__all__ = [
    "UsdmIndex",
    "build_usdm_index",
    "collect_factor_trace_issues",
    "collect_risk_trace_issues",
    "iter_factor_references",
    "iter_risk_references",
    "load_usdm",
    "trace_reference",
    "TRACE_STATUS_ID_MATCH",
    "TRACE_STATUS_NAME_MATCH",
    "TRACE_STATUS_NO_USDM",
    "TRACE_STATUS_TYPE_ONLY",
    "TRACE_STATUS_UNRESOLVED",
]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrappers used by compute_hallucination_detection
# ─────────────────────────────────────────────────────────────────────────────

def collect_risk_trace_issues(
    risk: Dict[str, Any], idx: Optional[UsdmIndex]
) -> List[Dict[str, Any]]:
    """Return one issue dict per unresolved USDM reference on a risk (M4 penalises these only).

    ``TRACE_STATUS_TYPE_ONLY`` is informational only: declared ``instanceType`` is
    present in the protocol JSON with no asserting id — same acceptance rule as
    PIPD protocol traceability; it is **not** a provenance defect.
    """
    issues: List[Dict[str, Any]] = []
    for field_path, ref in iter_risk_references(risk):
        trace = trace_reference(ref, idx)
        if trace["status"] == TRACE_STATUS_UNRESOLVED:
            issues.append({"field_path": field_path, **trace})
    return issues


def collect_factor_trace_issues(
    factor: Dict[str, Any], idx: Optional[UsdmIndex]
) -> List[Dict[str, Any]]:
    """Return one issue dict per unresolved reference on a critical factor (see collect_risk_trace_issues)."""
    issues: List[Dict[str, Any]] = []
    for field_path, ref in iter_factor_references(factor):
        trace = trace_reference(ref, idx)
        if trace["status"] == TRACE_STATUS_UNRESOLVED:
            issues.append({"field_path": field_path, **trace})
    return issues
