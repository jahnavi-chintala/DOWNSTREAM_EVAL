"""
Populate cmp_kri_ground_truth.csv for C5091017 with data from the actual CMP PDF.
Fields populated: forms_variables, logic_summary, corrective_action, comment
for all study-specific KRIs; comment for global KRIs.
"""

import shutil
import pandas as pd
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = REPO_ROOT / "cmp_eval" / "data" / "cmp_kri_ground_truth.csv"
STUDY = "C5091017"

SS_KRI_DATA = {
    "Missed NP Swab collection": {
        "forms_variables": (
            "ELECTRONIC SAMPLE TRACKING - NP SWAB [BEETRK005]; "
            "Sample Collected [BEOCCUR_005]; "
            "Date of Visit [SV001]; "
            "RANDOMIZATION [DS001_3]; "
            "Date of Randomization [DSSTDTC_001]"
        ),
        "logic_summary": (
            "Include randomized patients for whom 'Sample Collected' BEOCCUR_005 is "
            "marked as 'NO' or BEOCCUR_005_ND is marked as 'Not Done' at Day1, "
            "Day5, Day10, Day14, Day84, Day168 and ET visits."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The % of missing NP Swab collection is significantly "
            "higher than study average. Assess site procedures for NP swab collection. "
            "Determine root cause and any further appropriate mitigations."
        ),
        "comment": (
            "Study Management will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Missed Acute COVID Symptoms": {
        "forms_variables": (
            "SM_FACOSS042: Y_FADTC_042_DTS, Y_FACAT_042, Y_FAOBJ_042; "
            "SM_SLOPD: X_DS_STAT_001, X_DS_DAT_001; "
            "SM_SV001: SVSTDTC_001"
        ),
        "logic_summary": (
            "Acute COVID symptoms is non-CRF data transferred by vendor and loaded into DMW. "
            "For randomized subjects only. Category for Findings About is equal to "
            "COVID-19 SIGNS AND SYMPTOMS, Findings About Reported Term For Clinical Event "
            "is equal to STUFFY OR RUNNY NOSE. For each patient after 42 days "
            "(28 days to complete diary + 14 days for transfer delay) we calculate the "
            "number of missing diaries (distinct Date/Time of Collection [Y_FADTC_042_DTS]) "
            "based on the following: For subjects who don't have discontinued status or "
            "who have discontinued status after completing 28 days in the study we expect "
            "28 diaries. For subjects who have discontinued status before completing 28 days "
            "in study we expect diaries till the day the discontinued."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The % of missing Acute COVID Symptoms diary is "
            "significantly higher than study average. Assess site procedures for both "
            "diaries. Determine root cause and any further appropriate mitigations."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Missed Long Covid and Other Long Covid Symptoms": {
        "forms_variables": (
            "SM_QSLCS241: QSDTC_241_DTS and QSTEST_241; "
            "SM_SLOPD: X_DS_STAT_001 and X_DS_DAT_001; "
            "SM_SV001: SVSTDTC_001"
        ),
        "logic_summary": (
            "Missed Long Covid and Other Long Covid Symptoms is non-CRF data transferred "
            "by vendor and loaded into DMW. For randomized subjects only. Test Name is equal "
            "to LCS01-Shortness of Breath While Resting. For each patient after completing "
            "182 days (168 days + 14 days transfer delay) we calculate the number of missing "
            "diaries (distinct Date/Time of Collection [QSDTC_241_DTS]) based on the following: "
            "For subjects who don't have discontinued status we expect 21 diaries "
            "(we don't count the diary from DAY1). For subjects who have discontinued status "
            "after completing 29 days in study we expect weekly diaries till Week24."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The % of missing Missed Long Covid and Other Long Covid "
            "Symptoms significantly higher than study average. Assess site procedures for both "
            "diaries. Determine root cause and any further appropriate mitigations."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Missed Dosing Diary": {
        "forms_variables": (
            "EC001: Start Date of Treatment [ECSTDAT], Start Time of Treatment [ECSTTIM], "
            "Category of Treatment [ECCAT], Actual Dose [ECDOSE]; "
            "SV001: Date of Visit [SVSDTC_001]"
        ),
        "logic_summary": (
            "Missed Dosing Diary is non-CRF data transferred by vendor and loaded into RCC. "
            "For randomized subjects only. Category of Assessment is equal to "
            "INVESTIGATIONAL PRODUCT. Assessment will be marked as DONE when Date of Treatment "
            "and Dose responses are not NULL (14 Days of data transfer delay will be considered "
            "from Day 5 + 24hours). Patients will be flagged if they have taken fewer than "
            "16 tablets (8 doses) till DAY5 + 24hours (have compliance below 80%)."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The % of missing Dosing Diary is significantly higher "
            "than study average. Assess site procedures for Missed Dosing Diary. "
            "Determine root cause and any further appropriate mitigations."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "eCOA ePRO overall compliance": {
        "forms_variables": (
            "Compliance report download from Signant Health Trail Manager; Compliance (%)"
        ),
        "logic_summary": (
            "For randomized subjects only. The mean overall compliance by site."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The % of missing eCOA/ePRO is significantly higher "
            "than study average. Assess site procedures for eCOA/ePRO. "
            "Determine root cause and any further appropriate mitigations."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "SAE Reporting Timeliness": {
        "forms_variables": (
            "SAE Latency Report; Variable: DAYS ELAPSED (B/W AWARENESS DATE & DATE REPORTED)"
        ),
        "logic_summary": (
            "Average time from when an SAE comes to the knowledge of PI to its reporting."
        ),
        "corrective_action": (
            "Study team to investigate the root cause for the delay in reporting "
            "for mitigation action."
        ),
        "comment": (
            "Study Management will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Early Termination Rate": {
        "forms_variables": (
            "DISPOSITION - TREATMENT (DS001_5): Date of Completion/Discontinuation/Death "
            "(DSSTDTC_001), Status (DSDECOD_001); "
            "DISPOSITION - FOLLOW-UP (DS001_6): Date of Completion/Discontinuation/Death "
            "(DSSTDTC_001), Status (DSDECOD_001)"
        ),
        "logic_summary": (
            "The percentage of subjects who terminate from the study 'early', among all "
            "randomized subjects during both treatment and Follow-up phase."
        ),
        "corrective_action": (
            "Action Name: Review Site Level Documents/Contact Site for More Information. "
            "Action Description: Site has a higher Early Termination Rate when compared to "
            "other sites. CRA to check reason for termination to identify if this is "
            "coincidental or trend. Review subject retention plan with site. Please check "
            "the site's understanding of the Protocol defined termination criteria to "
            "determine if the site makes any mistakes in evaluating this."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Negative Viral RNA level at Baseline": {
        "forms_variables": (
            "MBMC019: MBORRES_019, MBLOC_019-NASOPHARYNX, MBDTC_019_DTS; "
            "DS001_3: DSSTDTC_001"
        ),
        "logic_summary": (
            "Percentage of participants with baseline viral load of "
            "'Not Detected/ below LLOQ (defined as < 2 log10 copies/mL)'."
        ),
        "corrective_action": (
            "Study team to investigate the root cause."
        ),
        "comment": (
            "1. DSSTDTC_001 = MBDTC_019_DTS (i.e., Randomization date and Viral load "
            "collection date must be same to find the relevant viral load record). "
            "Clinical will be assigned to review this signal in CluePoints per study team's agreement"
        ),
    },
    "AE Ongoing Days": {
        "forms_variables": (
            "Adverse Event Report: AESTDAT, AEONGO; Disposition (DS): DS_STAT"
        ),
        "logic_summary": (
            "Average number of days that AEs are ongoing for active participants."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The number of days for ongoing AEs for active "
            "participants is significantly higher than study average. Please determine "
            "the root cause and re-train the site on ongoing AE reporting."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
    "Ongoing AE Rate of Active Subjects": {
        "forms_variables": (
            "Adverse Event Report: AEONGO; Disposition (DS): DS_STAT"
        ),
        "logic_summary": (
            "Proportion of ongoing AEs of active participants with a minimum of "
            "30 days of ongoing AEs."
        ),
        "corrective_action": (
            "Action Name: Site Re-education. "
            "Action Description: CM: The number of ongoing AEs for active participants "
            "is significantly higher than study average. Please determine the root cause "
            "and re-train the site on ongoing AE reporting."
        ),
        "comment": (
            "Clinical will be assigned to review this signal in CluePoints "
            "per study team's agreement"
        ),
    },
}

GLOBAL_KRI_COMMENTS = {
    "Protocol Deviation Rate": (
        "Study Management will be assigned to review this signal in CluePoints"
    ),
    "AE Rate": (
        "For AE under-reporting signals, Study Management will be assigned to review the signals "
        "in CluePoints. For AE over-reporting signals, Clinical will be assigned to review the "
        "signals in CluePoints"
    ),
    "SAE Rate": (
        "For SAE under-reporting signals, Study Management will be assigned to review the signals "
        "in CluePoints. For SAE over-reporting signals, Clinical will be assigned to review the "
        "signals in CluePoints"
    ),
    "eDiary Dosing Open to Open": "Clinical will be assigned to review this signal in CluePoints",
    "eDiary Dosing Open to Save": "Clinical will be assigned to review this signal in CluePoints",
    "eDiary/ Acute COVID-19 Signs and Symptoms Open to Open": "Clinical will be assigned to review this signal in CluePoints",
    "eDiary/ Acute COVID-19 Signs and Symptoms Open to Save": "Clinical will be assigned to review this signal in CluePoints",
    "ePRO Long COVID-19 Signs and Symptoms Open to Open": "Clinical will be assigned to review this signal in CluePoints",
    "ePRO Long COVID-19 Signs and Symptoms Open to Save": "Clinical will be assigned to review this signal in CluePoints",
    "ePRO EQ5D5L Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO EQ5D5L Open to Save": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO WPAI Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO WPAI Open to Save": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PGI Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PGI Open to Save": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Fatigue Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Fatigue Open to Save": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Dyspnea Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Dyspnea Open to Save": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Cognitive Function Open to Open": "Study Management will be assigned to review this signal in CluePoints",
    "ePRO PROMIS Cognitive Function Open to Save": "Study Management will be assigned to review this signal in CluePoints",
}


def main():
    shutil.copy2(CSV_PATH, CSV_PATH.with_suffix(".csv.bak_pdf_populate"))
    df = pd.read_csv(CSV_PATH, dtype=str)

    study_mask = df["study_id"].str.strip() == STUDY
    updated_ss = 0
    updated_global = 0

    for idx, row in df[study_mask].iterrows():
        section = str(row.get("kri_section", "")).strip().lower()
        label = str(row.get("kri_label", "")).strip()

        if section == "study_specific" and label in SS_KRI_DATA:
            for field, value in SS_KRI_DATA[label].items():
                df.at[idx, field] = value
            updated_ss += 1

        elif section in ("global", "sister") and label in GLOBAL_KRI_COMMENTS:
            df.at[idx, "comment"] = GLOBAL_KRI_COMMENTS[label]
            updated_global += 1

    df.to_csv(CSV_PATH, index=False)

    print(f"Updated {updated_ss} study-specific KRI rows (forms_variables, logic_summary, corrective_action, comment)")
    print(f"Updated {updated_global} global KRI rows (comment)")

    # Verify
    df2 = pd.read_csv(CSV_PATH, dtype=str)
    c5 = df2[df2["study_id"].str.strip() == STUDY]
    ss = c5[c5["kri_section"].str.strip().str.lower() == "study_specific"]
    for col in ["forms_variables", "logic_summary", "corrective_action", "comment"]:
        filled = ss[col].notna() & (ss[col].str.strip() != "") & (ss[col].str.lower() != "nan")
        print(f"  SS {col}: {filled.sum()}/{len(ss)} filled")

    gk = c5[c5["kri_section"].str.strip().str.lower() == "global"]
    filled = gk["comment"].notna() & (gk["comment"].str.strip() != "") & (gk["comment"].str.lower() != "nan")
    print(f"  Global comment: {filled.sum()}/{len(gk)} filled")


if __name__ == "__main__":
    main()
