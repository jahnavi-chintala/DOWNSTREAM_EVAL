# CMP Eval Pipeline — D3 Protocol Intelligence Platform

Evaluates generated CMP JSONs against ground truth CSVs.
Checks **structure** (JSON schema + CluePoints compatibility) and **content** (KRI labels, thresholds, QTL values).

---

## Files

| File | Purpose |
|------|---------|
| `eval_d3_cmp.py` | Main CLI entry point — run this |
| `structure_validator.py` | Validates CMP JSON structure and CluePoints field rules |
| `content_scorer.py` | Scores KRIs/QTLs against ground truth CSVs |
| `report_generator.py` | Builds eval report JSON + console summary |
| `utils.py` | Levenshtein, Jaccard similarity, threshold parsing |
| `cmp_eval_config.yaml` | Scoring weights, metric targets, tolerance bands |
| `data/cmp_kri_ground_truth.csv` | Ground truth KRI data (32 Pfizer studies) |
| `data/cmp_qtl_ground_truth.csv` | Ground truth QTL data |
| `data/cmp_study_metadata.csv` | Study-level metadata |
| `outputs/generated/` | Drop generated CMP JSONs here |
| `outputs/eval/` | Eval reports written here |

---

## Setup

```bash
pip install -r requirements.txt
```

---

## Usage

### Evaluate one study
```bash
python eval_d3_cmp.py --input outputs/generated/B7981027_CMP.json
```

### Evaluate with explicit study ID
```bash
python eval_d3_cmp.py --input path/to/CMP.json --study-id B7981027
```

### Evaluate all JSONs in a directory
```bash
python eval_d3_cmp.py --input-dir outputs/generated/
```

### Evaluate verify set (all JSONs in outputs/generated/)
```bash
python eval_d3_cmp.py --verify-set --seed 42
```

### Suppress console output, get JSON only
```bash
python eval_d3_cmp.py --input outputs/generated/B7981027_CMP.json --no-print
```

---

## Input: CMP JSON Format

The generated CMP JSON must follow this structure:

```json
{
  "study_id": "B7981027",
  "therapeutic_area": "i_and_i",
  "phase": "phase_3",
  "crf_system": "Inform Volume 3",
  "analysis_frequency": {
    "enrollment_phase": "monthly",
    "first_kri_trigger": ">=10 unique sites..."
  },
  "signals_detected": {
    "ta": "i_and_i",
    "phase": "phase_3",
    "has_pk_sampling": true,
    "has_rollover_study": true
  },
  "global_kris": [ ... ],
  "study_specific_kris": [ ... ],
  "qtls": [ ... ]
}
```

Each KRI requires: `kri_id`, `kri_label`, `kri_type`, `active`, `thresholds`, `weight`.

Each QTL requires: `qtl_id`, `name`, `expectation_pct`, `tolerance_limit_pct`.

---

## Output: Eval Report

Each run produces `outputs/eval/eval_report_{study_id}_{date}.json` with:

- **Document Score** (0–100) + PASS/FAIL vs threshold (75) and target (80)
- **Structure Validation** — errors and warnings per CluePoints rules
- **M1 KRI Recall** — fraction of GT KRIs matched (target: 80%)
- **M2 Threshold Accuracy** — thresholds within 5% of GT modal (target: 90%)
- **M3 QTL Recall** — fraction of GT QTLs matched (target: 85%)
- **M4 Hallucinations** — conflicting IQMP Risk IDs (target: 0)
- **Section Scorecard** — Global KRIs (35%) / SS KRIs (40%) / QTLs (20%) / Metadata (5%)
- **KRI Detail** — per-KRI match type, threshold scores, issues
- **Improvement Actions** — prioritized HIGH/MEDIUM/LOW fix list

Multi-study runs also produce `outputs/eval/eval_summary.csv`.

---

## Scoring Logic

```
Document Score (0-100)
  └── Section score × section weight
        └── KRI score × (1 / max(generated, ground_truth))
              └── Attribute score × attribute weight

KRI Attributes (weights sum to 1.0):
  kri_label           0.35  verbatim → Levenshtein ≤5 → Jaccard/semantic
  moderate_threshold  0.20  within 5% of GT modal = 1.0; within 15% = 0.5
  high_threshold      0.15  same as moderate
  iqmp_risk_id        0.15  SR- code matches GT = 1.0; null GT = 0.5; conflict = 0.0
  confidence_tier     0.10  exact = 1.0; adjacent = 0.5 (no GT penalty)
  weight_field        0.05  exact match on Low/Moderate/High vocabulary
```

---

## Supported Study IDs

Any study ID present in `cmp_kri_ground_truth.csv` can be evaluated.
Currently includes: B7981027, B7981032, B7981040, B7981041, B7981080, B7981094, B7981119,
C1071003, C1071005, C1071006, C1071007, C1071015, C2321003, C2321008, C2321014,
C3651003, C3651021, C3671013, C3671058, C3671059, C4221015, C4221016, C4221022,
C4591048, C4591076, C4591081, C4591082, C4601003, C4891001, C4891002, C4891026, C5091017.

---

## Exit Codes

- `0` — all evaluated studies PASSED document_pass_threshold
- `1` — one or more studies FAILED
