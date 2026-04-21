"""
Build a readable Word document (.docx) from a D1 Risk Profile JSON file.

Usage:
  python risk_profile_json_to_docx.py --input data/B7981027_RiskProfile.json
  python risk_profile_json_to_docx.py --input data/B7981027_RiskProfile.json --output out/custom.docx
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from markdown_to_docx import write_docx_from_markdown


def _scalar_table(rows: List[tuple]) -> str:
    if not rows:
        return ""
    lines = ["| Field | Value |", "| --- | --- |"]
    for k, v in rows:
        vs = "" if v is None else str(v).replace("\n", " ").replace("|", "/")
        lines.append(f"| {k} | {vs} |")
    return "\n".join(lines) + "\n\n"


def _dict_scalars(d: Dict[str, Any], skip: Optional[set] = None) -> List[tuple]:
    skip = skip or set()
    out: List[tuple] = []
    for k in sorted(d.keys()):
        if k in skip:
            continue
        v = d[k]
        if isinstance(v, (dict, list)):
            continue
        out.append((k, v))
    return out


def _json_fence(label: str, obj: Any) -> str:
    body = json.dumps(obj, indent=2, ensure_ascii=False)
    return f"```{label}\n{body}\n```\n\n"


def build_markdown_from_risk_profile(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    meta = data.get("metadata") or {}
    sid = meta.get("study_id") or meta.get("protocol_id") or "Risk profile"
    doc_type = meta.get("document_type") or "Risk Profile"
    parts.append(f"# {doc_type}\n\n")
    parts.append(f"**Study:** `{sid}`\n\n")
    parts.append("## Metadata\n\n")
    parts.append(_scalar_table(_dict_scalars(meta)))

    gi = data.get("general_information")
    if isinstance(gi, dict) and gi:
        parts.append("## General information\n\n")
        parts.append(_scalar_table(_dict_scalars(gi)))

    so = data.get("study_overview")
    if isinstance(so, dict) and so:
        parts.append("## Study overview\n\n")
        usdm = so.get("usdm_sources")
        rows = _dict_scalars(so, skip={"usdm_sources"})
        parts.append(_scalar_table(rows))
        if isinstance(usdm, dict) and usdm:
            parts.append("### USDM sources (study overview)\n\n")
            parts.append(_scalar_table(_dict_scalars(usdm)))

    factors = data.get("critical_factors")
    if isinstance(factors, list) and factors:
        parts.append("## Critical factors\n\n")
        for f in factors:
            if not isinstance(f, dict):
                continue
            num = f.get("number", "")
            name = f.get("factor_name", "")
            parts.append(f"### {num}. {name}\n\n")
            if f.get("critical_data"):
                parts.append(f"- **Critical data:** {f['critical_data']}\n")
            if f.get("critical_process"):
                parts.append(f"- **Critical process:** {f['critical_process']}\n")
            parts.append("\n")
            src = f.get("usdm_sources")
            if isinstance(src, dict) and src:
                parts.append("#### USDM sources\n\n")
                parts.append(_json_fence("json", src))

    summary = data.get("risk_profile_summary")
    if isinstance(summary, dict) and summary:
        parts.append("## Risk profile summary\n\n")
        parts.append(_scalar_table(_dict_scalars(summary)))

    def append_risk_list(title: str, items: Any) -> None:
        if not isinstance(items, list) or not items:
            return
        parts.append(f"## {title}\n\n")
        for r in items:
            if not isinstance(r, dict):
                continue
            rname = r.get("risk_name", "Risk")
            rid = r.get("risk_id", "")
            parts.append(f"### {rname} (`{rid}`)\n\n")
            scalar_keys = (
                "risk_domain",
                "risk_status",
                "rpn",
                "impact",
                "likelihood",
                "detectability",
            )
            for key in scalar_keys:
                if key in r and r[key] is not None and r[key] != "":
                    parts.append(f"- **{key.replace('_', ' ').title()}:** {r[key]}\n")
            if r.get("risk_description"):
                parts.append(f"\n{r['risk_description']}\n\n")
            if r.get("additional_context"):
                parts.append(f"- **Additional context:** {r['additional_context']}\n")
            causes = r.get("associated_causes")
            if isinstance(causes, list) and causes:
                parts.append("\n#### Associated causes\n\n")
                for c in causes:
                    if isinstance(c, dict) and c.get("cause"):
                        parts.append(f"- {c['cause']}\n")
                parts.append("\n")
            linked = r.get("critical_factors_linked")
            if isinstance(linked, list) and linked:
                parts.append(f"- **Critical factors linked:** {', '.join(linked)}\n\n")
            drivers = r.get("usdm_drivers")
            if isinstance(drivers, list) and drivers:
                parts.append("#### USDM drivers\n\n")
                parts.append(_json_fence("json", drivers))
            controls = r.get("controls")
            if isinstance(controls, list) and controls:
                parts.append("#### Controls\n\n")
                for c in controls:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("control_id", "")
                    ctype = c.get("control_type", "")
                    parts.append(f"- **{cid}** — {ctype}\n")
                    if c.get("control_description"):
                        parts.append(f"  - {c['control_description']}\n")
                parts.append("\n")
            intel = r.get("intelligence")
            if isinstance(intel, dict) and intel:
                parts.append("#### Intelligence\n\n")
                parts.append(_scalar_table(_dict_scalars(intel)))
                parts.append("\n")

    append_risk_list("Risks", data.get("risks"))
    append_risk_list("Vendor risks", data.get("vendor_risks"))
    append_risk_list("Study / site risks", data.get("study_site_risks"))
    append_risk_list("Other domain risks", data.get("other_domain_risks"))

    return "".join(parts)


def write_risk_profile_docx(
    json_path: str,
    docx_path: Optional[str] = None,
) -> str:
    path = Path(json_path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Risk profile JSON must be an object at the root.")
    md = build_markdown_from_risk_profile(data)
    out = docx_path
    if not out:
        out = str(path.with_suffix(".docx"))
    write_docx_from_markdown(md, out)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Risk Profile JSON → Word (.docx)")
    p.add_argument("--input", "-i", required=True, help="Path to *_RiskProfile.json")
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output .docx path (default: same name as JSON)",
    )
    args = p.parse_args()
    out = write_risk_profile_docx(args.input, args.output)
    print(out)


if __name__ == "__main__":
    main()
