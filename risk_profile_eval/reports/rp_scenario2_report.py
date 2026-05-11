"""
rp_scenario2_report.py
----------------------
Risk-Profile Scenario 2 (no ground truth) — reference-style Markdown + Word report.

Mirrors the layout of ``C5091017_rp_eval_report.docx``:

  1. Study metadata
  2. Overall Quality Verdict (RAG pill + signal-health %)
  3. Signal Scorecard (S1…S7)
  4. Risk Name Benchmark · TA & Phase Presence  (from risk_profile_ground_truth.csv)
  5. Risk Detail Breakdown + LOW confidence line
  6. Hallucination Flags
  7. RPN Formula Verification
  8. Critical Factor Benchmark · TA & Phase Presence (from critical_factors_ground_truth.csv)
  9. Per-Risk USDM Traceability (driver refs resolved against the uploaded USDM)
 10. Critical Factors — USDM Sources
 11. Needs Human Review — N LOW Confidence Risks

Signal-health score is weighted PASS=1, WARN=0.5, FAIL=0, scaled to 0–100, and
exposed so the UI can render a hero "Overall score" tile alongside the traffic-
light verdict (matches the PIPD Scenario 2 convention).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from reports.markdown_to_docx import write_docx_from_markdown
from core.risk_usdm_tracing import (
    build_usdm_index,
    iter_factor_references,
    iter_risk_references,
    load_usdm,
    trace_reference,
)


# ─────────────────────────────────────────────────────────────────────────────
# Signal-health score
# ─────────────────────────────────────────────────────────────────────────────

_SIGNAL_WEIGHTS = {"PASS": 1.0, "WARN": 0.5, "FAIL": 0.0}


def compute_signal_health(signals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Weighted signal-health score over S1…S7 (PASS=1 · WARN=0.5 · FAIL=0)."""
    if not signals:
        return {"percent": 0.0, "pass": 0, "warn": 0, "fail": 0, "total": 0}
    pass_n = warn_n = fail_n = 0
    weighted = 0.0
    for blk in signals.values():
        st = str(blk.get("status") or "").upper()
        if st == "PASS":
            pass_n += 1
        elif st == "WARN":
            warn_n += 1
        elif st == "FAIL":
            fail_n += 1
        weighted += _SIGNAL_WEIGHTS.get(st, 0.0)
    total = pass_n + warn_n + fail_n
    pct = (weighted / total * 100.0) if total else 0.0
    return {
        "percent": round(pct, 1),
        "pass": pass_n,
        "warn": warn_n,
        "fail": fail_n,
        "total": total,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase / TA normalisation
# ─────────────────────────────────────────────────────────────────────────────

_PHASE_ALIASES = {
    "1": "PHASE I",   "I": "PHASE I",   "PH1": "PHASE I",   "PHASE1": "PHASE I",   "PHASEI": "PHASE I",
    "2": "PHASE II",  "II": "PHASE II", "PH2": "PHASE II",  "PHASE2": "PHASE II",  "PHASEII": "PHASE II",
    "3": "PHASE III", "III": "PHASE III", "PH3": "PHASE III", "PHASE3": "PHASE III", "PHASEIII": "PHASE III",
    "4": "PHASE IV",  "IV": "PHASE IV", "PH4": "PHASE IV",  "PHASE4": "PHASE IV",  "PHASEIV": "PHASE IV",
}


def _normalize_phase(p: Optional[str]) -> str:
    """Canonicalise 'Phase 3' / 'III' / 'ph3' → 'PHASE III' for CSV lookups."""
    if not p:
        return ""
    s = re.sub(r"\s+", "", str(p).strip().upper())
    if s in _PHASE_ALIASES:
        return _PHASE_ALIASES[s]
    # 'PHASE III' already normalised; keep last roman/arabic token
    if s.startswith("PHASE"):
        tail = s.replace("PHASE", "")
        return _PHASE_ALIASES.get(tail, f"PHASE {tail}") if tail else ""
    return s


def _normalize_ta(t: Optional[str]) -> str:
    if not t:
        return ""
    return re.sub(r"\s+", " ", str(t).strip().upper())


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark library helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_csv(path: Optional[str]) -> pd.DataFrame:
    if not path or not Path(path).is_file():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame()
    for col in ("ta", "phase", "study_id", "risk_name", "critical_factor_name"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    if "ta" in df.columns:
        df["_ta_norm"] = df["ta"].map(_normalize_ta)
    if "phase" in df.columns:
        df["_phase_norm"] = df["phase"].map(_normalize_phase)
    return df


def _slice_studies(
    df: pd.DataFrame,
    *,
    ta: Optional[str] = None,
    phase: Optional[str] = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if ta:
        tn = _normalize_ta(ta)
        if "_ta_norm" in out.columns:
            out = out[out["_ta_norm"] == tn]
        else:
            out = out.iloc[0:0]
    if phase:
        pn = _normalize_phase(phase)
        if "_phase_norm" in out.columns:
            out = out[out["_phase_norm"] == pn]
        else:
            out = out.iloc[0:0]
    return out


def _unique_studies(df: pd.DataFrame) -> int:
    if df.empty or "study_id" not in df.columns:
        return 0
    return int(df["study_id"].replace("", pd.NA).dropna().nunique())


def _presence_slice(
    df: pd.DataFrame,
    name_col: str,
    name: str,
    *,
    ta: Optional[str] = None,
    phase: Optional[str] = None,
) -> Optional[Tuple[int, int, float]]:
    """Return (studies_with_name, total_studies_in_slice, rate) or None if slice empty."""
    if df.empty or name_col not in df.columns:
        return None
    sliced = _slice_studies(df, ta=ta, phase=phase)
    total = _unique_studies(sliced)
    if total == 0:
        return None
    hits = _unique_studies(sliced[sliced[name_col].astype(str).str.strip().str.lower()
                                  == str(name).strip().lower()])
    return hits, total, (hits / total if total else 0.0)


def _fmt_slice(slc: Optional[Tuple[int, int, float]]) -> str:
    if slc is None:
        return "Novel — N/A"
    n, m, _ = slc
    return f"{n}/{m} / {n / m * 100:.0f}%" if m else "Novel — N/A"


def _all_benchmark_names(df: pd.DataFrame, name_col: str) -> List[str]:
    if df.empty or name_col not in df.columns:
        return []
    seen = set()
    out: List[str] = []
    for v in df[name_col].astype(str):
        s = v.strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return sorted(out, key=str.lower)


# ─────────────────────────────────────────────────────────────────────────────
# USDM provenance for per-risk table
# ─────────────────────────────────────────────────────────────────────────────

def _trace_risk_refs(risk: Dict[str, Any], idx) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path, ref in iter_risk_references(risk):
        r = trace_reference(ref, idx)
        r["field_path"] = path
        out.append(r)
    return out


def _trace_factor_refs(factor: Dict[str, Any], idx) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path, ref in iter_factor_references(factor):
        r = trace_reference(ref, idx)
        r["field_path"] = path
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Payload builder
# ─────────────────────────────────────────────────────────────────────────────

def build_scenario2_payload(
    s2: Dict[str, Any],
    generator_json_path: str,
    risks_gt_csv: Optional[str],
    factors_gt_csv: Optional[str],
    study_id: str,
    *,
    usdm_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Bundle all data the Scenario 2 markdown/Word renderer needs."""
    try:
        with open(generator_json_path, encoding="utf-8") as fh:
            gen = json.load(fh)
    except (OSError, json.JSONDecodeError):
        gen = {}

    overview = gen.get("study_overview") or {}
    meta = gen.get("metadata") or {}
    ta = _normalize_ta(overview.get("therapeutic_area") or s2.get("ta"))
    phase = _normalize_phase(overview.get("development_phase") or s2.get("phase"))

    df_risks = _load_csv(risks_gt_csv)
    df_factors = _load_csv(factors_gt_csv)

    # USDM index for per-risk traceability
    usdm_root = load_usdm(usdm_json_path)
    idx = build_usdm_index(usdm_root) if usdm_root else None
    usdm_loaded = bool(idx is not None and not getattr(idx, "empty", True))

    # Collect ALL domain risks for full hierarchy display
    study_risks  = gen.get("risks") or []
    vendor_risks = gen.get("vendor_risks") or []
    site_risks   = gen.get("study_site_risks") or []
    # Annotate with domain label if missing (vendor / site risks often lack risk_domain)
    for r in study_risks:
        if not r.get("risk_domain"):
            r["risk_domain"] = "Study Risks"
    for r in vendor_risks:
        if not r.get("risk_domain"):
            r["risk_domain"] = "Vendor Risks"
    for r in site_risks:
        if not r.get("risk_domain"):
            r["risk_domain"] = "Study Site Risks"
    risks_by_domain = {
        "Study Risks":      study_risks,
        "Vendor Risks":     vendor_risks,
        "Study Site Risks": site_risks,
    }
    all_risks = study_risks + vendor_risks + site_risks

    risks = study_risks   # keep for backward-compat with signals (S1–S7 only scan study_risks)
    factors = gen.get("critical_factors") or []

    # Per-risk traces used in §9 — include ALL domain risks
    per_risk_traces = []
    for r in all_risks:
        per_risk_traces.append({
            "risk": r,
            "traces": _trace_risk_refs(r, idx) if idx is not None else [],
        })

    # Per-factor traces used in §10
    per_factor_traces = []
    for f in factors:
        per_factor_traces.append({
            "factor": f,
            "traces": _trace_factor_refs(f, idx) if idx is not None else [],
        })

    # Signal health
    signals = s2.get("signals") or {}
    health = compute_signal_health(signals)

    # Training studies count for this TA/Phase slice (used in cold-start banner)
    training_studies = _unique_studies(_slice_studies(df_risks, ta=ta, phase=phase)) if not df_risks.empty else 0

    return {
        "study_id": study_id,
        "scenario": 2,
        "eval_date": (s2.get("timestamp") or datetime.utcnow().isoformat())[:10],
        "metadata": {
            "protocol_id":    overview.get("protocol_id") or meta.get("protocol_id") or study_id,
            "protocol_title": overview.get("title") or meta.get("protocol_title"),
            "study_drug":     overview.get("compound_code") or meta.get("study_drug"),
            "indication":     overview.get("indication"),
            "ta":             ta or "UNKNOWN",
            "phase":          phase or "UNKNOWN",
            "generated_by":   "Protocol Digitalization Platform — USDM 4.0",
        },
        "signal_health": health,
        "verdict": str(s2.get("verdict") or "").upper() or "RED",
        "signals": signals,
        "risks": risks,
        "all_risks": all_risks,
        "risks_by_domain": risks_by_domain,
        "critical_factors": factors,
        "per_risk_traces": per_risk_traces,
        "per_factor_traces": per_factor_traces,
        "risks_benchmark_names": _all_benchmark_names(df_risks, "risk_name"),
        "factors_benchmark_names": _all_benchmark_names(df_factors, "critical_factor_name"),
        "df_risks": df_risks,
        "df_factors": df_factors,
        "ta": ta,
        "phase": phase,
        "training_studies_for_slice": training_studies,
        "usdm_loaded": usdm_loaded,
        "review_list": s2.get("review_list") or [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────

def _md_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    def _row(cells):
        out = [str(c).replace("\n", " ").replace("|", "\\|") for c in cells]
        out += [""] * (width - len(out))
        return "| " + " | ".join(out) + " |"
    lines = [_row(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        lines.append(_row(r))
    return "\n".join(lines) + "\n"


_VERDICT_MSG = {
    "GREEN": "All proxy signals pass. Ready for submission pending standard human review.",
    "AMBER": "Warnings present. Review flagged items before submission.",
    "RED":   "Failures present. Remediate before submission.",
}


def _verdict_line(verdict: str, health: Dict[str, Any], review_count: int) -> str:
    pct = health.get("percent", 0.0)
    msg = _VERDICT_MSG.get(verdict, "")
    body = (
        f"**{verdict or '—'}** — {msg}  \n"
        f"**Signal health:** {pct}% "
        f"({health.get('pass', 0)} PASS · {health.get('warn', 0)} WARN · {health.get('fail', 0)} FAIL, "
        "weighted PASS=1 · WARN=0.5 · FAIL=0)"
    )
    if review_count:
        body += f"  \n**Risks requiring human review:** {review_count}"
    return body


def build_scenario2_markdown(payload: Dict[str, Any]) -> str:
    meta = payload["metadata"]
    ta = meta["ta"]
    phase = meta["phase"]
    risks = payload["risks"]                   # study_risks only (used by S1–S7 signals)
    all_risks = payload.get("all_risks", risks)  # all domains combined
    risks_by_domain = payload.get("risks_by_domain", {"Study Risks": risks})
    factors = payload["critical_factors"]
    health = payload["signal_health"]
    signals = payload["signals"]
    df_risks = payload["df_risks"]
    df_factors = payload["df_factors"]

    review_count = len(payload["review_list"])

    lines: List[str] = []
    lines.append("# Risk Profile — Scenario 2 Evaluation Report")
    lines.append("_No Ground Truth · Proxy Quality Signals · USDM Traceability_\n")

    # ── 1. Metadata ───────────────────────────────────────────────────────────
    lines.append("## Study metadata\n")
    lines.append(_md_table([
        ["Field", "Value"],
        ["Protocol ID",    str(meta["protocol_id"])],
        ["Protocol Title", str(meta.get("protocol_title") or "—")],
        ["Study Drug",     str(meta.get("study_drug") or "—")],
        ["Indication",     str(meta.get("indication") or "—")],
        ["TA / Phase",     f"{ta}  ·  {phase}"],
        ["Risks Generated",
         f"{len(all_risks)} total  "
         f"({len(risks_by_domain.get('Study Risks',[]))} Study  ·  "
         f"{len(risks_by_domain.get('Vendor Risks',[]))} Vendor  ·  "
         f"{len(risks_by_domain.get('Study Site Risks',[]))} Study Site)  ·  "
         f"{len(factors)} critical factors"],
        ["Generated By",   meta.get("generated_by") or "—"],
        ["Eval Date",      payload.get("eval_date") or datetime.now().strftime("%Y-%m-%d")],
    ]))

    # ── 2. Overall verdict ────────────────────────────────────────────────────
    lines.append("## 2  Overall Quality Verdict\n")
    lines.append(_verdict_line(payload["verdict"], health, review_count) + "\n")
    go = payload["verdict"] in ("GREEN", "AMBER")
    lines.append(f"**GO / NO-GO:** {'GO' if go else 'NO-GO'}\n")

    # ── 2b. Score breakdown ───────────────────────────────────────────────────
    _SIG_SHORT_RP = {
        "S1": "S1 — Hallucination Check (USDM drivers + benchmark_source present)",
        "S2": "S2 — RPN Confidence Distribution (< 40% LOW confidence)",
        "S3": "S3 — Risk Count Sanity (3–6 risks expected)",
        "S4": "S4 — USDM Traceability (all risks have FULL traceability)",
        "S5": "S5 — Critical Factor Completeness (expected factors present)",
        "S6": "S6 — Placeholder ID Presence (no real IRMS IDs leaked)",
        "S7": "S7 — RPN Formula Integrity (I × L × D = RPN for all risks)",
    }
    _W = {"PASS": 1.0, "WARN": 0.5, "FAIL": 0.0}
    total_sigs = len(signals)
    running = 0.0
    lines.append("### How the Score Was Calculated\n")
    lines.append(
        f"Score = sum of check points ÷ {total_sigs} checks × 100  "
        f"(PASS=1.0 pt · WARN=0.5 pt · FAIL=0.0 pt)\n"
    )
    sc_rows: List[List[str]] = [["#", "Quality Check", "Result", "Points"]]
    for idx, (sk, sv) in enumerate(signals.items(), 1):
        st = str(sv.get("status") or "").upper()
        pts = _W.get(st, 0.0)
        running += pts
        label = _SIG_SHORT_RP.get(sk, sk)
        msg = str(sv.get("message") or "")[:80]
        sc_rows.append([str(idx), f"**{label}**  \n_{msg}_", st, f"{pts:.1f} / 1.0"])
    sc_rows.append(["", f"**TOTAL  ·  {running:.1f} ÷ {total_sigs} × 100**", "",
                    f"**= {round(running / total_sigs * 100, 1)}%**"])
    lines.append(_md_table(sc_rows))
    lines.append(
        "_History note: Sections 4 and 8 below show each generated risk / critical factor against "
        "historical benchmark data (TA/Phase presence). Items not seen historically are flagged for review._\n"
    )

    # ── 3. Signal scorecard ───────────────────────────────────────────────────
    lines.append("## 3  Signal Scorecard\n")
    ts = payload.get("training_studies_for_slice", 0)
    if ts == 0 and ta and phase:
        lines.append(
            f"_{ta} {phase} has 0 training studies — LOW confidence expected for novel TA/phase_\n"
        )
    sc_rows: List[List[str]] = [["Signal", "What It Checks", "Status"]]
    sig_order = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    titles = {
        "S1": "S1 — Hallucination Check",
        "S2": "S2 — RPN Confidence Distribution",
        "S3": "S3 — Risk Count Sanity",
        "S4": "S4 — USDM Traceability",
        "S5": "S5 — Critical Factor Completeness",
        "S6": "S6 — Placeholder ID Presence",
        "S7": "S7 — RPN Formula Integrity",
    }
    descs = {
        "S1": "Every risk must have non-empty usdm_drivers AND non-null benchmark_source.",
        "S2": ">40% LOW confidence risks triggers WARN.",
        "S3": "Total risks must be 3–6 (floor=3, ceiling=6).",
        "S4": "Every risk must have usdm_traceability = FULL.",
        "S5": "Expected critical factors for TA/Phase must all be present.",
        "S6": "All risk_ids must be SR-PLACEHOLDER-XXX. No real IRMS IDs.",
        "S7": "impact × likelihood × detectability must equal rpn for every risk.",
    }
    for sid in sig_order:
        blk = signals.get(sid) or {}
        sc_rows.append([titles[sid], descs[sid], str(blk.get("status") or "—")])
    lines.append(_md_table(sc_rows))

    # ── 4. Risk Name Benchmark ────────────────────────────────────────────────
    lines.append("## 4  Risk Name Benchmark · TA & Phase Presence\n")
    lines.append(
        f"_TA slice: {ta} (all phases)  ·  Phase slice: {phase} (all TAs)  ·  "
        "Green ≥70%  Amber 40–69%  Red <40%  Grey = Novel TA/Phase_\n"
    )
    lines.append("_Bold blue risk names = generated by this study. All others = in benchmark library but not selected._\n")
    generated_risk_names = {str(r.get("risk_name", "")).strip().lower() for r in risks}
    all_names = payload["risks_benchmark_names"] or sorted(generated_risk_names)
    bench_rows: List[List[str]] = [["Risk Name", "Slice", ta or "TA", phase or "Ph", "Generated"]]
    for name in all_names:
        gen_flag = "✓ Generated" if name.strip().lower() in generated_risk_names else "—"
        ta_slc = _presence_slice(df_risks, "risk_name", name, ta=ta) if ta else None
        ph_slc = _presence_slice(df_risks, "risk_name", name, phase=phase) if phase else None
        bench_rows.append([name, "TA", _fmt_slice(ta_slc), "", gen_flag])
        bench_rows.append([name, "Ph", "", _fmt_slice(ph_slc), gen_flag])
    lines.append(_md_table(bench_rows))

    # ── 5. Domain Verification (all 3 domains) ───────────────────────────────
    lines.append("## 5  Domain Verification — Domains → Risks → Controls\n")
    lines.append(
        f"_Total {len(all_risks)} risks across {sum(1 for v in risks_by_domain.values() if v)} domain(s).  "
        "Each risk shows its RPN, confidence, and number of controls generated._\n"
    )
    REQUIRED_FIELDS = ["risk_id", "risk_name", "rpn", "risk_status", "risk_domain"]
    for domain_label, domain_risks in risks_by_domain.items():
        if not domain_risks:
            continue
        lines.append(f"### {domain_label}  ({len(domain_risks)} risks)\n")
        # Combined domain table: risk summary + field check in one place (no separate repeat)
        dh_rows: List[List[str]] = [
            ["Risk ID", "Risk Name", "RPN", "I", "L", "D", "Conf", "Controls #",
             "Evidence", "Fields OK?"],
        ]
        for r in domain_risks:
            intel = r.get("intelligence") or {}
            rname = str(r.get("risk_name") or "")
            controls = r.get("controls") or []
            ta_slc = _presence_slice(df_risks, "risk_name", rname, ta=ta) if (ta and not df_risks.empty) else None
            ph_slc = _presence_slice(df_risks, "risk_name", rname, phase=phase) if (phase and not df_risks.empty) else None
            gl_slc = _presence_slice(df_risks, "risk_name", rname) if not df_risks.empty else None
            has_usdm = bool(r.get("usdm_drivers"))
            has_hist = (ta_slc and ta_slc[0] > 0) or (ph_slc and ph_slc[0] > 0) or (gl_slc and gl_slc[0] > 0)
            ev = ("Confirmed" if has_usdm and has_hist
                  else "History-supported" if has_hist
                  else "USDM-only" if has_usdm
                  else "No Evidence")
            # Field completeness check
            missing = [f for f in REQUIRED_FIELDS if r.get(f) in (None, "", [], {})]
            fields_ok = "✓ All present" if not missing else f"MISSING: {', '.join(missing)}"
            i_v = r.get("impact", "—"); l_v = r.get("likelihood", "—"); d_v = r.get("detectability", "—")
            dh_rows.append([
                str(r.get("risk_id") or "—"),
                rname[:45] or "—",
                str(r.get("rpn", "—")),
                str(i_v), str(l_v), str(d_v),
                str(intel.get("rpn_confidence") or r.get("confidence") or "—").upper(),
                str(len(controls)),
                ev,
                fields_ok,
            ])
        lines.append(_md_table(dh_rows))
        # Controls sub-table for risks that have controls
        for r in domain_risks:
            controls = r.get("controls") or []
            if not controls:
                continue
            rname = str(r.get("risk_name") or r.get("risk_id") or "")
            lines.append(f"#### Controls — {rname[:60]}\n")
            cr: List[List[str]] = [["Control ID", "Type", "Owner", "Status", "Description"]]
            for c in controls:
                if not isinstance(c, dict):
                    continue
                cr.append([
                    str(c.get("control_id") or "—"),
                    str(c.get("control_type") or "—"),
                    str(c.get("control_owner") or "—"),
                    str(c.get("status") or "—"),
                    str(c.get("control_description") or "—")[:120],
                ])
            lines.append(_md_table(cr))
    lines.append(
        "_Evidence Status: **Confirmed** = USDM drivers + seen historically · "
        "**History-supported** = historical only · **USDM-only** = USDM driven but novel · "
        "**No Evidence** = neither source confirms this risk_\n"
    )

    # ── 6. Hallucination Flags ────────────────────────────────────────────────
    lines.append("## 6  Hallucination Flags\n")
    s1 = signals.get("S1") or {}
    violations = s1.get("violations") or []
    if not violations:
        lines.append("No hallucinations detected. All risks have valid usdm_drivers and benchmark_source.\n")
    else:
        lines.append(f"{len(violations)} provenance/benchmark defect(s) detected. Remediate before submission.\n")
        hr: List[List[str]] = [["Field path", "Value", "Reason"]]
        for v in violations[:40]:
            hr.append([
                str(v.get("field_path") or "")[:140],
                str(v.get("value"))[:80],
                str(v.get("reason") or "")[:160],
            ])
        lines.append(_md_table(hr))

    # ── 7. RPN Formula Verification ───────────────────────────────────────────
    lines.append("## 7  RPN Formula Verification\n")
    lines.append(
        "_Verifying: RPN = Impact × Likelihood × Detectability for Study Risks.  "
        "Vendor and Study Site risks store a combined RPN without I/L/D breakdown — shown as N/A._\n"
    )
    s7 = signals.get("S7") or {}
    mismatches_by_name = {m.get("risk_name"): m for m in (s7.get("mismatches") or [])}
    rpn_rows: List[List[str]] = [
        ["Risk ID", "Risk Name", "Domain", "I", "L", "D", "I×L×D", "RPN", "Check"],
    ]
    for r in all_risks:
        name   = r.get("risk_name", "—")
        domain = str(r.get("risk_domain") or "Study Risks")
        i_val  = r.get("impact")
        l_val  = r.get("likelihood")
        d_val  = r.get("detectability")
        rpn    = r.get("rpn", "—")
        if i_val is not None and l_val is not None and d_val is not None:
            try:
                computed = int(i_val) * int(l_val) * int(d_val)
                mismatch = mismatches_by_name.get(name)
                check = "FAIL ✗" if mismatch else ("PASS ✓" if computed == rpn else "MISMATCH ✗")
            except (TypeError, ValueError):
                computed = "—"; check = "N/A"
        else:
            computed = "N/A"; check = "N/A (no I/L/D)"
        rpn_rows.append([
            str(r.get("risk_id", "—")),
            str(name)[:50],
            domain.replace(" Risks", ""),
            str(i_val) if i_val is not None else "—",
            str(l_val) if l_val is not None else "—",
            str(d_val) if d_val is not None else "—",
            str(computed),
            str(rpn),
            check,
        ])
    lines.append(_md_table(rpn_rows))

    # ── 8. Critical Factor Benchmark ──────────────────────────────────────────
    lines.append("## 8  Critical Factor Benchmark · TA & Phase Presence\n")
    lines.append(f"_TA = {ta} (all phases)  ·  Phase = {phase} (all TAs)_\n")
    lines.append("_Red factor names = expected for this TA/Phase but not generated._\n")
    generated_factor_names = {
        str(f.get("factor_name", "")).strip().lower() for f in factors
    }
    all_factors = payload["factors_benchmark_names"] or sorted(generated_factor_names)
    cf_rows: List[List[str]] = [
        ["Critical Factor", "Slice", ta or "TA", phase or "Ph", "Global", "Generated"],
    ]
    for name in all_factors:
        gen_flag = "✓" if name.strip().lower() in generated_factor_names else "—"
        ta_slc = _presence_slice(df_factors, "critical_factor_name", name, ta=ta) if ta else None
        ph_slc = _presence_slice(df_factors, "critical_factor_name", name, phase=phase) if phase else None
        gl_slc = _presence_slice(df_factors, "critical_factor_name", name)
        cf_rows.append([name, "TA", _fmt_slice(ta_slc), "", _fmt_slice(gl_slc), gen_flag])
        cf_rows.append([name, "Ph", "", _fmt_slice(ph_slc), _fmt_slice(gl_slc), gen_flag])
    lines.append(_md_table(cf_rows))

    # ── 9. Per-Risk USDM Traceability ─────────────────────────────────────────
    lines.append("## 9  Per-Risk USDM Traceability\n")
    lines.append(
        "_For each generated risk: additional context, USDM drivers, associated causes, and controls._\n"
    )
    for prt in payload["per_risk_traces"]:
        r = prt["risk"]
        intel = r.get("intelligence") or {}
        rid = r.get("risk_id", "—")
        rname = r.get("risk_name", "—")
        rpn = r.get("rpn", "—")
        domain = r.get("risk_domain", "Study Risks")
        lines.append(f"### {rid}  {rname}  ·  RPN {rpn}  ·  {domain}\n")
        # Context / confidence strip
        ctx = str(r.get("additional_context") or r.get("risk_description") or "").strip()[:360]
        conf = str(intel.get("rpn_confidence") or "—").upper()
        bench_src = intel.get("benchmark_source") or "Benchmark — not set"
        lines.append(_md_table([
            [ctx or "—", f"Confidence: {conf}", f"Benchmark: {bench_src}"],
        ]))
        # Drivers + causes
        drivers = r.get("usdm_drivers") or []
        drv_lines = []
        trace_by_path = {t.get("field_path"): t for t in prt["traces"]}
        for k, d in enumerate(drivers):
            if not isinstance(d, dict):
                continue
            t = trace_by_path.get(f"usdm_drivers[{k}]") or {}
            tag = f" [{t.get('status')}]" if t.get("status") else ""
            drv_lines.append(f"{d.get('entity', '—')}: {d.get('signal', '—')}{tag}")
        causes = r.get("associated_causes") or []
        cause_lines = []
        for k, c in enumerate(causes):
            if not isinstance(c, dict):
                continue
            trig = c.get("usdm_trigger") or {}
            t = trace_by_path.get(f"associated_causes[{k}].usdm_trigger") or {}
            tag = f" [{t.get('status')}]" if t.get("status") else ""
            cause_lines.append(
                f"{c.get('cause', '—')}  →  {trig.get('entity', '—')}: {trig.get('signal', '—')}{tag}"
            )
        lines.append(_md_table([
            ["USDM Drivers (Why this risk was selected)", "Associated Causes → USDM Trigger"],
            ["; ".join(drv_lines) or "—", "; ".join(cause_lines) or "—"],
        ]))
        # Controls
        controls = r.get("controls") or []
        if controls:
            cr: List[List[str]] = [["Control ID", "Control Type", "Description"]]
            for c in controls:
                cr.append([
                    str(c.get("control_id", "—")),
                    str(c.get("control_type", "—")),
                    str(c.get("control_description", "—"))[:220],
                ])
            lines.append(_md_table(cr))

    # ── 10. Critical Factors — USDM Sources ──────────────────────────────────
    lines.append("## 10  Critical Factors — USDM Sources\n")
    cfs_rows: List[List[str]] = [
        ["Factor", "Critical Data", "Critical Process", "USDM Sources"],
    ]
    for pft in payload["per_factor_traces"]:
        f = pft["factor"]
        traces = pft["traces"]
        src_strs = []
        for t in traces:
            entity = t.get("entity") or ""
            sig = t.get("signal") or ""
            status = t.get("status") or ""
            tag = f" [{status}]" if status else ""
            src_strs.append(f"{entity}: {sig}{tag}")
        cfs_rows.append([
            str(f.get("factor_name", "—")),
            str(f.get("critical_data", "—"))[:260],
            str(f.get("critical_process", "—"))[:260],
            "; ".join(src_strs)[:260] or "—",
        ])
    lines.append(_md_table(cfs_rows))

    # ── 11. Needs Human Review ───────────────────────────────────────────────
    low_risks = payload["review_list"] or []
    lines.append(f"## 11  Needs Human Review — {len(low_risks)} Low Confidence Risks\n")
    if not low_risks:
        lines.append("_No LOW confidence risks generated for this study._\n")
    else:
        lines.append(
            "_All LOW confidence risks require human confirmation before submission. LOW confidence "
            "means the generator had no TA/Phase-specific training data for this risk._\n"
        )
        lr_rows: List[List[str]] = [
            ["Risk ID", "Risk Name", "RPN", "USDM Drivers (basis for inclusion)"],
        ]
        # Build a quick risk_name → risk lookup for driver text
        by_name = {r.get("risk_name"): r for r in risks}
        for item in low_risks:
            name = item.get("risk_name") or ""
            r = by_name.get(name) or {}
            drv = r.get("usdm_drivers") or []
            drv_text = "; ".join(
                f"{d.get('entity', '')}: {d.get('signal', '')}"
                for d in drv if isinstance(d, dict)
            )[:220]
            lr_rows.append([
                str(r.get("risk_id", "—")),
                str(name),
                str(item.get("rpn", r.get("rpn", "—"))),
                drv_text or "—",
            ])
        lines.append(_md_table(lr_rows))

    lines.append("\n---\n")
    lines.append(
        f"_Protocol Digitalization Platform — USDM 4.0  ·  "
        f"{payload.get('eval_date') or datetime.now().strftime('%Y-%m-%d')}  ·  CONFIDENTIAL_\n"
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# YAML + DOCX writers
# ─────────────────────────────────────────────────────────────────────────────

def _write_yaml(payload: Dict[str, Any], path: Path) -> None:
    try:
        import yaml  # type: ignore
    except ImportError:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # DataFrames and pandas objects don't serialise; strip them.
    cleaned = {
        k: v for k, v in payload.items()
        if k not in ("df_risks", "df_factors", "per_risk_traces", "per_factor_traces")
    }
    path.write_text(
        yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def write_scenario2_report(
    payload: Dict[str, Any],
    docx_path: str,
) -> str:
    """Render the Scenario 2 markdown + write the DOCX file. Returns the final docx path."""
    md = build_scenario2_markdown(payload)
    dp = Path(docx_path)
    dp.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_docx_from_markdown(md, str(dp))
    except PermissionError:
        alt = dp.parent / f"{dp.stem}_{datetime.now():%Y%m%d_%H%M%S}{dp.suffix}"
        write_docx_from_markdown(md, str(alt))
        return str(alt.resolve())
    return str(dp.resolve())
