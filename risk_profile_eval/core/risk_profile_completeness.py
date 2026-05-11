"""
Structural completeness checks for D1 Risk Profile JSON (vs expected IRMS-style shape).

Does not compare to ground-truth CSV values; flags missing sections/fields the eval
and provenance rules expect. Run:

  python risk_profile_completeness.py path/to/B7981027_RiskProfile.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# Aligns with eval_scenario1.load_generator_json and PROVENANCE_RULES
REQUIRED_TOP_LEVEL = ("metadata", "risks", "critical_factors")

RECOMMENDED_TOP_LEVEL = (
    "general_information",
    "study_overview",
    "risk_profile_summary",
    "vendor_risks",
    "study_site_risks",
    "other_domain_risks",
)

RISK_RECOMMENDED_KEYS = (
    "risk_id",
    "risk_name",
    "risk_domain",
    "risk_description",
    "risk_status",
    "rpn",
    "impact",
    "likelihood",
    "detectability",
    "intelligence",
    "usdm_drivers",
    "associated_causes",
    "controls",
)

FACTOR_RECOMMENDED_KEYS = (
    "number",
    "factor_name",
    "critical_data",
    "critical_process",
    "usdm_sources",
)


def _push(
    errors: List[str],
    warnings: List[str],
    severity: str,
    msg: str,
) -> None:
    if severity == "error":
        errors.append(msg)
    else:
        warnings.append(msg)


def _truthy(obj: Any) -> bool:
    if obj is None:
        return False
    if isinstance(obj, (list, dict)) and len(obj) == 0:
        return False
    if isinstance(obj, str) and not obj.strip():
        return False
    return True


def check_risk_profile_json(data: Dict[str, Any], path_label: str = "") -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    prefix = f"{path_label}: " if path_label else ""

    if not isinstance(data, dict):
        return {
            "ok": False,
            "errors": [f"{prefix}Root must be a JSON object"],
            "warnings": [],
        }

    for key in REQUIRED_TOP_LEVEL:
        if key not in data:
            _push(errors, warnings, "error", f"{prefix}Missing required top-level key '{key}'")

    for key in RECOMMENDED_TOP_LEVEL:
        if key not in data:
            _push(errors, warnings, "warning", f"{prefix}Missing recommended top-level key '{key}'")

    risks = data.get("risks")
    if isinstance(risks, list):
        for i, risk in enumerate(risks):
            if not isinstance(risk, dict):
                _push(errors, warnings, "error", f"{prefix}risks[{i}] is not an object")
                continue
            rp = f"risks[{i}]"
            for k in RISK_RECOMMENDED_KEYS:
                if k not in risk:
                    _push(errors, warnings, "warning", f"{prefix}{rp} missing '{k}'")
            intel = risk.get("intelligence")
            if isinstance(intel, dict):
                if not _truthy(intel.get("benchmark_source")):
                    _push(
                        errors,
                        warnings,
                        "warning",
                        f"{prefix}{rp}.intelligence.benchmark_source empty (M4 provenance)",
                    )
            elif risk.get("risk_name"):
                _push(errors, warnings, "warning", f"{prefix}{rp} missing intelligence object")

            if not _truthy(risk.get("usdm_drivers")):
                _push(errors, warnings, "warning", f"{prefix}{rp}.usdm_drivers empty or missing")

            causes = risk.get("associated_causes")
            if isinstance(causes, list):
                for j, c in enumerate(causes):
                    if isinstance(c, dict) and not _truthy(c.get("usdm_trigger")):
                        _push(
                            errors,
                            warnings,
                            "warning",
                            f"{prefix}{rp}.associated_causes[{j}] missing usdm_trigger",
                        )

    factors = data.get("critical_factors")
    if isinstance(factors, list):
        for i, fac in enumerate(factors):
            if not isinstance(fac, dict):
                _push(errors, warnings, "error", f"{prefix}critical_factors[{i}] is not an object")
                continue
            fp = f"critical_factors[{i}]"
            for k in FACTOR_RECOMMENDED_KEYS:
                if k not in fac:
                    _push(errors, warnings, "warning", f"{prefix}{fp} missing '{k}'")
            usdm = fac.get("usdm_sources")
            if not _truthy(usdm):
                _push(errors, warnings, "warning", f"{prefix}{fp}.usdm_sources empty (M4 provenance)")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Risk Profile JSON structural completeness")
    p.add_argument("json_path", help="Path to *_RiskProfile.json")
    args = p.parse_args()
    path = Path(args.json_path)
    if not path.is_file():
        print(json.dumps({"ok": False, "errors": [f"File not found: {path}"], "warnings": []}))
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    out = check_risk_profile_json(data, path_label=str(path))
    print(json.dumps(out, indent=2))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
