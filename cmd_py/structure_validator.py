"""
structure_validator.py — CMP JSON Structure & CluePoints Compatibility Validator
Validates before content scoring. Returns structured pass/fail results.
"""

import re
from typing import Any


# ─── Required top-level sections ──────────────────────────────────────────────

REQUIRED_TOP_LEVEL_KEYS = [
    "study_id",
    "global_kris",
    "study_specific_kris",
    "qtls",
    "signals_detected",
    "analysis_frequency",
]

OPTIONAL_TOP_LEVEL_KEYS = [
    "schema_version", "generated_date", "generator_version",
    "therapeutic_area", "phase", "crf_system",
    "dqa", "generation_metadata",
]

# ─── Required fields per KRI ──────────────────────────────────────────────────

KRI_REQUIRED_FIELDS = [
    "kri_id",
    "kri_label",
    "kri_type",
    "active",
    "thresholds",
    "weight",
]

KRI_OPTIONAL_FIELDS = [
    "iqmp_risk_id", "threshold_direction", "forms_variables",
    "logic", "corrective_actions", "source", "benchmark_ref",
    "confidence_tier", "linkage", "scope",
]

# ─── Required fields per QTL ──────────────────────────────────────────────────

QTL_REQUIRED_FIELDS = [
    "qtl_id",
    "name",
    "expectation_pct",
    "tolerance_limit_pct",
]

QTL_OPTIONAL_FIELDS = [
    "condition_trigger", "denominator", "trigger_n",
    "frequency", "source", "sister_kri", "confidence_tier",
]

# ─── CluePoints vocabulary constraints ───────────────────────────────────────

VALID_WEIGHTS = {"Low", "Moderate", "High"}
VALID_THRESHOLD_DIRECTIONS = {"above", "below", "both"}
KRI_LABEL_MAX_LEN = 100
RELATIVE_SCORE_RANGE = (0.5, 5.0)
IQMP_ID_PATTERN = re.compile(r'^SR-\d{5,}$')


# ─── Validator ────────────────────────────────────────────────────────────────

class StructureValidationResult:
    def __init__(self):
        self.errors: list[dict] = []      # Critical — would fail CluePoints import
        self.warnings: list[dict] = []    # Non-critical — generate but flag
        self.checks_run: int = 0
        self.checks_passed: int = 0

    def error(self, code: str, message: str, path: str = ""):
        self.errors.append({"level": "ERROR", "code": code, "message": message, "path": path})
        self.checks_run += 1

    def warn(self, code: str, message: str, path: str = ""):
        self.warnings.append({"level": "WARNING", "code": code, "message": message, "path": path})
        self.checks_run += 1

    def ok(self):
        self.checks_run += 1
        self.checks_passed += 1

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    @property
    def score(self) -> float:
        """Structural completeness score 0-100."""
        if self.checks_run == 0:
            return 0.0
        penalty_per_error = 5.0
        penalty_per_warning = 1.0
        raw = 100 - (len(self.errors) * penalty_per_error) - (len(self.warnings) * penalty_per_warning)
        return max(0.0, min(100.0, raw))

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "structure_score": round(self.score, 1),
            "errors": self.errors,
            "warnings": self.warnings,
            "checks_run": self.checks_run,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
        }


def validate_structure(cmp_json: dict) -> StructureValidationResult:
    """
    Run all structure and CluePoints compatibility checks on a CMP JSON.
    Returns StructureValidationResult.
    """
    result = StructureValidationResult()

    if not isinstance(cmp_json, dict):
        result.error("INVALID_JSON", "CMP input is not a JSON object", "$")
        return result

    # ── Section 1: Top-level keys ─────────────────────────────────────────────
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in cmp_json:
            result.error("MISSING_SECTION", f"Required top-level key missing: '{key}'", f"$.{key}")
        else:
            result.ok()

    # study_id non-null
    study_id = cmp_json.get("study_id", "")
    if not study_id:
        result.error("NULL_STUDY_ID", "study_id is null or empty", "$.study_id")
    else:
        result.ok()

    # ── Section 2: global_kris ────────────────────────────────────────────────
    global_kris = cmp_json.get("global_kris", [])
    if not isinstance(global_kris, list):
        result.error("INVALID_GLOBAL_KRIS", "global_kris must be an array", "$.global_kris")
    else:
        if len(global_kris) == 0:
            result.warn("EMPTY_GLOBAL_KRIS", "global_kris array is empty — expected 3-8 global KRIs", "$.global_kris")
        elif not (3 <= len(global_kris) <= 60):
            result.warn("GLOBAL_KRI_COUNT", f"global_kris count {len(global_kris)} outside expected range 3-8", "$.global_kris")
        else:
            result.ok()

        kri_ids_seen = set()
        for i, kri in enumerate(global_kris):
            _validate_kri(kri, f"$.global_kris[{i}]", kri_ids_seen, result)

    # ── Section 3: study_specific_kris ────────────────────────────────────────
    ss_kris = cmp_json.get("study_specific_kris", [])
    if not isinstance(ss_kris, list):
        result.error("INVALID_SS_KRIS", "study_specific_kris must be an array", "$.study_specific_kris")
    else:
        if len(ss_kris) == 0:
            result.warn("EMPTY_SS_KRIS", "study_specific_kris is empty — expected ≥1 SS KRI", "$.study_specific_kris")
        else:
            result.ok()

        kri_ids_seen_ss = set()
        for i, kri in enumerate(ss_kris):
            _validate_kri(kri, f"$.study_specific_kris[{i}]", kri_ids_seen_ss, result)

    # ── Section 4: No duplicate kri_ids across global + ss ───────────────────
    all_ids = [k.get("kri_id") for k in (global_kris + ss_kris) if isinstance(k, dict) and k.get("kri_id")]
    dup_ids = [x for x in all_ids if all_ids.count(x) > 1]
    if dup_ids:
        result.error("DUPLICATE_KRI_ID", f"Duplicate kri_ids across global+ss: {list(set(dup_ids))}", "$.kris")
    else:
        result.ok()

    # ── Section 5: qtls ───────────────────────────────────────────────────────
    qtls = cmp_json.get("qtls", [])
    if not isinstance(qtls, list):
        result.error("INVALID_QTLS", "qtls must be an array", "$.qtls")
    else:
        for i, qtl in enumerate(qtls):
            _validate_qtl(qtl, f"$.qtls[{i}]", result)

    # ── Section 6: signals_detected ──────────────────────────────────────────
    signals = cmp_json.get("signals_detected", {})
    if isinstance(signals, dict):
        if "ta" not in signals:
            result.warn("MISSING_TA_SIGNAL", "signals_detected.ta missing", "$.signals_detected.ta")
        else:
            result.ok()
        if "phase" not in signals:
            result.warn("MISSING_PHASE_SIGNAL", "signals_detected.phase missing", "$.signals_detected.phase")
        else:
            result.ok()
    else:
        result.error("INVALID_SIGNALS", "signals_detected must be an object", "$.signals_detected")

    # ── Section 7: analysis_frequency ────────────────────────────────────────
    af = cmp_json.get("analysis_frequency", {})
    if not isinstance(af, dict) or not af:
        result.warn("MISSING_ANALYSIS_FREQ", "analysis_frequency missing or empty", "$.analysis_frequency")
    else:
        result.ok()

    return result


def _validate_kri(kri: Any, path: str, ids_seen: set, result: StructureValidationResult):
    """Validate a single KRI object."""
    if not isinstance(kri, dict):
        result.error("KRI_NOT_OBJECT", f"KRI at {path} is not an object", path)
        return

    # Required fields
    for field in KRI_REQUIRED_FIELDS:
        if field not in kri:
            result.error("KRI_MISSING_FIELD", f"Required field '{field}' missing", f"{path}.{field}")
        else:
            result.ok()

    # active flag must be boolean
    active = kri.get("active")
    if active is not None and not isinstance(active, bool):
        result.warn("KRI_ACTIVE_NOT_BOOL", f"active should be boolean, got {type(active).__name__}", f"{path}.active")
    elif active is not None:
        result.ok()

    # kri_label length
    label = kri.get("kri_label", "")
    if label and len(label) > KRI_LABEL_MAX_LEN:
        result.error("KRI_LABEL_TOO_LONG", f"kri_label length {len(label)} > {KRI_LABEL_MAX_LEN} chars", f"{path}.kri_label")
    elif label:
        result.ok()

    # weight controlled vocabulary
    weight = kri.get("weight")
    if weight is not None and weight not in VALID_WEIGHTS:
        result.error("KRI_INVALID_WEIGHT", f"weight '{weight}' not in {VALID_WEIGHTS}", f"{path}.weight")
    elif weight is not None:
        result.ok()

    # threshold_direction controlled vocabulary
    td = kri.get("threshold_direction")
    if td is not None and td not in VALID_THRESHOLD_DIRECTIONS:
        result.error("KRI_INVALID_DIRECTION", f"threshold_direction '{td}' not in {VALID_THRESHOLD_DIRECTIONS}", f"{path}.threshold_direction")
    elif td is not None:
        result.ok()

    # thresholds structure
    thresholds = kri.get("thresholds", {})
    if isinstance(thresholds, dict):
        _validate_thresholds(thresholds, f"{path}.thresholds", result)

    # iqmp_risk_id format (if present)
    iqmp = kri.get("iqmp_risk_id")
    if iqmp and not _is_valid_iqmp(iqmp):
        result.warn("KRI_IQMP_FORMAT", f"iqmp_risk_id '{iqmp}' doesn't match SR-XXXXX pattern", f"{path}.iqmp_risk_id")
    elif iqmp:
        result.ok()

    # kri_id uniqueness
    kri_id = kri.get("kri_id")
    if kri_id:
        if kri_id in ids_seen:
            result.error("KRI_DUPLICATE_ID", f"Duplicate kri_id '{kri_id}'", f"{path}.kri_id")
        else:
            ids_seen.add(kri_id)
            result.ok()


def _validate_qtl(qtl: Any, path: str, result: StructureValidationResult):
    """Validate a single QTL object."""
    if not isinstance(qtl, dict):
        result.error("QTL_NOT_OBJECT", f"QTL at {path} is not an object", path)
        return

    for field in QTL_REQUIRED_FIELDS:
        if field not in qtl:
            result.error("QTL_MISSING_FIELD", f"Required field '{field}' missing from QTL", f"{path}.{field}")
        else:
            result.ok()

    # expectation_pct and tolerance_limit_pct must be numeric
    for num_field in ["expectation_pct", "tolerance_limit_pct"]:
        val = qtl.get(num_field)
        if val is not None:
            try:
                float(val)
                result.ok()
            except (TypeError, ValueError):
                result.warn("QTL_NON_NUMERIC", f"{num_field} is not numeric: {val}", f"{path}.{num_field}")


def _validate_thresholds(thresholds: dict, path: str, result: StructureValidationResult):
    """Validate thresholds sub-object."""
    for level in ["moderate", "high"]:
        if level in thresholds:
            t = thresholds[level]
            if isinstance(t, dict):
                rel = t.get("relative_score")
                if rel is not None:
                    try:
                        v = float(rel)
                        if not (RELATIVE_SCORE_RANGE[0] <= v <= RELATIVE_SCORE_RANGE[1]):
                            result.warn("UNUSUAL_REL_SCORE",
                                        f"{level} relative_score {v} outside typical range {RELATIVE_SCORE_RANGE}",
                                        f"{path}.{level}.relative_score")
                        else:
                            result.ok()
                    except (TypeError, ValueError):
                        result.warn("INVALID_REL_SCORE", f"{level} relative_score not numeric: {rel}", f"{path}.{level}")


def _is_valid_iqmp(iqmp: Any) -> bool:
    """Check SR-XXXXX format or accept comma-separated list of SR- codes."""
    s = str(iqmp).strip()
    # allow comma-separated or semicolon-separated multiple codes
    parts = re.split(r'[,;]', s)
    for part in parts:
        clean = part.strip()
        if clean and not IQMP_ID_PATTERN.match(clean):
            # Some ground truth has codes like "SR-02224 (KRI_MA_PGIC)" - extract SR- code
            m = re.search(r'SR-\d{5,}', clean)
            if not m:
                return False
    return True
