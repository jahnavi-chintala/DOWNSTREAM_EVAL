"""CMP evaluation API — health + browser upload UI (USDM + CMP JSON)."""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from eval_upload_routes import register_eval_upload_routes

app = FastAPI(
    title="CMP Eval API",
    description="Clinical Monitoring Plan generator evaluation",
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
    return {"status": "ok", "service": "cmp-eval", "ts": datetime.utcnow().isoformat() + "Z"}


@app.get("/")
def root():
    return {"service": "CMP Eval API", "upload_ui": "/ui", "docs": "/docs"}


register_eval_upload_routes(app)
