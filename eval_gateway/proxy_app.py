"""
Pfizer Eval Gateway — single public URL for PIPD (D2) and Risk Profile (D1) APIs.

Proxies:
  /pipd/*         → PIPD eval service (default http://127.0.0.1:8001)
  /risk-profile/* → Risk Profile eval service (default http://127.0.0.1:8002)

Start backends first, then:
  uvicorn proxy_app:app --host 0.0.0.0 --port 9000

Or use run_stack.py to start all three processes.
"""

from __future__ import annotations

import os
from typing import Iterable, Tuple

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

PIP_BASE = os.getenv("PIPD_BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
RISK_BASE = os.getenv("RISK_PROFILE_BACKEND_URL", "http://127.0.0.1:8002").rstrip("/")

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _forward_headers(scope_headers: Iterable[Tuple[bytes, bytes]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_k, raw_v in scope_headers:
        k = raw_k.decode("latin-1").lower()
        if k in HOP_BY_HOP:
            continue
        out[raw_k.decode("latin-1")] = raw_v.decode("latin-1")
    return out


app = FastAPI(
    title="Pfizer Eval API Gateway",
    description=(
        "Single entry point for integration. Routes: `/pipd` (PIPD eval), "
        "`/risk-profile` (Risk Profile eval). Backends must be running on "
        f"{PIP_BASE} and {RISK_BASE} unless overridden by env vars."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Gateway"])
async def gateway_health() -> dict:
    return {
        "status": "healthy",
        "gateway": "pfizer-eval-gateway",
        "routes": {
            "pipd": f"{PIP_BASE} (path prefix /pipd)",
            "risk_profile": f"{RISK_BASE} (path prefix /risk-profile)",
        },
    }


async def _proxy(request: Request, base: str, path: str) -> Response:
    target = f"{base}/{path}" if path else base
    q = request.query_params
    if q:
        target = f"{target}?{q}"

    body = await request.body()
    hdrs = _forward_headers(request.scope.get("headers", []))

    timeout = httpx.Timeout(600.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.request(
                request.method,
                target,
                content=body if body else None,
                headers=hdrs,
            )
        except httpx.ConnectError as e:
            return Response(
                content=f'{{"detail":"Backend unreachable: {e!s}"}}',
                status_code=502,
                media_type="application/json",
            )

    out_headers = {
        k: v
        for k, v in r.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "content-length"
    }
    return Response(content=r.content, status_code=r.status_code, headers=out_headers)


@app.api_route(
    "/pipd/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def pipd_proxy(request: Request, full_path: str) -> Response:
    return await _proxy(request, PIP_BASE, full_path)


@app.api_route(
    "/pipd",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def pipd_proxy_root(request: Request) -> Response:
    return await _proxy(request, PIP_BASE, "")


@app.api_route(
    "/risk-profile/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def risk_proxy(request: Request, full_path: str) -> Response:
    return await _proxy(request, RISK_BASE, full_path)


@app.api_route(
    "/risk-profile",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def risk_proxy_root(request: Request) -> Response:
    return await _proxy(request, RISK_BASE, "")
