"""
api.py
------
PIPD Eval Framework – FastAPI Microservice.

Exposes all evaluation functionality as RESTful HTTP endpoints so this
eval framework can be consumed by other pipeline microservices (generator
orchestrator, reporting service, dashboard, etc.).

All endpoints accept JSON bodies and return JSON responses.
Heavy eval operations (batch) are run synchronously; add a task queue
(Celery / ARQ) for async execution if runtimes grow beyond 2 min.

Start server:
    python -m uvicorn api:app --host 0.0.0.0 --port 8000 --reload
    On Windows use this form if the bare uvicorn command is not found.

Environment variables (optional, override via body params too):
    GROUND_TRUTH_CSV     – default path to pipd_ground_truth_clean.csv
    DEVIATION_BENCHMARKS – default path to deviation_subcategories_clean.csv
    GENERATOR_OUTPUT_DIR – default dir containing {study_id}_PIPD.json files
    OUTPUT_DIR           – default dir for output files

API groups:
  /health            – liveness / readiness
  /eval/detect       – scenario detection only
  /eval/scenario1    – Scenario 1 eval (ground truth)
  /eval/scenario1/report – Scenario 1 reference Markdown / JSON / YAML / Word (no composite)
  /eval/scenario2    – Scenario 2 eval (proxy signals)
  /eval/run          – Unified auto-detect eval
  /eval/batch        – Batch all 8 verify studies
  /eval/composite    – Weighted 0–100% score + Markdown / optional .docx report files
  /results/{study_id} – Retrieve stored results

Integration (USDM + PIPD from other services):
  This API does **not** accept multi‑MB JSON in HTTP bodies. Upstream services should
  write **USDM protocol JSON** and **PIPD generator JSON** (and ground-truth CSV) to a
  **shared path** the eval service can read, then call ``POST /eval/composite`` with
  ``generator_json_path``, ``ground_truth_csv``, optional ``usdm_json_path``, and
  ``write_docx: true``. The response includes structured **content** scores (composite +
  Scenario 1) and **pipd_structure** (schema completeness). USDM alignment is verified
  by resolving ``usdm_entity_id`` / types against the protocol file (see project docs)—
  the Word document is generated from that Markdown; it does not call OpenAI unless
  ``with_openai_report`` or ``generate_ai_doc`` is enabled.

  ``GET /health/openai`` – probe whether ``OPENAI_API_KEY`` is set and can reach OpenAI.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import os
import sys
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks   # HTTP framework
from fastapi.middleware.cors import CORSMiddleware             # Cross-origin for UI clients
from pydantic import BaseModel, Field                          # Request/response validation

# ── Internal modules ──────────────────────────────────────────────────────────
_pkg_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_pkg_root))
load_dotenv(_pkg_root / ".env")
from core.eval_scenario1 import run_scenario1_eval, classify_failures
from core.eval_scenario2 import run_scenario2_eval
from scripts.run_eval import detect_scenario, run_eval, save_results_json
from scripts.run_all_verify import run_all_verify
from reports.pipd_composite_report import write_composite_report_files
from api.routes import register_eval_upload_routes


# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title        = "PIPD Eval Framework API",
    description  = "D2 PIPD Evaluation Microservice – Pfizer DDF Hackathon",
    version      = "1.0.0",
    docs_url     = "/docs",
    redoc_url    = "/redoc",
)

# Allow all origins in development; restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# In-memory result cache { study_id: results_dict }
# Replace with Redis or a DB for production multi-worker deployments
_result_cache: Dict[str, Dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_GROUND_TRUTH     = os.getenv("GROUND_TRUTH_CSV",     "pipd_ground_truth_clean.csv")
DEFAULT_BENCHMARKS       = os.getenv("DEVIATION_BENCHMARKS", "deviation_subcategories_clean.csv")
DEFAULT_GENERATOR_DIR    = os.getenv("GENERATOR_OUTPUT_DIR", "./generator_outputs")
DEFAULT_OUTPUT_DIR       = os.getenv("OUTPUT_DIR",           "./eval_outputs")


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC REQUEST / RESPONSE MODELS
# ─────────────────────────────────────────────────────────────────────────────

class EvalRequest(BaseModel):
    """Shared fields for single-study evaluation requests."""
    study_id: str = Field(..., description="Study identifier, e.g. 'B7981027'")
    generator_json_path: str = Field(
        ..., description="Filesystem path to {study_id}_PIPD.json"
    )
    ground_truth_csv: str = Field(
        DEFAULT_GROUND_TRUTH,
        description="Path to pipd_ground_truth_clean.csv",
    )
    deviation_benchmarks: str = Field(
        DEFAULT_BENCHMARKS,
        description="Path to deviation_subcategories_clean.csv",
    )
    output_json: Optional[str] = Field(
        None,
        description="Optional path to persist results JSON; auto-generated if omitted",
    )


class Scenario1Request(EvalRequest):
    """Request body for explicit Scenario 1 (ground truth) evaluation."""
    pass


class Scenario1ReportRequest(BaseModel):
    """Reference-style Markdown + Word for Scenario 1 only (no weighted composite run)."""
    study_id: str = Field(..., description="Study identifier")
    generator_json_path: str = Field(..., description="Path to {study_id}_PIPD.json")
    ground_truth_csv: str = Field(
        DEFAULT_GROUND_TRUTH,
        description="Path to pipd ground truth CSV",
    )
    output_dir: Optional[str] = Field(
        DEFAULT_OUTPUT_DIR,
        description="Directory for Markdown, JSON, combined YAML, optional .docx",
    )
    write_docx: bool = Field(True, description="Write PIPD_Eval_Report_{study_id}.docx")
    usdm_json_path: Optional[str] = Field(
        None,
        description="Optional USDM JSON (else PIPD_USDM_JSON / data/ discovery)",
    )
    structure_check: bool = Field(True, description="Include pipd_structure in JSON")
    with_openai_report: bool = Field(
        False,
        description="OpenAI executive summary if OPENAI_API_KEY is set (after body when reference layout is on)",
    )
    openai_reference_layout: bool = Field(
        False,
        description="Rewrite report Markdown/Word to mirror reference_spec/PIPD_Eval_Report_B7981027.docx (OpenAI; metrics from evaluator)",
    )
    openai_enrich_sources: bool = Field(
        False,
        description="Append AI section from USDM + ground-truth excerpts (OPENAI_API_KEY; does not alter metric tables)",
    )
    artifacts: str = Field(
        "all",
        description="`all` (default) or `json_docx` — json_docx skips Markdown sidecars but still writes json + yaml + docx",
    )


class Scenario2Request(BaseModel):
    """Request body for explicit Scenario 2 (proxy signal) evaluation."""
    study_id: str = Field(..., description="Study identifier")
    generator_json_path: str = Field(..., description="Path to {study_id}_PIPD.json")
    deviation_benchmarks: str = Field(
        DEFAULT_BENCHMARKS,
        description="Path to deviation_subcategories_clean.csv",
    )
    output_json: Optional[str] = Field(None, description="Optional output path")
    usdm_json_path: Optional[str] = Field(
        None,
        description="Optional USDM protocol JSON; else PIPD_USDM_JSON or data/ auto-discovery",
    )


class UnifiedEvalRequest(EvalRequest):
    """Request body for the unified auto-detect endpoint."""
    output_dir: Optional[str] = Field(
        DEFAULT_OUTPUT_DIR,
        description="Directory for auxiliary outputs (near_misses, hallucination_report)",
    )


class BatchRequest(BaseModel):
    """Request body for batch verification across all 8 verify studies."""
    generator_output_dir: str = Field(
        DEFAULT_GENERATOR_DIR,
        description="Directory containing {study_id}_PIPD.json files",
    )
    ground_truth_csv: str = Field(
        DEFAULT_GROUND_TRUTH,
        description="Path to pipd_ground_truth_clean.csv",
    )
    deviation_benchmarks: str = Field(
        DEFAULT_BENCHMARKS,
        description="Path to deviation_subcategories_clean.csv",
    )
    output_dir: str = Field(
        DEFAULT_OUTPUT_DIR,
        description="Directory for all output files",
    )


class DetectRequest(BaseModel):
    """Request body for scenario detection only."""
    study_id: str = Field(..., description="Study identifier")
    ground_truth_csv: str = Field(
        DEFAULT_GROUND_TRUTH,
        description="Path to pipd_ground_truth_clean.csv",
    )


class CompositeEvalRequest(BaseModel):
    """Weighted completeness / accuracy / semantic / hallucination composite (Scenario-1-style GT)."""
    study_id: str = Field(..., description="Study identifier")
    generator_json_path: str = Field(..., description="Path to {study_id}_PIPD.json")
    ground_truth_csv: str = Field(
        DEFAULT_GROUND_TRUTH,
        description="Path to pipd ground truth CSV",
    )
    output_dir: Optional[str] = Field(
        DEFAULT_OUTPUT_DIR,
        description="Directory for composite JSON, Markdown, and optional .docx",
    )
    use_bertscore: bool = Field(True, description="Use BERTScore if bert-score is installed")
    with_openai_report: bool = Field(
        False,
        description="Prepend OpenAI narrative if OPENAI_API_KEY is set (from .env or env)",
    )
    generate_ai_doc: bool = Field(
        False,
        description="Write {study_id}_pipd_ai_document.md using prompts/ai_doc_*.txt",
    )
    write_docx: bool = Field(
        True,
        description="Also write .docx next to each Markdown report (requires python-docx)",
    )
    include_scenario1_eval: bool = Field(
        True,
        description="Embed full Scenario 1 eval (M1–M4, verdict, failures) in report and composite JSON",
    )
    usdm_json_path: Optional[str] = Field(
        None,
        description="Path to USDM protocol JSON for provenance + intelligence-truth (else PIPD_USDM_JSON / data/)",
    )
    structure_check: bool = Field(
        True,
        description="Include pipd_structure completeness (categories, recommended subcat fields) in composite JSON",
    )


class EvalResponse(BaseModel):
    """Standard envelope wrapping evaluation results."""
    status: str          = "success"
    study_id: str
    scenario: int
    go_no_go: str
    eval_date: str
    results: Dict[str, Any]
    classified_failures: Optional[List[Dict]] = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPER UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_output_path(study_id: str, requested: Optional[str]) -> str:
    """
    Resolve the output JSON path for a study.

    If the caller provided an explicit path, use it.  Otherwise auto-generate
    a timestamped path in DEFAULT_OUTPUT_DIR.

    Args:
        study_id  : Study identifier
        requested : Caller-provided output path (may be None)

    Returns:
        Resolved filesystem path string
    """
    if requested:
        return requested
    out_dir = Path(DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(out_dir / f"{study_id}_{ts}_results.json")


def _validate_file_exists(path: str, label: str) -> None:
    """
    Raise HTTP 422 if a required input file does not exist.

    Args:
        path  : Filesystem path to check
        label : Human-readable file description for error message
    """
    if not Path(path).exists():
        raise HTTPException(
            status_code = 422,
            detail      = f"{label} not found at path: '{path}'",
        )


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Health"])
def health_check() -> Dict:
    """
    Liveness check – confirms the API process is running.

    Returns:
        { status: "healthy", timestamp: ISO8601 }
    """
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/health/openai", tags=["Health"])
def openai_connectivity_check() -> Dict:
    """
    Check whether ``OPENAI_API_KEY`` is configured and the OpenAI API is reachable.

    Does not echo the key. Uses a lightweight ``GET /v1/models`` request.
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return {
            "configured": False,
            "reachable": None,
            "message": "OPENAI_API_KEY is empty — set it in .env for AI narrative / ai_doc.",
        }
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = int(resp.status)
        ok = 200 <= status < 300
        return {
            "configured": True,
            "reachable": ok,
            "http_status": status,
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "error": str(exc),
        }


@app.get("/health/ready", tags=["Health"])
def readiness_check() -> Dict:
    """
    Readiness check – verifies that default data files are accessible.

    Returns:
        { status: "ready"|"not_ready", checks: { file: bool } }
    """
    checks = {
        "ground_truth_csv":     Path(DEFAULT_GROUND_TRUTH).exists(),
        "deviation_benchmarks": Path(DEFAULT_BENCHMARKS).exists(),
        "generator_output_dir": Path(DEFAULT_GENERATOR_DIR).is_dir(),
    }
    ready = all(checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO DETECTION
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/detect", tags=["Evaluation"])
def detect_eval_scenario(req: DetectRequest) -> Dict:
    """
    Detect which evaluation scenario applies to a study without running eval.

    Returns:
        { study_id, scenario (1 or 2), description }
    """
    scenario = detect_scenario(req.study_id, req.ground_truth_csv)
    return {
        "study_id":    req.study_id,
        "scenario":    scenario,
        "description": (
            "Scenario 1 – ground truth available (verify set)"
            if scenario == 1 else
            "Scenario 2 – no ground truth, proxy quality signals only"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 1 ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/scenario1", response_model=EvalResponse, tags=["Evaluation"])
def eval_scenario1(req: Scenario1Request) -> EvalResponse:
    """
    Run Scenario 1 evaluation (ground truth available).

    Computes all 4 metrics (M1–M4) using exact string matching against
    pipd_ground_truth_clean.csv.  Returns structured results and classified
    failures for the generator developer.

    Args (body):
        study_id            : Study identifier e.g. 'B7981027'
        generator_json_path : Path to {study_id}_PIPD.json
        ground_truth_csv    : Path to pipd_ground_truth_clean.csv
        deviation_benchmarks: (not used in S1 but accepted for API consistency)
        output_json         : Optional path to persist results

    Returns:
        EvalResponse with full results dict and classified_failures list
    """
    _validate_file_exists(req.generator_json_path, "Generator JSON")
    _validate_file_exists(req.ground_truth_csv,    "Ground truth CSV")

    try:
        results = run_scenario1_eval(
            generator_json_path   = req.generator_json_path,
            ground_truth_csv_path = req.ground_truth_csv,
            study_id              = req.study_id,
        )
        failures = classify_failures(results)
        results["classified_failures"] = failures

        # Cache for retrieval
        _result_cache[req.study_id] = results

        # Optionally persist
        out_path = _resolve_output_path(req.study_id, req.output_json)
        save_results_json(results, out_path)

        return EvalResponse(
            status               = "success",
            study_id             = req.study_id,
            scenario             = 1,
            go_no_go             = results["go_no_go"],
            eval_date            = results["eval_date"],
            results              = results,
            classified_failures  = failures,
        )

    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Eval failed: {exc}")


@app.post("/eval/scenario1/report", tags=["Evaluation"])
def eval_scenario1_report(req: Scenario1ReportRequest) -> Dict:
    """
    Build the reference-layout eval report from **Scenario 1 only** (ground truth CSV +
    PIPD JSON). Writes ``pipd_eval_{study_id}.json/.yaml`` (YAML embeds
    ``pipd_eval_config.yaml`` + report payload), ``PIPD_Eval_Report_{study_id}.docx``, Markdown, and intelligence-truth
    sidecars. Does **not** run the weighted composite — the document
    score row shows M1 recall; use ``POST /eval/composite`` for the full 0–100 composite.
    """
    from reports.pipd_scenario1_report import write_scenario1_report_files

    _validate_file_exists(req.generator_json_path, "Generator JSON")
    _validate_file_exists(req.ground_truth_csv, "Ground truth CSV")
    if req.usdm_json_path:
        _validate_file_exists(req.usdm_json_path, "USDM JSON")

    out_dir = req.output_dir or DEFAULT_OUTPUT_DIR
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    _art = str(getattr(req, "artifacts", "all") or "all").strip().lower()
    if _art not in ("all", "json_docx"):
        _art = "all"

    try:
        result, paths = write_scenario1_report_files(
            req.generator_json_path,
            req.ground_truth_csv,
            req.study_id,
            out_dir,
            write_docx=req.write_docx,
            usdm_json_path=req.usdm_json_path,
            include_structure_check=req.structure_check,
            with_openai=req.with_openai_report,
            openai_reference_layout=req.openai_reference_layout,
            openai_enrich_sources=req.openai_enrich_sources,
            artifacts=_art,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Scenario 1 report failed: {exc}")

    s1 = result.get("scenario1_evaluation") or {}
    return {
        "status":      "success",
        "study_id":    req.study_id,
        "go_no_go":    s1.get("go_no_go", "UNKNOWN"),
        "eval_mode":   "scenario1_only",
        "output_files": paths,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 2 ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/scenario2", tags=["Evaluation"])
def eval_scenario2(req: Scenario2Request) -> Dict:
    """
    Run Scenario 2 evaluation (no ground truth, proxy quality signals).

    Runs 7 proxy signals on the generator JSON output.  Returns a GREEN /
    AMBER / RED overall verdict plus a mandatory human review list.

    This endpoint is the primary endpoint for the May 18 live test window.

    Args (body):
        study_id              : Study identifier
        generator_json_path   : Path to {study_id}_PIPD.json
        deviation_benchmarks  : Path to deviation_subcategories_clean.csv
        output_json           : Optional path to persist results

    Returns:
        { status, study_id, scenario, verdict, go_no_go, eval_date,
          results, human_review_count }
    """
    _validate_file_exists(req.generator_json_path,   "Generator JSON")
    _validate_file_exists(req.deviation_benchmarks,  "Deviation benchmarks CSV")

    try:
        results = run_scenario2_eval(
            generator_json_path = req.generator_json_path,
            benchmark_csv_path  = req.deviation_benchmarks,
            study_id            = req.study_id,
            usdm_json_path      = req.usdm_json_path,
        )

        _result_cache[req.study_id] = results
        out_path = _resolve_output_path(req.study_id, req.output_json)
        save_results_json(results, out_path)

        return {
            "status":             "success",
            "study_id":           req.study_id,
            "scenario":           2,
            "verdict":            results["overall_verdict"]["verdict"],
            "go_no_go":           results["go_no_go"],
            "eval_date":          results["eval_date"],
            "results":            results,
            "human_review_count": results["human_review_count"],
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Eval failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED AUTO-DETECT ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/run", tags=["Evaluation"])
def eval_run(req: UnifiedEvalRequest) -> Dict:
    """
    Unified evaluation endpoint – auto-detects scenario from ground truth CSV.

    Logic:
      If study_id found in ground_truth_csv (split=verify) → Scenario 1
      Otherwise → Scenario 2

    This is the recommended endpoint for all standard eval runs.

    Args (body):
        study_id            : Study identifier
        generator_json_path : Path to {study_id}_PIPD.json
        ground_truth_csv    : Path to pipd_ground_truth_clean.csv
        deviation_benchmarks: Path to deviation_subcategories_clean.csv
        output_json         : Optional path for main results JSON
        output_dir          : Optional dir for auxiliary outputs

    Returns:
        { status, study_id, scenario, go_no_go, eval_date, results }
    """
    _validate_file_exists(req.generator_json_path, "Generator JSON")

    out_path = _resolve_output_path(req.study_id, req.output_json)
    out_dir  = req.output_dir or str(Path(out_path).parent)

    try:
        results = run_eval(
            generator_json_path       = req.generator_json_path,
            ground_truth_csv_path     = req.ground_truth_csv,
            deviation_benchmarks_path = req.deviation_benchmarks,
            study_id                  = req.study_id,
            output_path               = out_path,
            output_dir                = out_dir,
        )

        _result_cache[req.study_id] = results
        scenario = results.get("scenario", 0)

        return {
            "status":    "success",
            "study_id":  req.study_id,
            "scenario":  scenario,
            "go_no_go":  results.get("go_no_go", "UNKNOWN"),
            "eval_date": results.get("eval_date", datetime.now().isoformat()),
            "results":   results,
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Eval failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# BATCH ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/eval/batch", tags=["Evaluation"])
def eval_batch(req: BatchRequest) -> Dict:
    """
    Batch evaluation across all 8 verify studies.

    Finds {study_id}_PIPD.json for each verify study in generator_output_dir,
    runs Scenario 1 on each, and writes all aggregate outputs to output_dir.

    This endpoint is synchronous.  For the May 18 window, all 8 studies
    should complete in < 2 minutes.

    Args (body):
        generator_output_dir : Dir containing {study_id}_PIPD.json files
        ground_truth_csv     : Path to pipd_ground_truth_clean.csv
        deviation_benchmarks : Path to deviation_subcategories_clean.csv
        output_dir           : Dir for all outputs

    Returns:
        { status, go_no_go, summary: eval_summary dict,
          output_dir, files_written: [str] }
    """
    _validate_file_exists(req.ground_truth_csv,    "Ground truth CSV")
    _validate_file_exists(req.deviation_benchmarks, "Deviation benchmarks CSV")

    if not Path(req.generator_output_dir).is_dir():
        raise HTTPException(
            status_code=422,
            detail=f"Generator output directory not found: '{req.generator_output_dir}'"
        )

    try:
        summary = run_all_verify(
            generator_output_dir      = req.generator_output_dir,
            ground_truth_csv_path     = req.ground_truth_csv,
            deviation_benchmarks_path = req.deviation_benchmarks,
            output_dir                = req.output_dir,
        )

        # List files written
        out_dir = Path(req.output_dir)
        files = [str(p) for p in out_dir.iterdir() if p.is_file()]

        return {
            "status":        "success",
            "go_no_go":      summary.get("go_no_go", "UNKNOWN"),
            "summary":       summary,
            "output_dir":    req.output_dir,
            "files_written": files,
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch eval failed: {exc}")


@app.post("/eval/composite", tags=["Evaluation"])
def eval_composite(req: CompositeEvalRequest) -> Dict:
    """
    Composite 0–100% score: completeness (40%), accuracy (30%), semantic (20%),
    hallucination penalty term (10%). Writes ``{study_id}_pipd_composite.json``,
    ``{study_id}_pipd_composite_report.md``, and by default ``{study_id}_pipd_composite_report.docx``
    under output_dir.

    Semantic similarity uses BERTScore when ``bert-score`` is installed; otherwise
    normalized Levenshtein.     The hallucination term counts **all** generated subcategories,
    including those under category numbers not present in the GT CSV for the study.
    By default the Markdown/JSON also include the full **Scenario 1** eval block
    (M1–M4, verdict, classified failures), matching ``run_eval.py`` output.
    Optional OpenAI narrative prepends the Markdown if ``with_openai_report`` is true
    and ``OPENAI_API_KEY`` is set.
    """
    _validate_file_exists(req.generator_json_path, "Generator JSON")
    _validate_file_exists(req.ground_truth_csv, "Ground truth CSV")
    if req.usdm_json_path:
        _validate_file_exists(req.usdm_json_path, "USDM JSON")

    out_dir = req.output_dir or DEFAULT_OUTPUT_DIR
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    try:
        result, out_files = write_composite_report_files(
            req.generator_json_path,
            req.ground_truth_csv,
            req.study_id,
            out_dir,
            use_bertscore=req.use_bertscore,
            with_openai=req.with_openai_report,
            generate_ai_doc=req.generate_ai_doc,
            write_docx=req.write_docx,
            include_scenario1_eval=req.include_scenario1_eval,
            usdm_json_path=req.usdm_json_path,
            include_structure_check=req.structure_check,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Composite eval failed: {exc}")

    return {
        "status":      "success",
        "study_id":    req.study_id,
        "composite":   result,
        "output_files": out_files,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS RETRIEVAL ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/results/{study_id}", tags=["Results"])
def get_results(study_id: str) -> Dict:
    """
    Retrieve cached evaluation results for a previously evaluated study.

    Results are cached in-memory per API process lifetime.  For persistence
    across restarts, pass an output_json path when calling /eval/run and
    read the file directly.

    Args:
        study_id : Study identifier (path parameter)

    Returns:
        Cached results dict or 404 if not found in this session
    """
    if study_id not in _result_cache:
        raise HTTPException(
            status_code=404,
            detail=f"No cached results for study_id='{study_id}' in this session. "
                   "Run an eval endpoint first, or load the results JSON file directly."
        )
    return {"status": "success", "study_id": study_id, "results": _result_cache[study_id]}


@app.get("/results/{study_id}/scorecard", tags=["Results"])
def get_scorecard(study_id: str) -> Dict:
    """
    Return a concise scorecard for a previously evaluated study.

    Extracts only the metric-level pass/fail summary – suitable for dashboards
    and downstream services that don't need the full results payload.

    Args:
        study_id : Study identifier (path parameter)

    Returns:
        { study_id, scenario, go_no_go, metrics: { M1: {score, pass}, ... } }
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404, detail=f"No cached results for '{study_id}'")

    res      = _result_cache[study_id]
    scenario = res.get("scenario", 0)

    if scenario == 1:
        m = res["metrics"]
        card = {
            "M1_recall":           {"score": m["m1_subcategory_recall"]["score"],     "pass": m["m1_subcategory_recall"]["pass"]},
            "M2_flag_accuracy":    {"score": m["m2_flag_accuracy"]["auto_confirmed_accuracy"], "pass": m["m2_flag_accuracy"]["pass"]},
            "M3_empty_category":   {"score": m["m3_empty_category_accuracy"]["score"], "pass": m["m3_empty_category_accuracy"]["pass"]},
            "M4_hallucinations":   {"count": m["m4_hallucination_detection"]["hallucinations_found"], "pass": m["m4_hallucination_detection"]["pass"]},
        }
    else:
        v    = res["overall_verdict"]
        card = {
            "verdict":     v["verdict"],
            "pass_count":  v["pass_count"],
            "warn_count":  v["warn_count"],
            "fail_count":  v["fail_count"],
            "notes":       v.get("remediation_notes", []),
        }

    return {
        "study_id":  study_id,
        "scenario":  scenario,
        "go_no_go":  res.get("go_no_go", "UNKNOWN"),
        "eval_date": res.get("eval_date"),
        "metrics":   card,
    }


@app.get("/results/{study_id}/failures", tags=["Results"])
def get_failures(study_id: str) -> Dict:
    """
    Return classified failures for a previously evaluated study.

    Only available for Scenario 1 results.  Returns the failure list
    grouped by type for direct use by the generator developer.

    Args:
        study_id : Study identifier (path parameter)

    Returns:
        { study_id, failures_by_type, total_failures, all_failures }
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404, detail=f"No cached results for '{study_id}'")

    res = _result_cache[study_id]
    if res.get("scenario") != 1:
        raise HTTPException(
            status_code=400,
            detail="Failure classification is only available for Scenario 1 results."
        )

    failures = res.get("classified_failures") or classify_failures(res)
    by_type: Dict[str, List] = {}
    for f in failures:
        by_type.setdefault(f["failure_type"], []).append(f)

    return {
        "study_id":       study_id,
        "total_failures": len(failures),
        "failures_by_type": by_type,
        "all_failures":   failures,
    }


@app.get("/results/{study_id}/review_list", tags=["Results"])
def get_review_list(study_id: str) -> Dict:
    """
    Return the human review list for a previously evaluated study.

    For Scenario 1: subcategories with wrong YES/NO flags or near misses.
    For Scenario 2: all review/low_confidence subcategories + all Cat 10.

    Args:
        study_id : Study identifier (path parameter)

    Returns:
        { study_id, review_count, review_items }
    """
    if study_id not in _result_cache:
        raise HTTPException(status_code=404, detail=f"No cached results for '{study_id}'")

    res = _result_cache[study_id]
    scenario = res.get("scenario", 0)

    if scenario == 2:
        items = res.get("human_review_list", [])
    else:
        # For S1, return YES/NO discrepancies + near misses as review items
        items = []
        for cat_num, cat in res.get("per_category", {}).items():
            for disc in cat.get("m2_discrepancies", []):
                items.append({
                    "category_num":     cat_num,
                    "subcategory_text": disc["subcategory_text"],
                    "reason": f"YES/NO mismatch: GT={disc['gt_flag']} Generated={disc['generated_flag']}",
                })
            for nm in res.get("near_misses", []):
                if nm.get("category_num") == cat_num:
                    items.append({
                        "category_num":     cat_num,
                        "subcategory_text": nm["generated_text"],
                        "reason": f"Near miss (distance={nm['distance']}) vs GT: '{nm['gt_text']}'",
                    })

    return {
        "study_id":     study_id,
        "review_count": len(items),
        "review_items": items,
    }


@app.delete("/results/{study_id}", tags=["Results"])
def clear_results(study_id: str) -> Dict:
    """
    Remove cached results for a study from in-memory cache.

    Useful for forcing a re-evaluation without restarting the service.

    Args:
        study_id : Study identifier (path parameter)

    Returns:
        { status, message }
    """
    if study_id in _result_cache:
        del _result_cache[study_id]
        return {"status": "success", "message": f"Cleared cached results for '{study_id}'"}
    return {"status": "success", "message": f"No cached results found for '{study_id}'"}


# ─────────────────────────────────────────────────────────────────────────────
# ROOT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root() -> Dict:
    """
    API root – returns service info and available endpoints.
    """
    return {
        "service":     "PIPD Eval Framework API",
        "version":     "1.0.0",
        "description": "D2 PIPD Evaluation Microservice – Pfizer DDF Hackathon",
        "docs":        "/docs",
        "upload_ui": "/ui",
        "endpoints": {
            "GET  /ui":                            "Browser upload: USDM + PIPD JSON",
            "POST /eval/upload-session":           "Multipart eval → session downloads",
            "GET  /health":                        "Liveness check",
            "GET  /health/openai":                 "OpenAI API key configured + reachable",
            "GET  /health/ready":                  "Readiness check (verifies input files)",
            "POST /eval/detect":                   "Detect which scenario applies to a study",
            "POST /eval/scenario1":                "Scenario 1 eval (ground truth available)",
            "POST /eval/scenario1/report":        "Scenario 1 reference report MD/JSON/YAML/.docx (no composite)",
            "POST /eval/scenario2":                "Scenario 2 eval (proxy signals, no ground truth)",
            "POST /eval/run":                      "Unified eval – auto-detects scenario",
            "POST /eval/batch":                    "Batch eval across all 8 verify studies",
            "POST /eval/composite":                "Weighted 0–100% + JSON/Markdown/.docx report",
            "GET  /results/{study_id}":            "Retrieve cached full results",
            "GET  /results/{study_id}/scorecard":  "Concise metric scorecard",
            "GET  /results/{study_id}/failures":   "Classified failures (Scenario 1 only)",
            "GET  /results/{study_id}/review_list":"Human review items",
            "DELETE /results/{study_id}":          "Clear cached results",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE RUNNER (for development)
# ─────────────────────────────────────────────────────────────────────────────

register_eval_upload_routes(app)


if __name__ == "__main__":
    import uvicorn   # ASGI server – install: pip install uvicorn --break-system-packages
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
