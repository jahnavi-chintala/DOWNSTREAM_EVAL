"""
yaml_builder.py — Build YAML benchmark files from ground truth CSVs
Produces: global_kri_rules.yaml, ss_kri_benchmarks.yaml, qtl_benchmarks.yaml

Usage:
    python yaml_builder.py --kri-csv data/cmp_kri_ground_truth.csv \
                            --qtl-csv data/cmp_qtl_ground_truth.csv \
                            --study-meta data/cmp_study_metadata.csv \
                            --output-dir data/yamls/ \
                            --split train

Split parameter:
    train  — use only training studies (not B7981027 verify study in this context)
    all    — use all studies
"""

import argparse
import os
import re
import sys
from collections import defaultdict

import pandas as pd
import yaml
from scipy import stats as scipy_stats


# ─── TA / Phase Label Normalization ───────────────────────────────────────────

TA_MAP = {
    "i_and_i": ["b7981027", "b7981032", "b7981040", "b7981041", "b7981080", "b7981094", "b7981119",
                "b7981015", "b7981028"],
    "oncology": ["c1071003", "c1071005", "c1071006", "c1071007", "c1071015",
                 "c2321003", "c2321008", "c2321014", "c4221015", "c4221016", "c4221022",
                 "c4891001", "c4891002", "c4891026"],
    "vaccines": ["c3651003", "c3651021", "c3671013", "c3671058", "c3671059",
                 "c4591048", "c4591076", "c4591081", "c4591082", "c4601003", "c5091017"],
}

FREQUENCY_THRESHOLD = 0.25  # Include if present in >=25% of TA/phase studies

PHASE_MAP = {
    "phase_1": ["phase 1", "phase i", "1b"],
    "phase_2": ["phase 2", "phase ii", "phase2"],
    "phase_3": ["phase 3", "phase iii", "phase3"],
}


def infer_ta(study_id: str) -> str:
    sid = study_id.strip().lower()
    for ta, ids in TA_MAP.items():
        if sid in ids:
            return ta
    # Fallback by prefix
    if sid.startswith("b79"):
        return "i_and_i"
    if sid.startswith("c107") or sid.startswith("c232") or sid.startswith("c422") or sid.startswith("c489"):
        return "oncology"
    if sid.startswith("c365") or sid.startswith("c367") or sid.startswith("c459") or sid.startswith("c460") or sid.startswith("c509"):
        return "vaccines"
    return "unknown"


def infer_phase(study_id: str, meta_df: pd.DataFrame = None) -> str:
    if meta_df is not None and not meta_df.empty:
        row = meta_df[meta_df["study_id"].str.strip().str.lower() == study_id.strip().lower()]
        if not row.empty:
            ext = str(row.iloc[0].get("external_data_sources", "")).lower()
            for phase_key, keywords in PHASE_MAP.items():
                if any(kw in ext for kw in keywords):
                    return phase_key
    return "phase_3"


def derive_modal(values: list) -> tuple:
    """Compute modal value and CV. Returns (modal_float_or_None, distribution_label)."""
    clean = []
    for v in values:
        parsed = _parse_num(v)
        if parsed is not None:
            clean.append(parsed)
    if not clean:
        return None, "no_data"
    if len(clean) == 1:
        return clean[0], "single_value"
    try:
        mode_result = scipy_stats.mode(clean, keepdims=True)
        modal = float(mode_result.mode[0])
    except Exception:
        modal = float(pd.Series(clean).mean())
    mean = pd.Series(clean).mean()
    std = pd.Series(clean).std()
    cv = std / mean if mean != 0 else 0
    if cv < 0.15:
        dist = "tight"
    elif cv < 0.35:
        dist = "moderate"
    else:
        dist = "dispersed"
    return modal, dist


def _parse_num(raw) -> float | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    if s.lower() in ("n/a", "null", "none", "", "nan"):
        return None
    m = re.search(r"([\d.]+)", s)
    return float(m.group(1)) if m else None


def most_frequent(series: pd.Series) -> str | None:
    vc = series.dropna().value_counts()
    return vc.index[0] if not vc.empty else None


def standardize_corrective_action(text: str) -> str:
    if not text or pd.isna(text):
        return ""
    text = re.sub(r"B\d{7}|C\d{7}", "{STUDY_ID}", str(text))
    text = re.sub(r"(ritlecitinib|elranatamab|\bpf-\d+)", "{STUDY_DRUG}", text, flags=re.I)
    text = " ".join(text.split())
    return text[:300]


# ─── YAML Builders ────────────────────────────────────────────────────────────

def build_global_kri_rules(df_kri: pd.DataFrame, meta_df: pd.DataFrame) -> dict:
    """Build global_kri_rules.yaml from global KRI rows."""
    df_g = df_kri[df_kri["kri_section"].str.strip().str.lower() == "global"].copy()
    df_g = df_g[~df_g["status"].str.strip().str.lower().isin(["retired", "not_applicable"])]

    standard_globals = [
        "Protocol Deviation Rate", "AE Rate", "SAE Rate",
        "Adverse Event Rate", "Serious Adverse Event Rate",
    ]
    # Also capture ePRO audit trail KRIs
    epro_patterns = ["Open to Open", "Open to Save", "Entry Time"]

    global_kris = []
    seen_ids = set()

    for kri_label_raw in df_g["kri_label"].dropna().unique():
        kri_label = str(kri_label_raw).strip()
        if not kri_label:
            continue

        subset = df_g[df_g["kri_label"].str.strip() == kri_label]
        study_count = subset["study_id"].nunique()
        source_studies = list(subset["study_id"].unique())

        # Determine tier
        if study_count >= 10:
            tier = 1
        elif study_count >= 3:
            tier = 2
        else:
            tier = 3

        mod_val, mod_dist = derive_modal(subset["moderate_threshold"].tolist())
        high_val, high_dist = derive_modal(subset["high_threshold"].tolist())
        if mod_dist == "dispersed":
            tier = min(tier, 2)

        iqmp_id = most_frequent(subset["iqmp_risk_id"])
        weight = most_frequent(subset["weight"]) or "High"
        rel_dir = most_frequent(subset["relative_score_direction"])
        direction = "both"
        if rel_dir == "Above":
            direction = "above"
        elif rel_dir == "Below":
            direction = "below"

        corrective = standardize_corrective_action(most_frequent(subset.get("comment", pd.Series(dtype=str))))

        kri_id = "KRI_" + re.sub(r"[^A-Z0-9]", "_", kri_label.upper())[:40]
        if kri_id in seen_ids:
            continue
        seen_ids.add(kri_id)

        entry = {
            "kri_id": kri_id,
            "kri_label": kri_label,
            "iqmp_risk_id": iqmp_id if iqmp_id and str(iqmp_id) not in ("nan", "None") else None,
            "kri_type": "global",
            "active": True,
            "thresholds": {
                "moderate": {
                    "absolute": mod_val,
                    "relative_score": 1.3,
                },
                "high": {
                    "absolute": high_val,
                    "relative_score": 3.0,
                },
            },
            "threshold_direction": direction,
            "weight": weight,
            "confidence_tier": tier,
            "source_study_count": study_count,
            "source_studies_sample": source_studies[:5],
            "threshold_distribution": mod_dist,
        }
        if corrective:
            entry["corrective_actions"] = corrective

        global_kris.append(entry)

    # Sort by study_count desc
    global_kris.sort(key=lambda x: x["source_study_count"], reverse=True)

    return {
        "metadata": {
            "version": "1.0",
            "source_studies": int(df_kri["study_id"].nunique()),
            "built_from": "cmp_kri_ground_truth.csv",
            "framework": "CluePoints RBM",
        },
        "global_kris": global_kris,
    }


def build_ss_kri_benchmarks(df_kri: pd.DataFrame, meta_df: pd.DataFrame) -> dict:
    """Build ss_kri_benchmarks.yaml — grouped by TA/phase."""
    df_ss = df_kri[df_kri["kri_section"].str.strip().str.lower() == "study_specific"].copy()
    df_ss = df_ss[~df_ss["status"].str.strip().str.lower().isin(["retired", "not_applicable"])]

    # Assign TA and phase to each row
    df_ss["ta"] = df_ss["study_id"].apply(infer_ta)
    df_ss["phase"] = df_ss["study_id"].apply(lambda s: infer_phase(s, meta_df))

    yaml_root = {"metadata": {"version": "1.0", "source_studies": int(df_kri["study_id"].nunique())}, }

    # Count studies per TA/phase bucket
    bucket_counts = df_ss.groupby(["ta", "phase"])["study_id"].nunique()

    for (ta, phase), group in df_ss.groupby(["ta", "phase"]):
        ta_clean = ta.replace("-", "_").replace(" ", "_").lower()
        phase_clean = phase.replace(" ", "_").replace("-", "_").lower()

        if ta_clean not in yaml_root:
            yaml_root[ta_clean] = {}
        if phase_clean not in yaml_root[ta_clean]:
            yaml_root[ta_clean][phase_clean] = {"study_count": int(bucket_counts.get((ta, phase), 0)), "templates": []}

        templates = []
        seen = set()

        for kri_label_raw in group["kri_label"].dropna().unique():
            kri_label = str(kri_label_raw).strip()
            if not kri_label or kri_label in seen:
                continue
            seen.add(kri_label)

            subset = group[group["kri_label"].str.strip() == kri_label]
            bucket_total = bucket_counts.get((ta, phase), 1)
            study_count = subset["study_id"].nunique()
            freq_rate = study_count / bucket_total

            # Only include KRIs present in ≥50% of bucket studies
            if freq_rate < FREQUENCY_THRESHOLD:
                continue

            tier = 1 if study_count >= 10 else (2 if study_count >= 3 else 3)
            mod_val, mod_dist = derive_modal(subset["moderate_threshold"].tolist())
            high_val, high_dist = derive_modal(subset["high_threshold"].tolist())
            if mod_dist == "dispersed":
                tier = min(tier, 2)

            iqmp_id = most_frequent(subset["iqmp_risk_id"])
            weight = most_frequent(subset["weight"]) or "High"
            rel_dir = most_frequent(subset["relative_score_direction"])
            direction = "both"
            if rel_dir == "Above":
                direction = "above"
            elif rel_dir == "Below":
                direction = "below"

            # Build kri_id from label
            kri_id = "KRI_" + re.sub(r"[^A-Z0-9]", "_", kri_label.upper())[:40].rstrip("_")

            # Logic template from most common logic_summary
            logic_text = ""
            if "logic_summary" in subset.columns:
                logic_text = standardize_corrective_action(most_frequent(subset["logic_summary"]))

            corrective = ""
            if "corrective_action" in subset.columns:
                corrective = standardize_corrective_action(most_frequent(subset["corrective_action"]))

            entry = {
                "kri_id": kri_id,
                "kri_label": kri_label,
                "iqmp_risk_id": iqmp_id if iqmp_id and str(iqmp_id) not in ("nan", "None") else None,
                "thresholds": {
                    "moderate": {"absolute": mod_val, "relative_score": 1.3},
                    "high": {"absolute": high_val, "relative_score": 3.0},
                },
                "threshold_direction": direction,
                "weight": weight,
                "confidence_tier": tier,
                "source_study_count": study_count,
                "frequency_rate": round(freq_rate, 2),
                "threshold_distribution": mod_dist,
            }
            if logic_text:
                entry["logic_template"] = logic_text[:200]
            if corrective:
                entry["corrective_actions"] = corrective

            templates.append(entry)

        # Sort by frequency_rate desc
        templates.sort(key=lambda x: x["frequency_rate"], reverse=True)
        yaml_root[ta_clean][phase_clean]["templates"] = templates

    return yaml_root


def build_qtl_benchmarks(df_qtl: pd.DataFrame, meta_df: pd.DataFrame) -> dict:
    """Build qtl_benchmarks.yaml — grouped by TA/phase."""
    df_qtl = df_qtl[~df_qtl.get("status", pd.Series(dtype=str)).str.strip().str.lower().isin(["retired"])].copy()
    df_qtl["ta"] = df_qtl["study_id"].apply(infer_ta)
    df_qtl["phase"] = df_qtl["study_id"].apply(lambda s: infer_phase(s, meta_df))

    yaml_root = {"metadata": {"version": "1.0", "source_studies": int(df_qtl["study_id"].nunique())}}

    for (ta, phase), group in df_qtl.groupby(["ta", "phase"]):
        ta_clean = ta.replace("-", "_").replace(" ", "_").lower()
        phase_clean = phase.replace(" ", "_").replace("-", "_").lower()

        if ta_clean not in yaml_root:
            yaml_root[ta_clean] = {}
        yaml_root[ta_clean][phase_clean] = {"qtls": []}

        for qtl_name_raw in group["qtl_name"].dropna().unique():
            qtl_name = str(qtl_name_raw).strip()
            subset = group[group["qtl_name"].str.strip() == qtl_name]
            study_count = subset["study_id"].nunique()

            exp_val, exp_dist = derive_modal(subset["expectation"].tolist())
            tol_val, tol_dist = derive_modal(subset["tolerance_limit"].tolist())

            tier = 1 if study_count >= 10 else (2 if study_count >= 3 else 3)
            sister = most_frequent(subset["has_sister_kri"]) if "has_sister_kri" in subset else None

            entry = {
                "qtl_name": qtl_name,
                "expectation_pct": exp_val,
                "tolerance_limit_pct": tol_val,
                "confidence_tier": tier,
                "source_study_count": study_count,
                "has_sister_kri": bool(str(sister).lower() == "true") if sister else False,
            }
            if "numerator_definition" in subset.columns:
                num_text = most_frequent(subset["numerator_definition"])
                if num_text and str(num_text) not in ("nan", "None"):
                    entry["numerator_hint"] = str(num_text)[:150]

            yaml_root[ta_clean][phase_clean]["qtls"].append(entry)

    return yaml_root


# ─── YAML Writer ──────────────────────────────────────────────────────────────

def _to_native(obj):
    """Recursively convert numpy/pandas types to Python natives for YAML safety."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_native(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and (obj != obj):  # NaN check
        return None
    return obj


def write_yaml(data: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = _to_native(data)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(clean, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"  Written: {path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build YAML benchmark files from ground truth CSVs.")
    parser.add_argument("--kri-csv", default="data/cmp_kri_ground_truth.csv")
    parser.add_argument("--qtl-csv", default="data/cmp_qtl_ground_truth.csv")
    parser.add_argument("--study-meta", default="data/cmp_study_metadata.csv")
    parser.add_argument("--output-dir", default="data/yamls")
    parser.add_argument("--split", choices=["train", "all"], default="train",
                        help="train=exclude verify set studies, all=use all studies")
    parser.add_argument("--frequency-threshold", type=float, default=0.25,
                        help="Minimum frequency rate to include SS KRI template (default 0.5)")
    args = parser.parse_args()

    print(f"Loading CSVs...")
    df_kri = pd.read_csv(args.kri_csv, dtype=str).fillna("")
    df_qtl = pd.read_csv(args.qtl_csv, dtype=str).fillna("")
    meta_df = pd.read_csv(args.study_meta, dtype=str).fillna("") if os.path.exists(args.study_meta) else pd.DataFrame()

    if args.split == "train":
        # Exclude verify set studies (keep B7981027 as train per design doc usage)
        # In production you'd have a verify_set list; here we just use all
        print(f"Using split=train (all {df_kri['study_id'].nunique()} studies as training data)")
        print("  Note: In production, exclude verify set study IDs before running yaml_builder")

    total_studies = df_kri["study_id"].nunique()
    print(f"Studies in KRI CSV: {total_studies}")
    print(f"KRI rows: {len(df_kri)} | QTL rows: {len(df_qtl)}")

    # Build global KRI rules
    print("\nBuilding global_kri_rules.yaml...")
    global_rules = build_global_kri_rules(df_kri, meta_df)
    write_yaml(global_rules, os.path.join(args.output_dir, "global_kri_rules.yaml"))

    # Build SS KRI benchmarks
    print("\nBuilding ss_kri_benchmarks.yaml...")
    ss_benchmarks = build_ss_kri_benchmarks(df_kri, meta_df)
    write_yaml(ss_benchmarks, os.path.join(args.output_dir, "ss_kri_benchmarks.yaml"))

    # Build QTL benchmarks
    print("\nBuilding qtl_benchmarks.yaml...")
    qtl_benchmarks = build_qtl_benchmarks(df_qtl, meta_df)
    write_yaml(qtl_benchmarks, os.path.join(args.output_dir, "qtl_benchmarks.yaml"))

    # Summary
    print(f"\nYAML Build Summary:")
    global_count = len(global_rules.get("global_kris", []))
    print(f"  global_kri_rules.yaml : {global_count} KRI templates")

    ss_count = 0
    for ta_data in ss_benchmarks.values():
        if isinstance(ta_data, dict):
            for phase_data in ta_data.values():
                if isinstance(phase_data, dict):
                    ss_count += len(phase_data.get("templates", []))
    print(f"  ss_kri_benchmarks.yaml: {ss_count} SS KRI templates across TA/phase buckets")

    qtl_count = 0
    for ta_data in qtl_benchmarks.values():
        if isinstance(ta_data, dict):
            for phase_data in ta_data.values():
                if isinstance(phase_data, dict):
                    qtl_count += len(phase_data.get("qtls", []))
    print(f"  qtl_benchmarks.yaml   : {qtl_count} QTL templates across TA/phase buckets")
    print(f"\nDone. Output: {args.output_dir}/")


if __name__ == "__main__":
    main()
