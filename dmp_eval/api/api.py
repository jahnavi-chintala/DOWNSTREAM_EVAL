"""DMP evaluation API — health + browser upload UI (USDM + DMP JSON)."""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import register_eval_upload_routes

app = FastAPI(
    title="DMP Eval API",
    description="Data Management Plan generator evaluation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "dmp-eval", "ts": datetime.utcnow().isoformat() + "Z"}


@app.get("/")
def root():
    return {"service": "DMP Eval API", "upload_ui": "/ui", "docs": "/docs"}


register_eval_upload_routes(app)
