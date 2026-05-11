"""
PIPD Scenario 2 — reference-style Markdown + Word report.

Scenario 2 has no ground truth, so instead of an accuracy score we render:
  • Overall Quality Verdict (GREEN / AMBER / RED) + weighted signal-health %
  • 7-signal scorecard (S1…S7)
  • Category benchmark grid (TA slice + Phase slice, with "Novel — N/A" fallback
    when the benchmark CSV has no rows for the study's TA)
  • Per-category USDM traceability (Subcategory → USDM Entity → Protocol
    Source → Context)
  • Needs Human Review list grouped by category

Per product feedback we intentionally omit the "Include in CSR" column that
appeared in the reference docx (``C5091017_eval_report.docx``); generator
``include_in_csr`` / ``rationale_if_no`` fields are still preserved in JSON.

Layout mirrors ``reference_spec/`` / the C5091017 reference DOCX so reviewers
can swap a Scenario 1 study out for a Scenario 2 study without re-training
their eyes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.eval_scenario2 import (
    FAIL,
    PASS,
    WARN,
    extract_all_subcategories,
    _build_scenario2_usdm_provenance,
)
from utils.pipd_eval_config import category_weights_and_names, load_pipd_eval_config
from reports.pipd_markdown_to_docx import write_docx_from_markdown


# ─────────────────────────────────────────────────────────────────────────────
# Signal health score (verdict companion)
# ─────────────────────────────────────────────────────────────────────────────

_SIGNAL_WEIGHTS = {PASS: 1.0, WARN: 0.5, FAIL: 0.0}

_SIGNAL_TITLES = {
    "S1_HALLUCINATION_CHECK":        "S1 — Hallucination Check",
    "S2_CONFIDENCE_DISTRIBUTION":    "S2 — Confidence Distribution",
    "S3_CATEGORY_COMPLETENESS":      "S3 — Category Completeness",
    "S4_PROTOCOL_SPECIFICITY_CAT10": "S4 — Cat 10 Protocol Specificity",
    "S5_BENCHMARK_ALIGNMENT":        "S5 — Benchmark Alignment",
    "S6_SUBCAT_COUNT_SANITY":        "S6 — Subcategory Count Sanity",
    "S7_NONE_IDENTIFIED_PLAUSIBILITY": "S7 — None-Identified Plausibility",
}

_SIGNAL_DESCRIPTIONS = {
    "S1_HALLUCINATION_CHECK":        "Checks for missing USDM provenance (usdm_entity_id) on auto_confirmed subcategories.",
    "S2_CONFIDENCE_DISTRIBUTION":    "Warns when >50% of subcategories in any category are low_confidence.",
    "S3_CATEGORY_COMPLETENESS":      "All 11 PIPD categories (1–11) must be present in the output.",
    "S4_PROTOCOL_SPECIFICITY_CAT10": "Category 10 (protocol-specific) subcats must NEVER be auto_confirmed; human review always required.",
    "S5_BENCHMARK_ALIGNMENT":        "auto_confirmed subcats must align with historical segment rate ≥ 70%.",
    "S6_SUBCAT_COUNT_SANITY":        "Per-category count must be < 3× historical average (flags over-generation). Uses global avg of 5.0 when no TA/Phase match.",
    "S7_NONE_IDENTIFIED_PLAUSIBILITY": "none_identified=true must be plausible given historical occurrence rates.",
}

_SIGNAL_NOTES = {
    "S1_HALLUCINATION_CHECK": (
        "NOTE: Flags are driven by missing usdm_entity_id (always null in current generator output), "
        "not by a missing usdm_entity class. This is a generator gap, not confirmed hallucination. "
        "Review flagged items semantically — if usdm_entity class is present and protocol_source is "
        "populated the subcategory is likely valid."
    ),
    "S4_PROTOCOL_SPECIFICITY_CAT10": (
        "NOTE: This signal only checks Category 10. Other categories are not covered here; "
        "use S2 (Confidence Distribution) for broader confidence monitoring."
    ),
    "S6_SUBCAT_COUNT_SANITY": (
        "NOTE: Expected average falls back to global 5.0 when no matching TA/Phase segment exists "
        "in the deviation_subcategories.csv benchmark (e.g. Unknown TA or novel phase). "
        "High counts in well-defined categories (e.g. Cat 1 I/E criteria) are often clinically correct."
    ),
}


def compute_signal_health(signals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Weighted signal-health score over the 7 proxy signals.

    PASS=1.0, WARN=0.5, FAIL=0.0; scaled to 0–100.
    """
    if not signals:
        return {"percent": 0.0, "pass": 0, "warn": 0, "fail": 0, "total": 0}
    pass_n = warn_n = fail_n = 0
    weighted = 0.0
    for blk in signals.values():
        status = str(blk.get("status") or "").upper()
        if status == PASS:
            pass_n += 1
        elif status == WARN:
            warn_n += 1
        elif status == FAIL:
            fail_n += 1
        weighted += _SIGNAL_WEIGHTS.get(status, 0.0)
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
# Benchmark (segment-rate) helpers
# ─────────────────────────────────────────────────────────────────────────────

_PHASE_ALIASES = {
    "1": "I", "PH1": "I", "PHI": "I", "PHASE1": "I", "PHASE I": "I",
    "2": "II", "PH2": "II", "PHII": "II", "PHASE2": "II",
    "3": "III", "PH3": "III", "PHIII": "III", "PHASE3": "III",
    "4": "IV", "PH4": "IV", "PHIV": "IV", "PHASE4": "IV",
}


def _normalize_phase(p: Optional[str]) -> str:
    """Normalize a phase string into canonical 'I'/'II'/'III'/'IV' for CSV lookup."""
    if not p:
        return ""
    s = re.sub(r"[^A-Za-z0-9]+", "", str(p).strip().upper())
    if s in _PHASE_ALIASES:
        return _PHASE_ALIASES[s]
    if s.endswith("III"):
        return "III"
    if s.endswith("II"):
        return "II"
    if s.endswith("IV"):
        return "IV"
    if s.endswith("I"):
        return "I"
    return s


_CATEGORY_FROM_CSV_RE = re.compile(r"^\s*(\d{1,2})\s*[\)\.]", re.MULTILINE)


def _csv_category_num(pd_category: str) -> Optional[int]:
    """Parse '1) Inc/Excl' → 1 from the deviation CSV 'pd_category' column."""
    if not pd_category:
        return None
    m = _CATEGORY_FROM_CSV_RE.match(str(pd_category))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _load_benchmark_csv(path: Optional[str]) -> pd.DataFrame:
    if not path or not Path(path).is_file():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return pd.DataFrame()
    if "pd_category" in df.columns:
        df["_cat_num"] = df["pd_category"].map(_csv_category_num)
    if "phase" in df.columns:
        df["_phase_norm"] = df["phase"].map(_normalize_phase)
    for col in ("study_folder", "therapeutic_area"):
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    return df


def _unique_studies_with_category(df: pd.DataFrame, cat_num: int) -> int:
    if df.empty or "_cat_num" not in df.columns:
        return 0
    sub = df[df["_cat_num"] == cat_num]
    if "study_folder" not in sub.columns:
        return 0
    return int(sub["study_folder"].replace("", pd.NA).dropna().nunique())


def _unique_studies_total(df: pd.DataFrame) -> int:
    if df.empty or "study_folder" not in df.columns:
        return 0
    return int(df["study_folder"].replace("", pd.NA).dropna().nunique())


def _slice_studies(df: pd.DataFrame, *, ta: Optional[str], phase_norm: Optional[str]) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if ta:
        if "therapeutic_area" not in out.columns:
            return df.iloc[0:0]
        ta_norm = ta.strip().lower()
        out = out[out["therapeutic_area"].str.strip().str.lower() == ta_norm]
    if phase_norm is not None:
        if "_phase_norm" not in out.columns:
            return df.iloc[0:0]
        out = out[out["_phase_norm"] == phase_norm]
    return out


def _benchmark_slice(
    df: pd.DataFrame,
    cat_num: int,
    *,
    ta: Optional[str] = None,
    phase: Optional[str] = None,
) -> Optional[Tuple[int, int, float]]:
    """
    Returns (studies_with_cat, total_studies_in_slice, rate) or ``None`` if the
    slice is empty (e.g. Novel TA).
    """
    phase_norm = _normalize_phase(phase) if phase else None
    sliced = _slice_studies(df, ta=ta, phase_norm=phase_norm)
    total = _unique_studies_total(sliced)
    if total == 0:
        return None
    present = _unique_studies_with_category(sliced, cat_num)
    return present, total, (present / total if total else 0.0)


def _benchmark_label(slc: Optional[Tuple[int, int, float]]) -> str:
    if slc is None:
        return "Novel — N/A"
    n, m, _ = slc
    return f"{n}/{m}  {n / m * 100:.0f}%" if m else "Novel — N/A"


def _benchmark_rag(rate: Optional[float]) -> str:
    if rate is None:
        return "Grey"
    if rate >= 0.70:
        return "Green"
    if rate >= 0.40:
        return "Amber"
    return "Red"


# ─────────────────────────────────────────────────────────────────────────────
# USDM tracing row → markdown context
# ─────────────────────────────────────────────────────────────────────────────

_LEX_SCORE_RE = re.compile(r"_\(?(?:weak\s+)?lex\s+([\d.]+)\)?_", re.IGNORECASE)
_LEX_REVIEW_THRESHOLD = 0.50   # below this → Low Match, flag for human review


def _extract_lex_score(prov_row: Optional[Dict[str, Any]]) -> Optional[float]:
    """Pull the numeric lex score out of usdm_protocol_source text, or return None."""
    if not prov_row:
        return None
    src = str(prov_row.get("usdm_protocol_source") or "")
    m = _LEX_SCORE_RE.search(src)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _provenance_status(
    entity: str,
    src: str,
    lex: Optional[float],
    hist_rate: Optional[float] = None,
) -> str:
    """Return combined evidence status from USDM match quality AND historical data.

    Both signals are needed:
    - USDM match tells us if the protocol *contains* a related element
    - History tells us if this type of deviation was *actually done* in practice

    Status labels:
    Confirmed        – USDM match OK (lex ≥ threshold) AND historically seen (rate > 0)
    Protocol-only    – USDM match OK but never seen historically (rate = 0 or unknown)
    History-supported– No strong USDM match but historically seen in other studies
    Low Match        – USDM match weak (lex < threshold) AND not historically confirmed
    Inferred         – USDM class assigned but no element match found
    No Evidence      – No USDM entity AND no historical support
    """
    usdm_ok = bool(entity) and src not in ("—", "") and (lex is None or lex >= _LEX_REVIEW_THRESHOLD)
    usdm_weak = bool(entity) and (lex is not None and lex < _LEX_REVIEW_THRESHOLD)
    has_history = hist_rate is not None and hist_rate > 0.0

    if not entity:
        return "History-supported" if has_history else "No Evidence"
    if usdm_weak:
        return "History-supported" if has_history else f"Low Match (lex {lex:.2f})"
    if src in ("—", ""):
        return "History-supported" if has_history else "Inferred"
    # usdm_ok = True
    if has_history:
        return "Confirmed"
    return "Protocol-only"


def _context_for_subcat(sub: Dict[str, Any], prov_row: Optional[Dict[str, Any]]) -> str:
    """Assemble a compact 'Why generated' string for the traceability table."""
    src = str(sub.get("protocol_source") or "").strip()
    entity = str(sub.get("usdm_entity") or "").strip()
    if prov_row:
        line = prov_row.get("usdm_protocol_source")
        if line:
            return str(line)[:260]
    if src and entity:
        return f"{entity}: {src}"
    if src:
        return src
    return "—"


def _protocol_source(sub: Dict[str, Any], prov_row: Optional[Dict[str, Any]]) -> str:
    s = str(sub.get("protocol_source") or "").strip()
    if s:
        return s
    if prov_row:
        uid = prov_row.get("usdm_entity_id")
        if uid:
            return f"id {uid}"
    return "—"


# ─────────────────────────────────────────────────────────────────────────────
# Payload + markdown builders
# ─────────────────────────────────────────────────────────────────────────────

def _generator_meta(generator_json_path: str) -> Dict[str, Any]:
    try:
        with open(generator_json_path, encoding="utf-8") as fh:
            g = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "protocol_id":    g.get("protocol_id") or g.get("study_id"),
        "protocol_title": g.get("protocol_title") or g.get("title"),
        "study_drug":     g.get("study_drug") or g.get("drug"),
        "therapeutic_area": g.get("therapeutic_area") or g.get("ta"),
        "phase":          g.get("phase"),
        "generated_by":   g.get("generated_by"),
        "version":        g.get("version"),
        "version_date":   g.get("version_date"),
    }


def _build_category_history(df: pd.DataFrame) -> Dict[int, Dict[str, Any]]:
    """Build per-category historical context from the benchmark CSV.

    Returns dict keyed by category_num with:
        - presence_pct: % of studies that have this category
        - avg_count: average number of subcategory rows per study
        - common_subcategories: top 5 most-seen subcategory texts with their occurrence counts
        - n_studies: total studies in the benchmark
    """
    if df.empty:
        return {}

    import re as _re
    def _cat_num(s):
        m = _re.match(r"(\d+)", str(s))
        return int(m.group(1)) if m else 0

    col_cat = None
    for c in ("pd_category", "category_num", "category"):
        if c in df.columns:
            col_cat = c
            break
    if not col_cat:
        return {}

    col_study = None
    for c in ("study_folder", "study_id", "study"):
        if c in df.columns:
            col_study = c
            break
    if not col_study:
        return {}

    col_subcat = None
    for c in ("pd_subcategory", "subcategory_text", "subcategory"):
        if c in df.columns:
            col_subcat = c
            break

    df = df.copy()
    df["_cn"] = df[col_cat].apply(_cat_num)
    n_studies = df[col_study].nunique()
    if n_studies == 0:
        return {}

    result: Dict[int, Dict[str, Any]] = {}
    for cn in sorted(df["_cn"].unique()):
        if cn == 0:
            continue
        cat_df = df[df["_cn"] == cn]
        presence_pct = round(cat_df[col_study].nunique() / n_studies * 100, 0)
        avg_count = round(cat_df.groupby(col_study).size().mean(), 1)
        common: List[str] = []
        if col_subcat:
            top = cat_df[col_subcat].dropna().value_counts().head(5)
            for txt, cnt in top.items():
                common.append(f"{txt} [{cnt}/{n_studies} studies]")
        result[int(cn)] = {
            "presence_pct": float(presence_pct),
            "avg_count":    float(avg_count),
            "common_subcategories": [str(c) for c in common],
            "n_studies":    int(n_studies),
        }
    return result


def build_scenario2_report_payload(
    s2: Dict[str, Any],
    generator_json_path: str,
    deviation_benchmarks_path: str,
    study_id: str,
    *,
    usdm_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the dict that drives Markdown + YAML rendering for Scenario 2."""
    meta = _generator_meta(generator_json_path)
    signals = s2.get("signals") or {}
    health = compute_signal_health(signals)
    overall_verdict = s2.get("overall_verdict") or {}
    verdict = overall_verdict.get("verdict") if isinstance(overall_verdict, dict) else str(overall_verdict)

    df_bench = _load_benchmark_csv(deviation_benchmarks_path)
    ta = meta.get("therapeutic_area")
    phase = meta.get("phase")

    # Prefer USDM provenance already present on results; otherwise build now.
    usdm_prov = s2.get("usdm_provenance")
    if not usdm_prov or not usdm_prov.get("loaded"):
        try:
            with open(generator_json_path, encoding="utf-8") as fh:
                gen = json.load(fh)
            all_subs = extract_all_subcategories(gen)
            usdm_prov = _build_scenario2_usdm_provenance(all_subs, study_id, usdm_json_path)
        except Exception:
            usdm_prov = {"loaded": False, "subcategories": []}

    prov_rows = {}
    for row in (usdm_prov or {}).get("subcategories") or []:
        key = (row.get("category_num"), str(row.get("subcategory_text") or "").strip())
        prov_rows[key] = row

    _, cat_names = category_weights_and_names(load_pipd_eval_config())

    # Build augmented human review list: base (low/review confidence) + S1 unclear items + S6 over-count
    base_review = list(s2.get("human_review_list") or [])
    base_keys = {(int(r.get("category_num") or 0), str(r.get("subcategory_text") or "").strip())
                 for r in base_review}

    # S1: add items where usdm_entity is null/empty (genuinely unclear provenance, not just missing ID)
    s1_flagged = (signals.get("S1_HALLUCINATION_CHECK") or {}).get("flagged") or []
    for item in s1_flagged:
        entity = str(item.get("usdm_entity") or "").strip()
        src    = str(item.get("protocol_source") or "").strip()
        rate   = float(item.get("benchmark_rate") or 0.0)
        key    = (int(item.get("category_num") or 0), str(item.get("subcategory_text") or "").strip())
        if key in base_keys:
            continue
        # Unclear context: no entity class AND no protocol source
        if not entity and not src:
            base_review.append({
                "category_num":    item.get("category_num"),
                "subcategory_text": item.get("subcategory_text"),
                "confidence":      "low_confidence",
                "review_reason":   "S1: no usdm_entity and no protocol_source — context completely unclear",
            })
            base_keys.add(key)
        # Low benchmark rate — rarely seen historically
        elif rate == 0.0:
            base_review.append({
                "category_num":    item.get("category_num"),
                "subcategory_text": item.get("subcategory_text"),
                "confidence":      "low_confidence",
                "review_reason":   f"S1: benchmark rate 0% for this segment — historically uncommon",
            })
            base_keys.add(key)

    # Part 2: low USDM match quality — lex score < threshold
    for row in (usdm_prov or {}).get("subcategories") or []:
        lex = _extract_lex_score(row)
        if lex is not None and lex < _LEX_REVIEW_THRESHOLD:
            key = (int(row.get("category_num") or 0), str(row.get("subcategory_text") or "").strip())
            if key not in base_keys:
                src_ctx = str(row.get("usdm_protocol_source") or "—")[:120]
                base_review.append({
                    "category_num":     row.get("category_num"),
                    "subcategory_text": row.get("subcategory_text"),
                    "confidence":       str(row.get("confidence") or "auto_confirmed"),
                    "review_reason":    (
                        f"Weak USDM match (lex {lex:.2f} < {_LEX_REVIEW_THRESHOLD}) — "
                        f"the closest protocol element may not be genuinely related. "
                        f"Closest: {src_ctx}"
                    ),
                })
                base_keys.add(key)

    # Part 3: "Protocol-only" — has a USDM match but NEVER seen historically (all_studies rate = 0)
    # Being in the protocol does not mean the deviation actually occurred — history confirms that.
    try:
        with open(generator_json_path, encoding="utf-8") as _fh:
            _gen_raw = json.load(_fh)
    except Exception:
        _gen_raw = {}
    for _cat in (_gen_raw.get("categories") or []):
        _cn = int(_cat.get("category_num") or 0)
        for _sub in (_cat.get("subcategories") or []):
            _txt = str(_sub.get("subcategory_text") or "").strip()
            _key = (_cn, _txt)
            if _key in base_keys:
                continue
            _all_rate = None
            try:
                _all_rate = float(_sub.get("benchmark", {}).get("all_studies", {}).get("rate") or 0.0)
            except (TypeError, ValueError):
                pass
            _entity = str(_sub.get("usdm_entity") or "").strip()
            # Flag if has a USDM class (so it's in the protocol) but no historical occurrence
            if _entity and _all_rate is not None and _all_rate == 0.0:
                base_review.append({
                    "category_num":     _cn,
                    "subcategory_text": _txt,
                    "confidence":       str(_sub.get("confidence") or "auto_confirmed"),
                    "review_reason":    (
                        "Found in protocol (USDM) but never recorded as a deviation in any historical study. "
                        "Confirm this deviation type is applicable to this trial."
                    ),
                })
                base_keys.add(_key)

    # S6: only add to review when real segment data is available (not global fallback)
    s6_blk_aug = signals.get("S6_SUBCAT_COUNT_SANITY") or {}
    s6_per_cat = s6_blk_aug.get("per_category") or {}
    avgs = [v.get("expected_avg") for v in s6_per_cat.values() if v.get("expected_avg")]
    s6_is_fallback = len(set(avgs)) <= 1  # all categories got the same avg → fallback mode
    if not s6_is_fallback:
        s6_warnings_aug = s6_blk_aug.get("warnings") or []
        for w in s6_warnings_aug:
            cn    = int(w.get("category_num") or 0)
            gen_n = w.get("generated_count", 0)
            avg_n = w.get("expected_avg", 0)
            ratio = w.get("ratio", 0.0)
            cat_name = cat_names.get(cn, f"Category {cn}")
            base_review.append({
                "category_num":    cn,
                "subcategory_text": f"[CATEGORY LEVEL] {cat_name} — {gen_n} subcats generated vs avg {avg_n}",
                "confidence":      "review",
                "review_reason":   f"S6: {gen_n} subcats generated vs avg {avg_n} (ratio {ratio:.1f}×) — check for duplicates or over-splitting",
            })

    return {
        "study_id": study_id,
        "scenario": 2,
        "eval_date": s2.get("eval_date") or datetime.now().isoformat(),
        "metadata": meta,
        "signal_health": health,
        "verdict": verdict,
        "signals": signals,
        "per_category_confidence": (signals.get("S2_CONFIDENCE_DISTRIBUTION") or {}).get("per_category") or {},
        "per_category_counts":     (signals.get("S6_SUBCAT_COUNT_SANITY") or {}).get("per_category") or {},
        "remediation_notes":       (overall_verdict.get("remediation_notes") if isinstance(overall_verdict, dict) else None) or [],
        "human_review_list":       base_review,
        "categories":              _collect_categories(generator_json_path, cat_names),
        "benchmarks": {
            "loaded": not df_bench.empty,
            "ta": ta,
            "phase": phase,
            "rows": _build_benchmark_rows(df_bench, cat_names, ta, phase),
        },
        "category_history": _build_category_history(df_bench),
        "usdm_provenance": usdm_prov,
        "prov_rows": prov_rows,
        "generator_path": str(Path(generator_json_path).resolve()),
        "benchmark_csv_path": str(Path(deviation_benchmarks_path).resolve())
            if deviation_benchmarks_path else "",
    }


def _collect_categories(generator_json_path: str, cat_names: Dict[int, str]) -> List[Dict[str, Any]]:
    """Read categories + subcategories from the generator JSON for the report."""
    try:
        with open(generator_json_path, encoding="utf-8") as fh:
            gen = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    out: List[Dict[str, Any]] = []
    for cat in gen.get("categories") or []:
        try:
            cn = int(cat.get("category_num"))
        except (TypeError, ValueError):
            continue
        out.append({
            "category_num":    cn,
            "category_name":   cat.get("category_name") or cat_names.get(cn, f"Category {cn}"),
            "guidance":        cat.get("guidance") or cat.get("description") or "",
            "none_identified": bool(cat.get("none_identified", False)),
            "subcategories":   list(cat.get("subcategories") or []),
        })
    out.sort(key=lambda c: c["category_num"])
    return out


def _build_benchmark_rows(
    df: pd.DataFrame,
    cat_names: Dict[int, str],
    ta: Optional[str],
    phase: Optional[str],
) -> List[Dict[str, Any]]:
    """One row per category with TA-slice and Phase-slice benchmark stats."""
    rows: List[Dict[str, Any]] = []
    for cn in range(1, 12):
        ta_slc = _benchmark_slice(df, cn, ta=ta) if ta else None
        ph_slc = _benchmark_slice(df, cn, phase=phase) if phase else None
        gl_slc = _benchmark_slice(df, cn)
        rows.append({
            "category_num":  cn,
            "category_name": cat_names.get(cn, f"Category {cn}"),
            "ta_slice":      ta_slc,
            "phase_slice":   ph_slc,
            "global_slice":  gl_slc,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────

def _verdict_line(verdict: str, health: Dict[str, Any]) -> str:
    pill = {"GREEN": "GREEN", "AMBER": "AMBER", "RED": "RED"}.get((verdict or "").upper(), verdict or "—")
    msg = {
        "GREEN": "All proxy signals pass. Ready for submission pending standard human review.",
        "AMBER": "Warnings present. Review flagged items before submission.",
        "RED":   "Failures present. Remediate before submission.",
    }.get(pill, "")
    pct = health.get("percent", 0.0)
    line = (
        f"**{pill}** — {msg}  \n"
        f"**Signal health:** {pct}% "
        f"({health.get('pass', 0)} PASS · {health.get('warn', 0)} WARN · {health.get('fail', 0)} FAIL, "
        "weighted PASS=1 · WARN=0.5 · FAIL=0)"
    )
    return line


def _md_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(rows[0]) + " |"]
    out.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
    for r in rows[1:]:
        cells = [str(c).replace("\n", " ").replace("|", "\\|") for c in r]
        if len(cells) < len(rows[0]):
            cells += [""] * (len(rows[0]) - len(cells))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def build_scenario2_markdown(payload: Dict[str, Any]) -> str:
    meta = payload.get("metadata") or {}
    sid = payload.get("study_id", "")
    ed = str(payload.get("eval_date") or "")[:10]
    ta_label = meta.get("therapeutic_area") or "Unknown"
    phase_raw = meta.get("phase") or ""
    phase_label = f"Phase {_normalize_phase(phase_raw)}" if phase_raw else "Unknown"

    health = payload.get("signal_health") or {}
    signals = payload.get("signals") or {}
    cats = payload.get("categories") or []
    total_subcats = sum(len(c.get("subcategories") or []) for c in cats)
    prov_rows = payload.get("prov_rows") or {}
    review_list = payload.get("human_review_list") or []
    bench = (payload.get("benchmarks") or {}).get("rows") or []
    cat_history = payload.get("category_history") or {}

    # Compute review counts
    n_review = len(review_list)
    n_ok = total_subcats - n_review

    # ─── Report ──────────────────────────────────────────────────────────────
    lines: List[str] = []
    lines.append("# PIPD Evaluation Report")
    lines.append(f"_Study {sid}  ·  {ed or datetime.now().strftime('%Y-%m-%d')}  ·  CONFIDENTIAL_\n")

    # ── Score box ──
    verdict_upper = (payload.get("verdict") or "").upper()
    go_no_go = "GO" if verdict_upper in ("GREEN", "AMBER") else "NO-GO"
    health_pct = round(health.get("percent", 0.0), 1)
    pass_flag = "1" if go_no_go == "GO" else "0"
    lines.append(
        f"<!-- DOC_SCORE: seg=PIPD Quality Assessment"
        f"|score={health_pct}"
        f"|pass={pass_flag}"
        f"|threshold=62.5"
        f"|target=87.5"
        f"|label={n_ok}/{total_subcats} items verified · {n_review} need review -->"
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 1: SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    lines.append("## 1. Summary\n")
    lines.append(_md_table([
        ["", ""],
        ["Study",             f"{sid} — {str(meta.get('protocol_title') or '')}"],
        ["Drug / Phase",      f"{str(meta.get('study_drug') or '—')}  ·  {phase_label}"],
        ["Total Deviation Subcategories Generated", str(total_subcats)],
        ["Categories Covered", f"{sum(1 for c in cats if c.get('subcategories'))} of 11"],
        ["Items Verified (OK)", f"**{n_ok}**"],
        ["Items Needing Human Review", f"**{n_review}**"],
        ["Overall Quality Score", f"**{health_pct}%**"],
        ["Recommendation",    f"**{go_no_go}** — {'Proceed with standard review' if go_no_go == 'GO' else 'Fix flagged items before proceeding'}"],
    ]))

    # ── Score Breakdown ──────────────────────────────────────────────────────
    _SIG_SHORT = {
        "S1_HALLUCINATION_CHECK":          "S1 — USDM Provenance (entity_id check)",
        "S2_CONFIDENCE_DISTRIBUTION":      "S2 — Confidence Distribution",
        "S3_CATEGORY_COMPLETENESS":        "S3 — All 11 Categories Present",
        "S4_PROTOCOL_SPECIFICITY_CAT10":   "S4 — Cat 10 Not Auto-Confirmed",
        "S5_BENCHMARK_ALIGNMENT":          "S5 — Benchmark Rate ≥ 70%",
        "S6_SUBCAT_COUNT_SANITY":          "S6 — Subcategory Count Sanity",
        "S7_NONE_IDENTIFIED_PLAUSIBILITY": "S7 — Empty Categories Plausible",
    }
    _STATUS_ICON = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}
    _WEIGHT_LABEL = {PASS: "1.0 / 1.0", WARN: "0.5 / 1.0", FAIL: "0.0 / 1.0"}
    total_signals = health.get("total", 7)
    lines.append("### How the Score Was Calculated\n")
    lines.append(
        f"The score is based on **{total_signals} quality checks**. "
        "Each check is scored PASS (1.0 point), WARN (0.5 point), or FAIL (0.0 point). "
        f"Score = total points ÷ {total_signals} checks × 100.\n"
    )
    score_rows: List[List[str]] = [["#", "Quality Check", "Result", "Points"]]
    running_pts = 0.0
    for idx, (sig_key, sig_blk) in enumerate(signals.items(), 1):
        st = str(sig_blk.get("status") or "").upper()
        pts = _SIGNAL_WEIGHTS.get(st, 0.0)
        running_pts += pts
        label = _SIG_SHORT.get(sig_key, sig_key)
        msg = str(sig_blk.get("message") or "")[:80]
        score_rows.append([
            str(idx),
            f"**{label}**  \n_{msg}_",
            _STATUS_ICON.get(st, st),
            _WEIGHT_LABEL.get(st, "—"),
        ])
    score_rows.append([
        "",
        f"**TOTAL  ·  Formula: {running_pts:.1f} ÷ {total_signals} × 100**",
        "",
        f"**= {round(running_pts / total_signals * 100, 1)}%**",
    ])
    lines.append(_md_table(score_rows))
    lines.append(
        "_Note: A FAIL on S1 (USDM entity_id always null in current generator output) is a known "
        "generator gap, not confirmed hallucination. See Section 5 for detail._\n"
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 2: WHAT WAS CHECKED
    # ═══════════════════════════════════════════════════════════════════════════
    lines.append("## 2. What Was Checked\n")
    lines.append(
        "The generator produced a PIPD (Protocol Important Protocol Deviation) form from the study protocol. "
        "Since no manually-created ground truth exists for this study, the evaluator checks the output's "
        "internal quality using two independent evidence sources:\n\n"
        "**Evidence Source 1 — USDM (Unified Study Data Model)**  \n"
        "The protocol is encoded as a structured USDM JSON. The evaluator searches this JSON for the "
        "closest matching element to each deviation subcategory. It finds two things:\n\n"
        "- **USDM Class** (from the generator): The type of protocol element the deviation maps to "
        "(e.g. `EligibilityCriterion`, `Activity`, `StudyIntervention`). The generator assigns this class but "
        "does **not** provide a specific entity ID — so we can confirm the *type* is sensible, but cannot "
        "definitively link to one exact protocol sentence.\n"
        "- **Closest USDM Element** (from the evaluator): The evaluator runs a semantic similarity search "
        "over all USDM entities of that class and finds the nearest one. The match score (lex) ranges from "
        "0.0 (unrelated) to 1.0 (near-identical). A score ≥ 0.5 is considered a reasonable match; below "
        "0.5 is flagged for human review.\n\n"
        "**Evidence Source 2 — Historical Benchmark**  \n"
        "A database of PIPD deviations from ~30 Pfizer studies. It shows how often each category and "
        "subcategory type appears historically, giving reviewers context on whether the generated "
        "deviations are typical or unusual.\n\n"
        "**What the evaluator cannot do:**  \n"
        "It cannot confirm a deviation against the original PDF protocol text directly. "
        "It can only confirm against the USDM data model (structured extract of the protocol). "
        "Items that cannot be anchored in the USDM *or* confirmed by history are flagged for human review.\n\n"
        "**Important:** USDM match alone is NOT sufficient to confirm a deviation. Just because something "
        "appears in the protocol does not mean it is a deviation that actually occurs. The historical benchmark "
        "provides the second check — if this deviation type has never been recorded in practice across ~30 Pfizer "
        "studies, it should be reviewed even if the USDM match is strong.\n\n"
        "For each subcategory, the evaluator asks:\n\n"
        "1. **Is it in the protocol?** — Does the USDM contain a matching entity (class + closest element)?\n"
        "2. **Has it been seen before?** — Does the historical benchmark show this deviation type was recorded in prior studies?\n"
        "3. **Are both signals present?** — Only items with both a strong USDM match AND historical precedent are marked **Confirmed**.\n"
        "4. **Is it complete?** — Are all 11 deviation categories represented?\n"
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 3: ITEMS NEEDING REVIEW (the actionable section, up front)
    # ═══════════════════════════════════════════════════════════════════════════
    lines.append(f"## 3. Items Needing Human Review ({n_review})\n")
    if not review_list:
        lines.append("**All items passed quality checks.** No human review needed beyond standard process.\n")
    else:
        lines.append(
            "The following items could not be confidently verified against the protocol. "
            "A clinical reviewer should confirm or reject each before the PIPD form is finalized.\n"
        )
        by_cat: Dict[int, List[Dict[str, Any]]] = {}
        for item in review_list:
            try:
                cn = int(item.get("category_num"))
            except (TypeError, ValueError):
                continue
            by_cat.setdefault(cn, []).append(item)
        _, cat_names_lookup = category_weights_and_names(load_pipd_eval_config())
        for cn in sorted(by_cat):
            name = cat_names_lookup.get(cn, f"Category {cn}")
            lines.append(f"### Category {cn} — {name}\n")
            rv_rows: List[List[str]] = [["Deviation Subcategory", "Why Flagged", "Action"]]
            for item in by_cat[cn]:
                reason_raw = str(item.get("review_reason") or "")
                # Produce plain-English reason from stored review_reason
                if "Weak USDM match" in reason_raw or "lex" in reason_raw:
                    lex_m = _LEX_SCORE_RE.search(reason_raw)
                    lex_val = f" (score: {lex_m.group(1)})" if lex_m else ""
                    reason = (
                        f"The closest protocol element found is not strongly related{lex_val}. "
                        "The USDM match may be coincidental — verify this deviation against the actual protocol."
                    )
                elif "never recorded" in reason_raw or "Protocol-only" in reason_raw:
                    reason = (
                        "This deviation category exists in the protocol but has never appeared as an actual "
                        "deviation in any historical Pfizer study. Confirm it is truly applicable."
                    )
                elif "no usdm_entity" in reason_raw.lower() or "No USDM class" in reason_raw:
                    reason = (
                        "No protocol element could be found for this deviation — no USDM class assigned "
                        "and no historical precedent. Requires manual confirmation."
                    )
                elif "historically uncommon" in reason_raw or "benchmark rate 0" in reason_raw:
                    reason = (
                        "This type of deviation has never been recorded in historical Pfizer studies for "
                        "this study type. Confirm it genuinely applies here."
                    )
                else:
                    reason = str(reason_raw) or "Generator confidence was low. Confirm this deviation is valid."
                rv_rows.append([
                    str(item.get("subcategory_text") or "")[:200],
                    reason,
                    "Confirm / Reject",
                ])
            lines.append(_md_table(rv_rows))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 4: CATEGORY-BY-CATEGORY DETAIL
    # ═══════════════════════════════════════════════════════════════════════════
    lines.append("## 4. Category-by-Category Detail\n")
    lines.append(
        "For each of the 11 PIPD categories: what was generated, what USDM class it maps to, "
        "the closest matching USDM element found by semantic search, and historical context.\n\n"
        "**Columns:**\n\n"
        "- **USDM Class** — The protocol entity type the generator assigned (e.g. `EligibilityCriterion`). "
        "Confirms the *category* is sensible but does not link to a specific protocol sentence.\n"
        "- **Closest USDM Element** — The most semantically similar element found in the USDM JSON "
        "by the evaluator. Format: `Class · 'text' · id=X (lex Y.YY)`. Higher lex = stronger match. "
        "This comes from the USDM data model, *not* the original PDF protocol.\n"
        "- **Evidence Status** — Combined verdict using BOTH USDM match quality AND historical presence:\n"
        "  - **Confirmed** = Strong USDM element match (lex ≥ 0.50) AND seen in historical studies — fully supported\n"
        "  - **Protocol-only** = Strong USDM match but **never seen historically** — flagged in Section 3 for review\n"
        "  - **History-supported** = Seen in historical studies but USDM match is weak or absent — likely valid, but verify\n"
        "  - **Low Match** = USDM match is weak (lex < 0.50) AND not historically confirmed — flagged in Section 3\n"
        "  - **Inferred** = USDM class assigned but no specific element found and no history — inferred only\n"
        "  - **No Evidence** = No USDM entity AND no historical support — requires human confirmation\n"
    )

    for cat in cats:
        cn = cat["category_num"]
        name = cat["category_name"]
        subs = cat.get("subcategories") or []
        hist = cat_history.get(cn) or {}

        lines.append(f"### {cn}. {name}  ({len(subs)} subcategories)\n")

        # Historical context box
        if hist:
            presence = hist.get("presence_pct", 0)
            avg_count = hist.get("avg_count", 0)
            n_studies = hist.get("n_studies", 0)
            lines.append(
                f"_Historical benchmark ({n_studies} Pfizer studies):_ "
                f"Category present in **{presence:.0f}%** of studies · "
                f"Avg **{avg_count}** subcategories per study.  \n"
                "_Note: history supplements USDM evidence — it does not replace protocol verification._  \n"
            )
            common = hist.get("common_subcategories") or []
            if common:
                lines.append("_Historically common deviations in this category:_\n")
                for c_text in common[:4]:
                    lines.append(f"- {c_text}")
                lines.append("")

        if not subs:
            if cat.get("none_identified"):
                if hist and hist.get("presence_pct", 0) < 50:
                    lines.append(
                        f"_No deviations identified. This is reasonable — "
                        f"only {hist.get('presence_pct', 0):.0f}% of studies historically have this category._\n"
                    )
                else:
                    lines.append("_No deviations identified for this category._\n")
            else:
                lines.append("_No subcategories generated._\n")
            continue

        tr_rows: List[List[str]] = [[
            "#", "Deviation Subcategory", "USDM Class (Evaluator)", "Closest USDM Element + ID", "Evidence Status",
        ]]
        for i, s in enumerate(subs, 1):
            sub_text = str(s.get("subcategory_text") or "").strip()
            prov = prov_rows.get((cn, sub_text))
            # Prefer evaluator-found class/ID over generator-assigned values
            eval_etype = str((prov or {}).get("eval_usdm_entity_type") or "").strip()
            eval_eid   = str((prov or {}).get("eval_usdm_entity_id")   or "").strip()
            gen_entity = str(s.get("usdm_entity") or "").strip()
            # Use evaluator class if found, otherwise show generator class with note
            if eval_etype:
                display_class = eval_etype
                if eval_etype != gen_entity and gen_entity:
                    display_class += f" _(gen: {gen_entity})_"
            else:
                display_class = gen_entity or "—"
            lex    = _extract_lex_score(prov)
            ctx    = _context_for_subcat(s, prov)
            # Append evaluator-found entity ID if available
            ctx_clean = _LEX_SCORE_RE.sub("", ctx).strip().rstrip("_").strip()
            if eval_eid:
                ctx_clean = f"id={eval_eid} · {ctx_clean}" if ctx_clean and ctx_clean != "—" else f"id={eval_eid}"
            # Historical rate: prefer all_studies rate for the broadest signal
            try:
                hist_rate = float(s.get("benchmark", {}).get("all_studies", {}).get("rate") or 0.0)
            except (TypeError, ValueError):
                hist_rate = None
            prov_status = _provenance_status(
                eval_etype or gen_entity,
                str((prov or {}).get("usdm_protocol_source") or ""),
                lex,
                hist_rate,
            )
            tr_rows.append([
                str(i),
                sub_text[:180],
                display_class[:80],
                ctx_clean[:160],
                prov_status,
            ])
        lines.append(_md_table(tr_rows))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 5: QUALITY CHECKS (technical detail for evaluators, collapsed)
    # ═══════════════════════════════════════════════════════════════════════════
    lines.append("## 5. Quality Check Summary (Technical)\n")
    lines.append("_This section provides technical detail on the automated checks run. "
                 "Non-technical readers can skip this section._\n")
    check_rows: List[List[str]] = [["Check", "What it verifies", "Result"]]
    check_items = [
        ("Structure",       "All 11 PIPD categories present in output",
         str((signals.get("S3_CATEGORY_COMPLETENESS") or {}).get("status") or "—").upper()),
        ("USDM Element Match",  "Each subcategory has a USDM element match (lex ≥ 0.50)",
         f"{n_ok} OK, {n_review} need review"),
        ("Category 10",     "Protocol-specific deviations are not auto-confirmed (require human review)",
         str((signals.get("S4_PROTOCOL_SPECIFICITY_CAT10") or {}).get("status") or "—").upper()),
        ("Confidence",      "No category has >50% low-confidence items",
         str((signals.get("S2_CONFIDENCE_DISTRIBUTION") or {}).get("status") or "—").upper()),
        ("Completeness",    "None-identified categories are plausible given study type",
         str((signals.get("S7_NONE_IDENTIFIED_PLAUSIBILITY") or {}).get("status") or "—").upper()),
    ]
    for name, desc, result in check_items:
        check_rows.append([name, desc, result])
    lines.append(_md_table(check_rows))

    lines.append("\n---\n")
    lines.append(
        f"_Protocol Digitalization Platform — USDM 4.0  ·  {ed or datetime.now().strftime('%Y-%m-%d')}  ·  CONFIDENTIAL_\n"
    )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# YAML + DOCX writers
# ─────────────────────────────────────────────────────────────────────────────

def _to_native(obj: Any) -> Any:
    """Recursively convert numpy/pandas types to native Python for YAML serialisation."""
    try:
        import numpy as np  # type: ignore
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {_to_native(k): _to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_native(v) for v in obj]
    return obj


def _write_yaml(payload: Dict[str, Any], path: Path) -> None:
    try:
        import yaml  # type: ignore
    except ImportError:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Strip prov_rows (tuple keys won't serialise); convert numpy types.
    cleaned = _to_native({k: v for k, v in payload.items() if k != "prov_rows"})
    path.write_text(
        yaml.safe_dump(
            cleaned, sort_keys=False, allow_unicode=True, default_flow_style=False
        ),
        encoding="utf-8",
    )


def write_scenario2_yaml_and_word(
    payload: Dict[str, Any],
    output_dir: str,
    study_id: str,
    *,
    artifact_stem: Optional[str] = None,
    write_yaml: bool = True,
    write_docx: bool = True,
) -> Dict[str, str]:
    """Write ``{stem}.yaml`` and ``PIPD_Eval_Report_{study}.docx`` (mirrors Scenario 1 API)."""
    paths: Dict[str, str] = {}
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (artifact_stem or "").strip() or f"pipd_eval_{study_id}"
    yname = f"{stem}.yaml"
    dname = (
        f"{artifact_stem}.docx" if artifact_stem else f"PIPD_Eval_Report_{study_id}.docx"
    )

    if write_yaml:
        try:
            yp = out_dir / yname
            _write_yaml(payload, yp)
            paths["yaml"] = str(yp.resolve())
        except Exception as exc:
            paths["yaml_error"] = str(exc)

    if write_docx:
        md = build_scenario2_markdown(payload)
        dp = out_dir / dname
        try:
            write_docx_from_markdown(md, str(dp), reference_eval=True)
            paths["docx"] = str(dp.resolve())
        except PermissionError:
            alt = out_dir / f"{dp.stem}_{datetime.now():%Y%m%d_%H%M%S}{dp.suffix}"
            write_docx_from_markdown(md, str(alt), reference_eval=True)
            paths["docx"] = str(alt.resolve())
            paths["docx_note"] = "Default .docx locked; wrote timestamped file."
        except Exception as exc:
            paths["docx_error"] = str(exc)

    return paths
