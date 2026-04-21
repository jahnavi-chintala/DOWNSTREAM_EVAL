"""
Structural completeness checks for PIPD generator JSON (categories / subcategories).

Run:
  python pipd_structure_completeness.py path/to/B7981027_PIPD.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from eval_scenario1 import NUM_CATEGORIES


SUBCAT_RECOMMENDED = (
    "subcategory_text",
    "include_in_csr",
    "confidence",
    "usdm_entity",
    "benchmark",
)


def check_pipd_json(data: Dict[str, Any], path_label: str = "") -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    prefix = f"{path_label}: " if path_label else ""

    if not isinstance(data, dict):
        return {"ok": False, "errors": [f"{prefix}Root must be a JSON object"], "warnings": []}

    cats = data.get("categories")
    if not isinstance(cats, list):
        errors.append(f"{prefix}Missing or invalid 'categories' array")
        return {"ok": False, "errors": errors, "warnings": warnings}

    seen: Set[int] = set()
    for i, cat in enumerate(cats):
        if not isinstance(cat, dict):
            errors.append(f"{prefix}categories[{i}] is not an object")
            continue
        cn = cat.get("category_num")
        if cn is None:
            errors.append(f"{prefix}categories[{i}] missing category_num")
            continue
        try:
            n = int(cn)
        except (TypeError, ValueError):
            errors.append(f"{prefix}categories[{i}] invalid category_num")
            continue
        seen.add(n)
        if "category_name" not in cat:
            warnings.append(f"{prefix}Category {n}: missing category_name")
        if "none_identified" not in cat:
            warnings.append(f"{prefix}Category {n}: missing none_identified")
        subs = cat.get("subcategories")
        if not isinstance(subs, list):
            warnings.append(f"{prefix}Category {n}: subcategories not a list")
            continue
        if not cat.get("none_identified") and len(subs) == 0:
            warnings.append(f"{prefix}Category {n}: empty subcategories but none_identified is false")
        for j, sub in enumerate(subs):
            if not isinstance(sub, dict):
                errors.append(f"{prefix}Category {n} subcategories[{j}] not an object")
                continue
            if not (sub.get("subcategory_text") or "").strip():
                warnings.append(f"{prefix}Category {n} sub[{j}]: empty subcategory_text")
            for key in SUBCAT_RECOMMENDED:
                if key not in sub:
                    warnings.append(f"{prefix}Category {n} sub[{j}]: missing '{key}'")

    expected = set(range(1, NUM_CATEGORIES + 1))
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    if missing:
        warnings.append(f"{prefix}Missing category_num values: {missing}")
    if extra:
        warnings.append(f"{prefix}Unexpected category_num values: {extra}")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}


def main() -> None:
    p = argparse.ArgumentParser(description="PIPD JSON structural completeness")
    p.add_argument("json_path", help="Path to *_PIPD.json")
    args = p.parse_args()
    path = Path(args.json_path)
    if not path.is_file():
        print(json.dumps({"ok": False, "errors": [f"File not found: {path}"], "warnings": []}))
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = check_pipd_json(data, path_label=str(path))
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
