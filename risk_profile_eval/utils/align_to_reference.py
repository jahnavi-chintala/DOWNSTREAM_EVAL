"""
Align eval report dicts to a reference JSON *shape* (key order + nesting).

Values come from the live ``report``; keys only present on the reference keep
structural parity with a reference JSON shape (optional dev tool).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def align_to_reference(ref: Any, data: Any) -> Any:
    """Return a tree with the same keys/nesting as ref; values from data when paths exist.

    Handles JSON int/str key interop: a reference key "1" (string, from json.load) will match
    a live-data key 1 (int, from Python eval code), and vice versa.
    """
    if isinstance(ref, dict):
        src = data if isinstance(data, dict) else {}
        out: dict[str, Any] = {}
        for k, rv in ref.items():
            if k in src:
                sv = src[k]
            elif isinstance(k, str) and k.isdigit() and int(k) in src:
                sv = src[int(k)]
            elif not isinstance(k, str) and str(k) in src:
                sv = src[str(k)]
            else:
                if isinstance(rv, dict):
                    out[k] = align_to_reference(rv, {})
                elif isinstance(rv, list):
                    out[k] = []
                else:
                    out[k] = None
                continue
            out[k] = align_to_reference(rv, sv)
        # Keep fields present only on live data (e.g. m2 ``skipped``, ``skip_reason``)
        for k, sv in src.items():
            if k not in out:
                out[k] = copy.deepcopy(sv)
        return out
    if isinstance(ref, list):
        if not isinstance(data, list):
            return []
        if not ref:
            return copy.deepcopy(data)
        elem_ref = ref[0]
        return [align_to_reference(elem_ref, item) for item in data]
    return copy.deepcopy(data)


def load_and_align(reference_json_path: Path, report: dict) -> dict:
    with open(reference_json_path, encoding="utf-8") as fh:
        ref = json.load(fh)
    return align_to_reference(ref, report)


def try_align(reference_json_path: Path | None, report: dict) -> dict:
    if reference_json_path is None or not reference_json_path.is_file():
        return report
    return load_and_align(reference_json_path, report)
