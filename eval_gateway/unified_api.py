from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


ROOT = Path(__file__).resolve().parent
PFIZER_ROOT = Path(os.getenv("PFIZER_ROOT", str(ROOT.parent))).resolve()
BUNDLES_PARENT = Path(
    os.getenv("BUNDLES_PARENT", str(PFIZER_ROOT / "protocol_eval_hub" / "protocol_bundles"))
).resolve()
HUB_RUNNER = Path(
    os.getenv(
        "HUB_RUNNER",
        str(PFIZER_ROOT / "protocol_eval_hub" / "run_protocol_eval_bundle.py"),
    )
).resolve()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(ROOT / 'eval_gateway.db').as_posix()}")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret-at-least-32-bytes")
JWT_ALG = "HS256"
JWT_TTL_MIN = int(os.getenv("JWT_TTL_MINUTES", "720"))
APP_USER = os.getenv("APP_USERNAME", "admin")
APP_PASS = os.getenv("APP_PASSWORD", "admin123")


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    study_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    initiated_by: Mapped[str] = mapped_column(String(128))
    risk_mode: Mapped[str] = mapped_column(String(64), default="scenario1_forced")
    bundle_dir: Mapped[str] = mapped_column(Text)
    output_dir: Mapped[str] = mapped_column(Text)
    log_path: Mapped[str] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    metrics: Mapped[List["RunMetric"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[List["Artifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunMetric(Base):
    __tablename__ = "run_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    product: Mapped[str] = mapped_column(String(32), index=True)
    metric_key: Mapped[str] = mapped_column(String(128))
    metric_value: Mapped[str] = mapped_column(String(256))
    verdict: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    run: Mapped[Run] = relationship(back_populates="metrics")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    artifact_type: Mapped[str] = mapped_column(String(16), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)

    run: Mapped[Run] = relationship(back_populates="artifacts")


engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _db() -> Session:
    with SessionLocal() as session:
        yield session


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    expires_at: str


class RunCreateIn(BaseModel):
    study_id: str = Field(..., description="Protocol/study id, e.g., C4891002")


class RunOut(BaseModel):
    id: str
    study_id: str
    status: str
    initiated_by: str
    risk_mode: str
    bundle_dir: str
    output_dir: str
    log_path: str
    error_message: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


class ArtifactOut(BaseModel):
    id: int
    run_id: str
    relative_path: str
    artifact_type: str
    size_bytes: int


class MetricOut(BaseModel):
    product: str
    metric_key: str
    metric_value: str
    verdict: Optional[str]


class RunDetailOut(RunOut):
    metrics: List[MetricOut]
    artifacts: List[ArtifactOut]


bearer = HTTPBearer(auto_error=False)


def _issue_token(username: str) -> str:
    exp = _utc_now() + timedelta(minutes=JWT_TTL_MIN)
    payload = {"sub": username, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def _current_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> str:
    if not cred or not cred.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALG])
        return str(payload.get("sub") or "unknown")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}") from exc


def _artifact_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in {"json", "yaml", "yml", "docx", "png", "csv", "md", "txt"}:
        return ext
    return "other"


def _extract_metrics_from_json(study_id: str, output_dir: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    candidates = [
        output_dir / f"pipd_eval_{study_id}.json",
        output_dir / f"risk_profile_eval_{study_id}.json",
        output_dir / f"cmp_eval_{study_id}.json",
        output_dir / f"dmp_eval_{study_id}.json",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        product = p.stem.split("_eval_")[0]
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        verdict = str(j.get("verdict") or j.get("go_no_go") or j.get("overall_pass") or "")
        if product == "pipd":
            s1 = (j.get("scenario1_evaluation") or {}).get("metrics", {}).get("m1_subcategory_recall", {})
            if not s1:
                s1 = (j.get("metrics") or {}).get("m1_subcategory_recall", {})
            score = s1.get("score")
            rows.append(
                {
                    "product": "pipd",
                    "metric_key": "m1_subcategory_recall",
                    "metric_value": f"{score:.4f}" if isinstance(score, (int, float)) else str(score),
                    "verdict": verdict or str(s1.get("pass")),
                }
            )
        elif product == "risk_profile":
            m1 = (j.get("metrics") or {}).get("m1_risk_name_recall", {})
            score = m1.get("score")
            rows.append(
                {
                    "product": "risk",
                    "metric_key": "m1_risk_name_recall",
                    "metric_value": f"{score:.4f}" if isinstance(score, (int, float)) else str(score),
                    "verdict": verdict or str(m1.get("passed")),
                }
            )
        elif product == "cmp":
            ds = j.get("document_score")
            rows.append(
                {
                    "product": "cmp",
                    "metric_key": "document_score",
                    "metric_value": str(ds),
                    "verdict": verdict,
                }
            )
        elif product == "dmp":
            ds = j.get("document_score")
            rows.append(
                {
                    "product": "dmp",
                    "metric_key": "document_score",
                    "metric_value": str(ds),
                    "verdict": verdict,
                }
            )
    return rows


def _index_run_outputs(db: Session, run: Run) -> None:
    out_dir = Path(run.output_dir)
    if not out_dir.exists():
        return
    db.query(Artifact).filter(Artifact.run_id == run.id).delete()
    db.query(RunMetric).filter(RunMetric.run_id == run.id).delete()
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(out_dir)).replace("\\", "/")
            db.add(
                Artifact(
                    run_id=run.id,
                    relative_path=rel,
                    artifact_type=_artifact_type(p),
                    size_bytes=p.stat().st_size,
                )
            )
    for row in _extract_metrics_from_json(run.study_id, out_dir):
        db.add(
            RunMetric(
                run_id=run.id,
                product=row["product"],
                metric_key=row["metric_key"],
                metric_value=row["metric_value"],
                verdict=row.get("verdict") or None,
            )
        )


class JobRunner:
    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="eval-gateway-worker", daemon=True)
        self._thread.start()

    def enqueue(self, run_id: str) -> None:
        self._q.put(run_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_one(run_id)
            finally:
                self._q.task_done()

    def _run_one(self, run_id: str) -> None:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run:
                return
            run.status = "running"
            run.started_at = _utc_now()
            run.error_message = None
            db.commit()

            cmd = [
                os.sys.executable,
                str(HUB_RUNNER),
                "--bundle",
                run.bundle_dir,
            ]
            log_path = Path(run.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as lf:
                proc = subprocess.run(
                    cmd,
                    cwd=str(HUB_RUNNER.parent),
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    check=False,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )

            run.finished_at = _utc_now()
            if proc.returncode == 0:
                run.status = "completed"
            else:
                run.status = "completed_with_issues"
                run.error_message = f"Runner exit code {proc.returncode}. See log."
            _index_run_outputs(db, run)
            db.commit()


job_runner = JobRunner()

app = FastAPI(
    title="Pfizer Unified Eval Gateway",
    version="1.0.0",
    description=(
        "Unified run orchestration + dashboard backend for CMP, DMP, PIPD, and Risk evaluations."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_to_out(r: Run) -> RunOut:
    return RunOut(
        id=r.id,
        study_id=r.study_id,
        status=r.status,
        initiated_by=r.initiated_by,
        risk_mode=r.risk_mode,
        bundle_dir=r.bundle_dir,
        output_dir=r.output_dir,
        log_path=r.log_path,
        error_message=r.error_message,
        started_at=_as_iso(r.started_at),
        finished_at=_as_iso(r.finished_at),
    )


@app.on_event("startup")
def _startup() -> None:
    Base.metadata.create_all(bind=engine)
    job_runner.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    job_runner.stop()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "bundles_parent": str(BUNDLES_PARENT),
        "hub_runner": str(HUB_RUNNER),
        "risk_mode": "scenario1_forced",
    }


@app.post("/auth/login", response_model=LoginOut)
def login(payload: LoginIn) -> LoginOut:
    if payload.username != APP_USER or payload.password != APP_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    exp = _utc_now() + timedelta(minutes=JWT_TTL_MIN)
    return LoginOut(token=_issue_token(payload.username), expires_at=exp.isoformat())


@app.get("/protocols")
def list_protocols(user: str = Depends(_current_user)) -> Dict[str, Any]:
    if not BUNDLES_PARENT.is_dir():
        raise HTTPException(status_code=500, detail=f"Bundles directory not found: {BUNDLES_PARENT}")
    studies = [
        p.name
        for p in sorted(BUNDLES_PARENT.iterdir())
        if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
    ]
    return {"count": len(studies), "protocols": studies, "requested_by": user}


@app.post("/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def create_run(payload: RunCreateIn, db: Session = Depends(_db), user: str = Depends(_current_user)) -> RunOut:
    study_id = payload.study_id.strip().upper()
    bundle = (BUNDLES_PARENT / study_id).resolve()
    if not bundle.is_dir():
        raise HTTPException(status_code=404, detail=f"Bundle not found: {bundle}")
    out_dir = bundle / "eval_outputs" / study_id
    logs_dir = ROOT / "run_logs"
    run_id = str(uuid.uuid4())
    run = Run(
        id=run_id,
        study_id=study_id,
        status="queued",
        initiated_by=user,
        risk_mode="scenario1_forced",
        bundle_dir=str(bundle),
        output_dir=str(out_dir),
        log_path=str((logs_dir / f"{run_id}.log").resolve()),
        started_at=_utc_now(),
    )
    db.add(run)
    db.commit()
    job_runner.enqueue(run_id)
    return _run_to_out(run)


@app.get("/runs", response_model=List[RunOut])
def list_runs(
    limit: int = 50,
    study_id: Optional[str] = None,
    db: Session = Depends(_db),
    user: str = Depends(_current_user),
) -> List[RunOut]:
    stmt = select(Run).order_by(Run.started_at.desc()).limit(max(1, min(limit, 500)))
    if study_id:
        stmt = stmt.where(Run.study_id == study_id.strip().upper())
    runs = db.scalars(stmt).all()
    return [_run_to_out(r) for r in runs]


@app.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run_id: str, db: Session = Depends(_db), user: str = Depends(_current_user)) -> RunDetailOut:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    metrics = db.scalars(select(RunMetric).where(RunMetric.run_id == run_id).order_by(RunMetric.product)).all()
    artifacts = db.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.relative_path)).all()
    base = _run_to_out(run).model_dump()
    return RunDetailOut(
        **base,
        metrics=[
            MetricOut(
                product=m.product,
                metric_key=m.metric_key,
                metric_value=m.metric_value,
                verdict=m.verdict,
            )
            for m in metrics
        ],
        artifacts=[
            ArtifactOut(
                id=a.id,
                run_id=a.run_id,
                relative_path=a.relative_path,
                artifact_type=a.artifact_type,
                size_bytes=a.size_bytes,
            )
            for a in artifacts
        ],
    )


@app.get("/runs/{run_id}/artifacts", response_model=List[ArtifactOut])
def list_artifacts(run_id: str, db: Session = Depends(_db), user: str = Depends(_current_user)) -> List[ArtifactOut]:
    rows = db.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.relative_path)).all()
    return [
        ArtifactOut(
            id=a.id,
            run_id=a.run_id,
            relative_path=a.relative_path,
            artifact_type=a.artifact_type,
            size_bytes=a.size_bytes,
        )
        for a in rows
    ]


@app.get("/runs/{run_id}/artifact/{artifact_id}")
def download_artifact(
    run_id: str,
    artifact_id: int,
    db: Session = Depends(_db),
    user: str = Depends(_current_user),
) -> FileResponse:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    art = db.get(Artifact, artifact_id)
    if not art or art.run_id != run_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    p = Path(run.output_dir) / art.relative_path
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact file missing: {p}")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@app.get("/runs/{run_id}/log")
def run_log(run_id: str, db: Session = Depends(_db), user: str = Depends(_current_user)) -> Dict[str, Any]:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    p = Path(run.log_path)
    if not p.is_file():
        return {"run_id": run_id, "log": "", "note": "Log not created yet"}
    return {"run_id": run_id, "log": p.read_text(encoding="utf-8", errors="replace")}
