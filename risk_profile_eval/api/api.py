"""
api.py
------
D1 Risk Profile Eval Framework – FastAPI Microservice.

Exposes all eval functions as REST endpoints so other D2/D1 platform components
(orchestrator, reporting tool, D3 generator) can trigger evaluations without
importing Python modules directly.

Endpoints:
  GET  /health                          Liveness probe
  GET  /health/ready                    Readiness probe (checks CSV files exist)
  POST /eval/detect                     Detect which scenario would run (no eval)
  POST /eval/scenario1                  Explicit Scenario 1 eval (ground truth)
  POST /eval/scenario2                  Explicit Scenario 2 eval (no ground truth)
  POST /eval/run                        Unified auto-detect + run (recommended)
  POST /eval/batch                      Batch run all 8 verify studies
  POST /export/risk-profile-docx       Risk Profile JSON → Word (.docx)
  GET  /results/{study_id}             Full cached result
  GET  /results/{study_id}/scorecard   Compact metrics scorecard
  GET  /results/{study_id}/failures    Failure list (Scenario 1) or signals (S2)
  GET  /results/{study_id}/review_list Human review queue (Scenario 2)
  DELETE /results/{study_id}           Clear cached result

Start:
    export GROUND_TRUTH_RISKS=path/to/risk_profile_ground_truth.csv
    export GROUND_TRUTH_FACTORS=path/to/critical_factors_ground_truth.csv
    export OUTPUT_DIR=outputs/
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Interactive docs: http://localhost:8000/docs
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Third-party ───────────────────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

# ── Internal ──────────────────────────────────────────────────────────────────
from core.eval_scenario1 import run_scenario1_eval, classify_failures, VERIFY_STUDIES
from core.eval_scenario2 import run_scenario2_eval
from scripts.run_eval import (
    detect_scenario, run_eval,
    save_results_json, save_near_misses_csv, save_hallucination_report,
)
from scripts.run_all_verify import run_all_verify
from reports.risk_profile_json_to_docx import write_risk_profile_docx
from api.routes import register_eval_upload_routes


# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

GROUND_TRUTH_RISKS: str = os.getenv("GROUND_TRUTH_RISKS", "")
GROUND_TRUTH_FACTORS: str = os.getenv("GROUND_TRUTH_FACTORS", "")
GENERATOR_OUTPUT_DIR: str = os.getenv("GENERATOR_OUTPUT_DIR", "")
OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "/tmp/riskprofile_eval_outputs")

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY RESULT CACHE
# Replace with Redis for multi-worker production deployment.
# ─────────────────────────────────────────────────────────────────────────────

_result_cache: Dict[str, Dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class EvalRequest(BaseModel):
    """Base request model shared by all eval endpoints."""
    generator_json: str = Field(..., description="Path to {study_id}_RiskProfile.json")
    study_id: str = Field(..., description="Protocol / study identifier")
    output_dir: Optional[str] = Field(None, description="Output directory (defaults to OUTPUT_DIR env var)")


class Scenario1Request(EvalRequest):
    """Request model for the /eval/scenario1 endpoint."""
    ground_truth_risks: str = Field(..., description="Path to risk_profile_ground_truth.csv")
    ground_truth_factors: str = Field(..., description="Path to critical_factors_ground_truth.csv")


class Scenario2Request(BaseModel):
    """Request model for the /eval/scenario2 endpoint (no ground truth)."""
    generator_json: str = Field(..., description="Path to {study_id}_RiskProfile.json")
    study_id: str = Field(..., description="Study identifier")
    output_dir: Optional[str] = Field(None, description="Output directory")


class UnifiedEvalRequest(EvalRequest):
    """Request model for the /eval/run endpoint (auto-detect scenario)."""
    ground_truth_risks: Optional[str] = Field(None, description="Path to ground truth risks CSV (may be None for Scenario 2)")
    ground_truth_factors: Optional[str] = Field(None, description="Path to critical factors CSV")


class BatchRequest(BaseModel):
    """Request model for the /eval/batch endpoint (all 8 verify studies)."""
    generator_output_dir: str = Field(..., description="Directory containing generator JSON files")
    ground_truth_risks: str = Field(..., description="Path to risk_profile_ground_truth.csv")
    ground_truth_factors: str = Field(..., description="Path to critical_factors_ground_truth.csv")
    output_dir: Optional[str] = Field(None, description="Output directory")


class DetectRequest(BaseModel):
    """Request model for the /eval/detect endpoint."""
    study_id: str = Field(..., description="Study identifier to check")
    ground_truth_risks: Optional[str] = Field(None, description="Path to ground truth risks CSV")


class RiskProfileDocxRequest(BaseModel):
    """Request model for /export/risk-profile-docx."""
    risk_profile_json: str = Field(..., description="Path to {study_id}_RiskProfile.json")
    output_path: Optional[str] = Field(
        None,
        description="Full path for the .docx file (default: same directory/name as JSON)",
    )


class EvalResponse(BaseModel):
    """Standard response envelope for eval endpoints."""
    study_id: str
    scenario: int
    verdict: str
    timestamp: str
    output_files: List[str] = []
    result: Dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_output_dir(requested: Optional[str], study_id: str) -> str:
    """
    Resolve the output directory for an eval run.

    Priority: caller-supplied > OUTPUT_DIR env var > /tmp default.
    Creates the directory if it does not exist.
    Logs which study is resolving output to help debug path issues.

    Args:
        requested: Caller-supplied output_dir (may be None)
        study_id: Used in log messages only

    Returns:
        Resolved and created directory path string.
    """
    out_dir = requested or OUTPUT_DIR or "/tmp/riskprofile_eval_outputs"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return out_dir


def _validate_file(path: str, label: str) -> None:
    """
    Validate that a required input file exists on disk.

    Args:
        path: File path to check
        label: Human-readable label for error messages

    Raises:
        HTTPException 404 if the file does not exist
    """
    if not path or not Path(path).exists():
        raise HTTPException(
            status_code=404,
            detail=f"{label} not found: '{path}'. Check path and ensure file is accessible.",
        )


def _resolve_gt_risks(requested: Optional[str]) -> str:
    """Resolve ground_truth_risks path from request or environment."""
    path = requested or GROUND_TRUTH_RISKS
    if not path:
        raise HTTPException(
            status_code=422,
            detail="ground_truth_risks not provided and GROUND_TRUTH_RISKS env var not set.",
        )
    return path


def _resolve_gt_factors(requested: Optional[str]) -> str:
    """Resolve ground_truth_factors path from request or environment."""
    path = requested or GROUND_TRUTH_FACTORS
    if not path:
        raise HTTPException(
            status_code=422,
            detail="ground_truth_factors not provided and GROUND_TRUTH_FACTORS env var not set.",
        )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="D1 Risk Profile Eval API",
    description="Quality evaluation service for the D1 Risk Profile Generator (Pfizer DDF Hackathon)",
    version="1.0.0",
)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> Dict:
    """
    Liveness probe.

    Returns HTTP 200 immediately with status 'ok'. Used by Kubernetes liveness
    checks to confirm the container process is alive. Never returns 5xx unless
    the Python process itself has crashed.

    Returns:
        JSON: {status, service, version}
    """
    return {
        "status": "ok",
        "service": "d1-riskprofile-eval",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/health/ready")
async def readiness_check() -> Dict:
    """
    Readiness probe.

    Checks that the GROUND_TRUTH_RISKS and GROUND_TRUTH_FACTORS environment
    variables are set and that the files they point to exist on disk.
    Returns HTTP 200 if ready, HTTP 503 if not.

    Used by Kubernetes readiness checks to gate traffic before required files
    are mounted / available.

    Returns:
        JSON: {status, checks: {env_vars, file_access}}
    """
    checks = {}
    ready = True

    # Check env vars
    _pkg = Path(__file__).resolve().parent
    eff_risks = GROUND_TRUTH_RISKS or str(_pkg.parent / "data" / "risk_profile_ground_truth.csv")
    eff_factors = GROUND_TRUTH_FACTORS or str(_pkg.parent / "data" / "critical_factors_ground_truth.csv")
    checks["GROUND_TRUTH_RISKS"] = bool(GROUND_TRUTH_RISKS)
    checks["GROUND_TRUTH_FACTORS"] = bool(GROUND_TRUTH_FACTORS)
    checks["OUTPUT_DIR"] = bool(OUTPUT_DIR)
    checks["effective_ground_truth_risks"] = eff_risks
    checks["effective_ground_truth_factors"] = eff_factors

    checks["risks_csv_accessible"] = Path(eff_risks).is_file()
    checks["factors_csv_accessible"] = Path(eff_factors).is_file()
    if not checks["risks_csv_accessible"] or not checks["factors_csv_accessible"]:
        ready = False

    if not ready:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})

    return {"status": "ready", "checks": checks}


# ─────────────────────────────────────────────────────────────────────────────
# EVAL ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/detect")
async def detect_eval_scenario(req: DetectRequest) -> Dict:
    """
    Auto-detect which evaluation scenario would run for a given study_id.

    Does NOT run the evaluation — only returns which scenario would be used.
    Useful for the orchestrator to pre-check routing before submitting a full
    eval request.

    Args:
        req.study_id: Study identifier to check
        req.ground_truth_risks: Path to ground truth CSV (uses env var if not supplied)

    Returns:
        JSON: {study_id, scenario (1 or 2), reason}
    """
    gt_risks = req.ground_truth_risks or GROUND_TRUTH_RISKS
    scenario = detect_scenario(req.study_id, gt_risks) if gt_risks else 2

    return {
        "study_id": req.study_id,
        "scenario": scenario,
        "reason": (
            "Study ID found in risk_profile_ground_truth.csv (verify set)"
            if scenario == 1 else
            "Study ID not found in ground truth CSV — Scenario 2 (no ground truth) will run"
        ),
        "in_verify_set": req.study_id in VERIFY_STUDIES,
    }


@app.post("/eval/scenario1")
async def eval_scenario1(req: Scenario1Request) -> EvalResponse:
    """
    Run Scenario 1 evaluation for a single study (ground truth required).

    Validates all file paths, runs all 4 metrics (M1 Risk Name Recall, M2 RPN Tier
    Accuracy, M3 Critical Factor Match, M4 Traceability Flags), saves output
    files, caches the result, and returns an EvalResponse envelope.

    Args:
        req: Scenario1Request body

    Returns:
        EvalResponse with full Scenario 1 result payload.

    Raises:
        HTTP 404 if generator JSON or ground truth CSVs not found
        HTTP 422 on Pydantic validation errors
    """
    _validate_file(req.generator_json, "Generator JSON")
    _validate_file(req.ground_truth_risks, "Ground truth risks CSV")
    _validate_file(req.ground_truth_factors, "Critical factors ground truth CSV")

    out_dir = _resolve_output_dir(req.output_dir, req.study_id)

    result = run_scenario1_eval(
        generator_json_path=req.generator_json,
        ground_truth_risks_csv=req.ground_truth_risks,
        ground_truth_factors_csv=req.ground_truth_factors,
        study_id=req.study_id,
        allow_empty_risk_gt=True,
    )

    output_files = []
    output_files.append(save_results_json(result, out_dir, req.study_id))
    nm_path = save_near_misses_csv(result, out_dir, req.study_id)
    if nm_path:
        output_files.append(nm_path)
    output_files.append(save_hallucination_report(result, out_dir, req.study_id))

    result["output_files"] = output_files
    _result_cache[req.study_id] = result

    return EvalResponse(
        study_id=req.study_id,
        scenario=1,
        verdict=result["verdict"],
        timestamp=result["timestamp"],
        output_files=output_files,
        result=result,
    )


@app.post("/eval/scenario2")
async def eval_scenario2(req: Scenario2Request) -> EvalResponse:
    """
    Run Scenario 2 evaluation for a single study (no ground truth).

    Runs all 7 proxy signals (S1 Hallucination, S2 Confidence, S3 Risk Count,
    S4 USDM Traceability, S5 Critical Factors, S6 Placeholder IDs, S7 RPN Formula).
    Returns an overall GREEN/AMBER/RED verdict.

    Args:
        req: Scenario2Request body

    Returns:
        EvalResponse with full Scenario 2 result payload including review_list.

    Raises:
        HTTP 404 if generator JSON not found
    """
    _validate_file(req.generator_json, "Generator JSON")

    out_dir = _resolve_output_dir(req.output_dir, req.study_id)

    result = run_scenario2_eval(
        generator_json_path=req.generator_json,
        study_id=req.study_id,
    )

    output_files = [
        save_results_json(result, out_dir, req.study_id),
        save_hallucination_report(result, out_dir, req.study_id),
    ]

    result["output_files"] = output_files
    _result_cache[req.study_id] = result

    return EvalResponse(
        study_id=req.study_id,
        scenario=2,
        verdict=result["verdict"],
        timestamp=result["timestamp"],
        output_files=output_files,
        result=result,
    )


@app.post("/eval/run")
async def eval_run(req: UnifiedEvalRequest) -> EvalResponse:
    """
    Unified eval endpoint — auto-detects scenario and runs the appropriate module.

    This is the recommended endpoint for the orchestrator. It auto-detects whether
    the study_id is in the ground truth CSV (Scenario 1) or not (Scenario 2) and
    routes accordingly. Returns the same EvalResponse structure regardless of scenario.

    Args:
        req: UnifiedEvalRequest body (all fields optional except generator_json + study_id)

    Returns:
        EvalResponse JSON — structure matches the detected scenario.
    """
    _validate_file(req.generator_json, "Generator JSON")

    gt_risks = req.ground_truth_risks or GROUND_TRUTH_RISKS or ""
    gt_factors = req.ground_truth_factors or GROUND_TRUTH_FACTORS or ""
    out_dir = _resolve_output_dir(req.output_dir, req.study_id)

    result = run_eval(
        generator_json=req.generator_json,
        ground_truth_risks_csv=gt_risks,
        ground_truth_factors_csv=gt_factors,
        study_id=req.study_id,
        output_dir=out_dir,
    )

    _result_cache[req.study_id] = result
    output_files = result.get("output_files", [])

    return EvalResponse(
        study_id=req.study_id,
        scenario=result["scenario"],
        verdict=result["verdict"],
        timestamp=result["timestamp"],
        output_files=output_files,
        result=result,
    )


@app.post("/eval/batch")
async def eval_batch(req: BatchRequest, background_tasks: BackgroundTasks) -> Dict:
    """
    Run the full batch evaluation over all 8 verify studies.

    Runs synchronously and returns when all studies are complete. For the 8-study
    verify set, runtime is under 60 seconds (design spec target). Results for each
    study are also cached individually in _result_cache.

    Args:
        req: BatchRequest body

    Returns:
        JSON with aggregate summary and paths to all output files.

    Raises:
        HTTP 404 if generator_output_dir or ground truth CSVs not found
    """
    if not Path(req.generator_output_dir).exists():
        raise HTTPException(
            status_code=404,
            detail=f"Generator output directory not found: '{req.generator_output_dir}'"
        )
    _validate_file(req.ground_truth_risks, "Ground truth risks CSV")
    _validate_file(req.ground_truth_factors, "Critical factors ground truth CSV")

    out_dir = _resolve_output_dir(req.output_dir, "batch")

    all_results = run_all_verify(
        generator_output_dir=req.generator_output_dir,
        ground_truth_risks_csv=req.ground_truth_risks,
        ground_truth_factors_csv=req.ground_truth_factors,
        output_dir=out_dir,
    )

    # Cache all individual results
    for r in all_results:
        if not r.get("skipped"):
            _result_cache[r["study_id"]] = r

    # Load summary
    summary_path = Path(out_dir) / "eval_summary.json"
    summary = {}
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)

    return {
        "status": "complete",
        "studies_evaluated": len([r for r in all_results if not r.get("skipped")]),
        "studies_skipped": len([r for r in all_results if r.get("skipped")]),
        "go_no_go": summary.get("go_no_go", "UNKNOWN"),
        "output_dir": out_dir,
        "summary": summary,
    }


@app.post("/export/risk-profile-docx")
async def export_risk_profile_docx(req: RiskProfileDocxRequest) -> Dict:
    """
    Convert a generator Risk Profile JSON file into a Word document (.docx).

    Does not run evaluation; only formats the JSON into a readable report.
    """
    _validate_file(req.risk_profile_json, "Risk profile JSON")
    if req.output_path:
        parent = Path(req.output_path).parent
        parent.mkdir(parents=True, exist_ok=True)
    try:
        docx_path = write_risk_profile_docx(req.risk_profile_json, req.output_path)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DOCX export failed: {exc}") from exc

    return {"status": "success", "docx": docx_path}


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/results/{study_id}")
async def get_results(study_id: str) -> Dict:
    """
    Return the full cached eval result for a study.

    Results are stored in the in-memory _result_cache keyed by study_id.
    For production with multiple uvicorn workers, replace _result_cache with Redis
    so all workers share the same cache.

    Args:
        study_id: Study identifier (path parameter)

    Returns:
        Full result dict as JSON.

    Raises:
        HTTP 404 if study_id not cached (run /eval/run first)
    """
    if study_id not in _result_cache:
        raise HTTPException(
            status_code=404,
            detail=f"No eval results found for study '{study_id}'. Run /eval/run or /eval/batch first."
        )
    return _result_cache[study_id]


@app.get("/results/{study_id}/scorecard")
async def get_scorecard(study_id: str) -> Dict:
    """
    Return a compact scorecard for a study.

    Returns verdict + per-metric pass/fail + individual scores without the full
    result payload. Designed for dashboard widgets and quick status checks.

    For Scenario 1: returns M1–M4 scores and targets.
    For Scenario 2: returns S1–S7 signal statuses.

    Args:
        study_id: Study identifier (path parameter)

    Returns:
        JSON: {study_id, verdict, scenario, metrics or signals}
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404,
                            detail=f"No results for study '{study_id}'.")

    r = _result_cache[study_id]
    scenario = r.get("scenario", 1)

    if scenario == 1:
        metrics = r.get("metrics", {})
        m1 = metrics.get("m1_risk_name_recall", {})
        m2 = metrics.get("m2_rpn_tier_accuracy", {})
        m3 = metrics.get("m3_critical_factor_match", {})
        m4 = metrics.get("m4_hallucination_detection", {})
        scorecard_metrics = [
            {"metric": "M1 Risk Name Recall", "score": m1.get("score"), "target": 0.85, "passed": m1.get("passed")},
            {
                "metric": "M2 RPN Exact Match",
                "score": m2.get("score"),
                "target": 0.90,
                "passed": m2.get("passed"),
                "skipped": m2.get("skipped"),
            },
            {"metric": "M3 Critical Factor Match", "score": m3.get("score"), "target": 0.80, "passed": m3.get("passed"), "skipped": m3.get("skipped")},
            {
                "metric": "M4 Provenance Defects",
                "score": m4.get("provenance_defect_count", m4.get("hallucinations_found")),
                "semantic_unmatched_risks": m4.get("semantic_hallucination_count"),
                "target": 0,
                "passed": m4.get("passed"),
            },
        ]
    else:
        signals = r.get("signals", {})
        scorecard_metrics = [
            {"signal": sid, "name": sig["name"], "status": sig["status"]}
            for sid, sig in sorted(signals.items())
        ]

    return {
        "study_id": study_id,
        "scenario": scenario,
        "verdict": r.get("verdict"),
        "ta": r.get("ta"),
        "phase": r.get("phase"),
        "timestamp": r.get("timestamp"),
        "metrics" if scenario == 1 else "signals": scorecard_metrics,
    }


@app.get("/results/{study_id}/failures")
async def get_failures(study_id: str) -> Dict:
    """
    Return the failure list for a study.

    For Scenario 1: returns the classify_failures() output — structured failure
    entries with severity, actual vs target, root cause, and recommended fix.
    Used by the reporting tool to populate the 'Issues Found' section.

    For Scenario 2: returns the FAIL and WARN signal details.

    Args:
        study_id: Study identifier (path parameter)

    Returns:
        JSON: {study_id, scenario, failures: []}
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404,
                            detail=f"No results for study '{study_id}'.")

    r = _result_cache[study_id]
    scenario = r.get("scenario", 1)

    if scenario == 1:
        failures = classify_failures(r)
    else:
        # Scenario 2: return failing and warning signals
        failures = []
        for sid, sig in sorted(r.get("signals", {}).items()):
            if sig["status"] in ("FAIL", "WARN"):
                failures.append({
                    "signal_id": sid,
                    "name": sig["name"],
                    "status": sig["status"],
                    "description": sig.get("description", ""),
                })

    return {
        "study_id": study_id,
        "scenario": scenario,
        "failure_count": len(failures),
        "failures": failures,
    }


@app.get("/results/{study_id}/review_list")
async def get_review_list(study_id: str) -> Dict:
    """
    Return the human review queue for a study.

    For Scenario 2: returns all risks with LOW rpn_confidence that require clinical
    reviewer sign-off before submission. This list is mandatory to work through on
    May 18 before confirming outputs are ready.

    For Scenario 1: returns an empty list (ground truth makes manual review
    unnecessary for verify studies).

    Args:
        study_id: Study identifier (path parameter)

    Returns:
        JSON: {study_id, review_list_count, review_list: []}
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404,
                            detail=f"No results for study '{study_id}'.")

    r = _result_cache[study_id]
    review_list = r.get("review_list", [])

    return {
        "study_id": study_id,
        "scenario": r.get("scenario"),
        "review_list_count": len(review_list),
        "review_list": review_list,
    }


@app.delete("/results/{study_id}")
async def clear_results(study_id: str) -> Dict:
    """
    Remove a study's cached result from memory.

    Used by test harnesses and between batch runs to prevent stale data from a
    previous eval run affecting subsequent results. Safe to call even if the study
    is not cached (returns HTTP 404 in that case).

    Args:
        study_id: Study identifier (path parameter)

    Returns:
        JSON: {study_id, cleared: true}
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404,
                            detail=f"No cached results for study '{study_id}'.")
    del _result_cache[study_id]
    return {"study_id": study_id, "cleared": True}


@app.get("/")
async def root() -> Dict:
    """API root – returns service info and available endpoints."""
    return {
        "service": "D1 Risk Profile Eval API",
        "version": "1.0.0",
        "upload_ui": "/ui",
        "multi_product_gateway": "protocol_eval_hub/unified_eval_app.py (one port, all four evals)",
        "endpoints": [
            "GET  /ui",
            "POST /eval/upload-session",
            "GET  /health",
            "GET  /health/ready",
            "POST /eval/detect",
            "POST /eval/scenario1",
            "POST /eval/scenario2",
            "POST /eval/run",
            "POST /eval/batch",
            "POST /export/risk-profile-docx",
            "GET  /results/{study_id}",
            "GET  /results/{study_id}/scorecard",
            "GET  /results/{study_id}/failures",
            "GET  /results/{study_id}/review_list",
            "DELETE /results/{study_id}",
        ],
        "docs": "/docs",
    }


register_eval_upload_routes(app)
