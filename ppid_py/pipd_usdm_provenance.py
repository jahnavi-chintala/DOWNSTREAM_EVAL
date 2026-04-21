"""
Map PIPD subcategories to human-readable **USDM protocol** locations.

- **id_lookup:** ``usdm_entity_id`` matches an object in the USDM JSON → show
  instanceType, criterion section (e.g. Inclusion/Exclusion), and text snippet.
- **type_match:** no id but ``usdm_entity`` (instanceType) set → best lexical
  match among USDM nodes of that type (Levenshtein ratio).
- **ai_assisted** (optional): if ``PIPD_USDM_SOURCE_AI=1`` and lexical confidence
  is weak, ask OpenAI to pick the best excerpt among top candidates.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from Levenshtein import ratio as lev_ratio

from eval_scenario1 import NULL_PLACEHOLDERS


def _is_null_id(val: Any) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in NULL_PLACEHOLDERS or s == ""


def index_usdm_nodes_by_id(root: Any) -> Dict[str, Dict[str, Any]]:
    """First occurrence wins for each ``id`` string."""
    by_id: Dict[str, Dict[str, Any]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            i = o.get("id")
            if isinstance(i, str) and i.strip():
                key = i.strip()
                if key not in by_id:
                    by_id[key] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(root)
    return by_id


def index_usdm_nodes_by_instance_type(root: Any) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            it = o.get("instanceType")
            if isinstance(it, str) and it.strip():
                out.setdefault(it.strip(), []).append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(root)
    return out


def _node_text_blob(node: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("name", "text", "expandedText", "sectionTitle", "label", "description"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    cat = node.get("category")
    if isinstance(cat, dict):
        dec = cat.get("decode")
        if isinstance(dec, str) and dec.strip():
            parts.append(dec.strip())
    return " ".join(parts)


def format_node_provenance(node: Dict[str, Any], max_snippet: int = 140) -> str:
    """One-line description: type · section · snippet · id."""
    it = str(node.get("instanceType") or "?").strip()
    parts: List[str] = [it]
    cat = node.get("category")
    if isinstance(cat, dict):
        dec = cat.get("decode")
        if isinstance(dec, str) and dec.strip():
            parts.append(dec.strip())
    snippet = ""
    for key in ("sectionTitle", "name", "text", "expandedText"):
        t = node.get(key)
        if isinstance(t, str) and t.strip():
            snippet = t.strip().replace("\n", " ")
            break
    if snippet:
        if len(snippet) > max_snippet:
            snippet = snippet[: max_snippet - 1] + "…"
        parts.append(f'"{snippet}"')
    nid = node.get("id")
    if isinstance(nid, str) and nid.strip():
        parts.append(f"id={nid.strip()}")
    return " · ".join(parts)


def lexical_best_type_match(
    pipd_text: str,
    instance_type: str,
    type_index: Dict[str, List[Dict[str, Any]]],
) -> Optional[Tuple[Dict[str, Any], float]]:
    if not pipd_text.strip() or not instance_type.strip():
        return None
    candidates = type_index.get(instance_type.strip()) or []
    if not candidates:
        return None
    pt = pipd_text.lower().strip()[:800]
    best_node: Optional[Dict[str, Any]] = None
    best_r = -1.0
    for node in candidates:
        blob = _node_text_blob(node).lower()[:4000]
        if not blob.strip():
            continue
        r = lev_ratio(pt, blob)
        if r > best_r:
            best_r = r
            best_node = node
    if best_node is None:
        return None
    return (best_node, best_r)


def _lexical_min_ratio() -> float:
    try:
        return float(os.environ.get("PIPD_USDM_LEXICAL_MIN", "0.18"))
    except ValueError:
        return 0.18


def _openai_pick_candidate(
    pipd_line: str,
    candidates: List[Tuple[int, str]],
) -> Optional[Tuple[int, str]]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    numbered = "\n".join(f"{i}. {txt[:500]}" for i, txt in candidates)
    user = (
        "PIPD deviation line (generator):\n"
        f"{pipd_line[:1200]}\n\n"
        "USDM protocol excerpts (pick the single best supporting source, or say NONE):\n"
        f"{numbered}\n\n"
        'Reply with JSON only: {"choice": <0-based index or -1 if none>, "note": "<12 words>"}'
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You map clinical deviation text to protocol JSON excerpts. JSON only.",
                },
                {"role": "user", "content": user},
            ],
            "max_tokens": 120,
            "temperature": 0.2,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        raw = out["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        j = json.loads(raw)
        idx = int(j.get("choice", -1))
        note = str(j.get("note", "")).strip()
        if idx < 0 or idx >= len(candidates):
            return None
        return (idx, note)
    except Exception:
        return None


def _rank_type_candidates(
    pipd_text: str,
    utype_s: str,
    type_index: Dict[str, List[Dict[str, Any]]],
    top_k: int = 8,
) -> List[Tuple[float, Dict[str, Any]]]:
    candidates_nodes = type_index.get(utype_s) or []
    pt = pipd_text.lower()[:800]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for n in candidates_nodes:
        blob = _node_text_blob(n).lower()
        if not blob.strip():
            continue
        scored.append((lev_ratio(pt, blob[:4000]), n))
    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


def try_ai_usdm_source(
    pipd_text: str,
    utype_s: str,
    type_index: Dict[str, List[Dict[str, Any]]],
) -> Optional[str]:
    top = _rank_type_candidates(pipd_text, utype_s, type_index, top_k=8)
    if not top:
        return None
    numbered = [(i, format_node_provenance(n, max_snippet=200)) for i, (_, n) in enumerate(top)]
    picked = _openai_pick_candidate(pipd_text, numbered)
    if picked is None:
        return None
    ai_idx, note = picked
    if ai_idx < 0 or ai_idx >= len(top):
        return None
    chosen = top[ai_idx][1]
    base = format_node_provenance(chosen)
    if note:
        return f"{base} _(AI: {note})_"
    return base


def resolve_usdm_source_for_subcategory(
    sub: Dict[str, Any],
    pipd_text: str,
    by_id: Dict[str, Dict[str, Any]],
    type_index: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Optional[str], str]:
    """
    Returns (display_line, method) where method is
    id_lookup | id_missing | type_match | ai_assisted | none.
    """
    use_ai = os.environ.get("PIPD_USDM_SOURCE_AI", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    min_lex = _lexical_min_ratio()
    ai_below = float(os.environ.get("PIPD_USDM_AI_BELOW_RATIO", "0.35"))

    uid = sub.get("usdm_entity_id")
    if not _is_null_id(uid):
        node = by_id.get(str(uid).strip())
        if node:
            return format_node_provenance(node), "id_lookup"
        return f"(id `{uid}` not in protocol file)", "id_missing"

    utype = sub.get("usdm_entity")
    utype_s = str(utype).strip() if utype is not None else ""
    if not utype_s:
        return None, "none"

    lex = lexical_best_type_match(pipd_text, utype_s, type_index)
    if not lex:
        return f"(no `{utype_s}` nodes in protocol)", "none"

    node, score = lex
    line = format_node_provenance(node)

    if use_ai and (score < ai_below or score < min_lex):
        ai_line = try_ai_usdm_source(pipd_text, utype_s, type_index)
        if ai_line:
            return ai_line, "ai_assisted"

    if score >= min_lex:
        return f"{line} _(lex {score:.2f})_", "type_match"
    return f"{line} _(weak lex {score:.2f})_", "type_match"
