import json, pandas as pd
from pathlib import Path

with open(r"C:\Users\jahna\Downloads\C5091017 (2)\C5091017\Output\D3\generated\C5091017_CMP.json", "r", encoding="utf-8") as f:
    data = json.load(f)

df = pd.read_csv(str(Path(__file__).resolve().parent.parent / "data" / "cmp_kri_ground_truth.csv"), dtype=str)
c5 = df[(df["study_id"].str.strip() == "C5091017") & (df["kri_section"].str.strip().str.lower() == "study_specific")]

for kri in data["study_specific_kris"][:3]:
    label = kri.get("kri_label", "")
    print(f"=== {label} ===")
    
    print("  GENERATOR logic_summary:")
    print(f"    {kri.get('logic_summary', '')}")
    
    gt_row = c5[c5["kri_label"].str.strip() == label]
    if not gt_row.empty:
        gt_ls = gt_row.iloc[0].get("logic_summary", "")
        print("  GT (verbatim from PDF):")
        print(f"    {gt_ls}")
    
    print("  GENERATOR corrective_action:")
    print(f"    {kri.get('corrective_action', '')}")
    
    if not gt_row.empty:
        gt_ca = gt_row.iloc[0].get("corrective_action", "")
        print("  GT (verbatim from PDF):")
        print(f"    {gt_ca}")
    print()
