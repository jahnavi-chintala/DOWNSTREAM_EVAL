"""Fix cmp_kri_ground_truth.csv rows for a CMP verify study (default C5091017).

Corrects truncated/corrupted kri_label values and removes orphan fragment
rows created by bad PDF table extraction.
"""
import argparse

import pandas as pd
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = REPO_ROOT / "cmp_eval" / "data" / "cmp_kri_ground_truth.csv"

CORRECT_GLOBAL_LABELS = [
    "Protocol Deviation Rate",
    "AE Rate",
    "SAE Rate",
    "eDiary Dosing Open to Open",
    "eDiary Dosing Open to Save",
    "eDiary/ Acute COVID-19 Signs and Symptoms Open to Open",
    "eDiary/ Acute COVID-19 Signs and Symptoms Open to Save",
    "ePRO Long COVID-19 Signs and Symptoms Open to Open",
    "ePRO Long COVID-19 Signs and Symptoms Open to Save",
    "ePRO EQ5D5L Open to Open",
    "ePRO EQ5D5L Open to Save",
    "ePRO WPAI Open to Open",
    "ePRO WPAI Open to Save",
    "ePRO PGI Open to Open",
    "ePRO PGI Open to Save",
    "ePRO PROMIS Fatigue Open to Open",
    "ePRO PROMIS Fatigue Open to Save",
    "ePRO PROMIS Dyspnea Open to Open",
    "ePRO PROMIS Dyspnea Open to Save",
    "ePRO PROMIS Cognitive Function Open to Open",
    "ePRO PROMIS Cognitive Function Open to Save",
]

HIGH_WEIGHT_KRIS = {"Protocol Deviation Rate", "AE Rate", "SAE Rate"}

SS_CORRECTIONS = [
    {"kri_label": "Missed NP Swab collection", "iqmp_risk_id": "SR-03134",
     "moderate_threshold": ">=10% and < 25% missing", "high_threshold": ">= 25% missing",
     "kri_code": "KRI_MISSED_NPSWAB"},
    {"kri_label": "Missed Acute COVID Symptoms", "iqmp_risk_id": "SR-03125",
     "moderate_threshold": ">10% and < 25% missing", "high_threshold": ">= 25% missing",
     "kri_code": "KRI_ACUTE_COVID"},
    {"kri_label": "Missed Long Covid and Other Long Covid Symptoms", "iqmp_risk_id": "SR-03125",
     "moderate_threshold": ">10% and < 25% missing", "high_threshold": ">= 25% missing",
     "kri_code": "KRI_LONG_COVID"},
    {"kri_label": "Missed Dosing Diary", "iqmp_risk_id": "SR-03131",
     "moderate_threshold": ">10% and < 25% missing", "high_threshold": ">= 25% missing",
     "kri_code": "KRI_MISSED_DOSING_DIARY"},
    {"kri_label": "eCOA ePRO overall compliance", "iqmp_risk_id": "VR-00007",
     "moderate_threshold": "NA", "high_threshold": "NA",
     "kri_code": "KRI_ECOA_EPRO_OVERALL_COMPLIANCE"},
    {"kri_label": "SAE Reporting Timeliness", "iqmp_risk_id": "SR-03123",
     "moderate_threshold": ">1<1.5 Days", "high_threshold": ">=1.5 Days",
     "kri_code": "KRI_SAE_LATENCY"},
    {"kri_label": "Early Termination Rate", "iqmp_risk_id": "TA Specific Standard KRI",
     "moderate_threshold": "NA", "high_threshold": "NA",
     "kri_code": "KRI_EARLY_TERM"},
    {"kri_label": "Negative Viral RNA level at Baseline", "iqmp_risk_id": "SR-03134, SR-03125",
     "moderate_threshold": "25%", "high_threshold": "30%",
     "kri_code": "KRI_VLBL"},
    {"kri_label": "AE Ongoing Days", "iqmp_risk_id": "SR-03123",
     "moderate_threshold": "100", "high_threshold": "150",
     "kri_code": "KRI_AE_ONGOING_DAYS"},
    {"kri_label": "Ongoing AE Rate of Active Subjects", "iqmp_risk_id": "SR-03132",
     "moderate_threshold": "20%", "high_threshold": "30%",
     "kri_code": "KRI_AE_ONGOING_ACTIVE"},
]

SISTER_CORRECTIONS = [
    {"kri_label": "Participants lost to follow-up at Day 28",
     "moderate_threshold": ">=10% and < 20%", "high_threshold": ">= 20%"},
]


def fix_cmp_kri_ground_truth(study_id: str) -> None:
    shutil.copy2(CSV_PATH, CSV_PATH.with_name(f"{CSV_PATH.stem}.{study_id}.bak_gtfix{CSV_PATH.suffix}"))
    print("Backup created.")

    df = pd.read_csv(CSV_PATH, dtype=str)
    n_before = len(df)

    is_tgt = df["study_id"].str.strip() == study_id
    is_global = df["kri_section"].str.strip().str.lower() == "global"
    is_ss = df["kri_section"].str.strip().str.lower() == "study_specific"

    old_global_mask = is_tgt & is_global
    old_global_idx = df[old_global_mask].index
    print(f"Removing {len(old_global_idx)} old global rows for {study_id}")

    template = df.loc[old_global_idx[0]].to_dict()

    new_rows = []
    for label in CORRECT_GLOBAL_LABELS:
        row = template.copy()
        row["kri_label"] = label
        row["weight"] = "High" if label in HIGH_WEIGHT_KRIS else "Moderate"
        row["moderate_threshold"] = "n/a - only relative score"
        row["high_threshold"] = "n/a - only relative score"
        row["relative_score_direction"] = "Both"
        row["status"] = "active"
        row["comment"] = ""
        row["iqmp_risk_id"] = ""
        new_rows.append(row)

    insert_pos = old_global_idx[0]
    df_before = df.loc[: insert_pos - 1] if insert_pos > 0 else pd.DataFrame(columns=df.columns)
    df_after = df.loc[old_global_idx[-1] + 1 :]
    new_global_df = pd.DataFrame(new_rows, columns=df.columns)
    df = pd.concat([df_before, new_global_df, df_after], ignore_index=True)

    # Fix study-specific KRIs
    is_tgt = df["study_id"].str.strip() == study_id
    is_ss = df["kri_section"].str.strip().str.lower() == "study_specific"
    ss_rows = df[is_tgt & is_ss]
    for (csv_idx, _), fix in zip(ss_rows.iterrows(), SS_CORRECTIONS):
        for col, val in fix.items():
            df.at[csv_idx, col] = val
        df.at[csv_idx, "weight"] = "High"
        df.at[csv_idx, "relative_score_direction"] = "Both"
        df.at[csv_idx, "status"] = "active"

    # Fix sister KRIs
    is_sister = df["kri_section"].str.strip().str.lower() == "sister"
    sister_rows = df[is_tgt & is_sister]
    for (csv_idx, _), fix in zip(sister_rows.iterrows(), SISTER_CORRECTIONS):
        for col, val in fix.items():
            df.at[csv_idx, col] = val
        df.at[csv_idx, "weight"] = "Moderate"
        df.at[csv_idx, "relative_score_direction"] = "Above"
        df.at[csv_idx, "status"] = "active"

    df.to_csv(CSV_PATH, index=False)
    print(f"Saved. Rows: {n_before} -> {len(df)}")

    # Verify
    c5 = df[df["study_id"].str.strip() == study_id]
    gk = c5[c5["kri_section"].str.strip().str.lower() == "global"]
    ss = c5[c5["kri_section"].str.strip().str.lower() == "study_specific"]
    sr = c5[c5["kri_section"].str.strip().str.lower() == "sister"]
    print(f"\n{study_id}: {len(c5)} rows  (global={len(gk)}, ss={len(ss)}, sister={len(sr)})")
    print("\nGlobal KRIs:")
    for i, (_, r) in enumerate(gk.iterrows()):
        print(f"  {i:2d}. {r['kri_label']}")
    print("\nStudy-Specific KRIs:")
    for i, (_, r) in enumerate(ss.iterrows()):
        iqmp = r.get("iqmp_risk_id", "")
        print(f"  {i:2d}. {r['kri_label']}  (iqmp={iqmp})")
    print("\nSister KRIs:")
    for i, (_, r) in enumerate(sr.iterrows()):
        print(f"  {i:2d}. {r['kri_label']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair CMP KRI ground-truth rows for a verify study.")
    parser.add_argument("--study-id", default="C5091017", help="Study id column value to repair (default C5091017).")
    ns = parser.parse_args()
    fix_cmp_kri_ground_truth(ns.study_id)
