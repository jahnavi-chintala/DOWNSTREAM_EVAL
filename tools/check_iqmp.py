"""Pretty-print iqmp/asrp linkage fields from a CMP generator JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inspect iqmp/asrp linkage fields inside CMP JSON.")
    parser.add_argument("cmp_json", type=Path, help="Path to CMP generator JSON.")
    args = parser.parse_args(argv)

    with Path(args.cmp_json).open(encoding="utf-8") as handle:
        data = json.load(handle)

    print("=== SS KRIs: iqmp_risk_id vs asrp_risk_ids ===")
    for k in data.get("study_specific_kris", []):
        label = k.get("kri_label", "?")
        iqmp = k.get("iqmp_risk_id")
        asrp = k.get("asrp_risk_ids")
        print(f"  {label}")
        print(f"    iqmp_risk_id  = {repr(iqmp)}")
        print(f"    asrp_risk_ids = {repr(asrp)}")

    print("\n=== Global KRIs: iqmp_risk_id vs asrp_risk_ids (first 5) ===")
    for k in data.get("global_kris", [])[:5]:
        label = k.get("kri_label", k.get("label", "?"))
        iqmp = k.get("iqmp_risk_id")
        asrp = k.get("asrp_risk_ids")
        print(f"  {label}")
        print(f"    iqmp_risk_id  = {repr(iqmp)}")
        print(f"    asrp_risk_ids = {repr(asrp)}")


if __name__ == "__main__":
    main()
