"""WorkGuard FastAPI application."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.config import settings
from backend.db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.workspace_auth and not settings.workspace_token_secret:
        raise RuntimeError(
            "WORKGUARD_WORKSPACE_TOKEN_SECRET or WORKGUARD_API_KEY is required "
            "when WORKGUARD_WORKSPACE_AUTH=1"
        )
    init_db()
    if settings.task_worker_enabled:
        from backend.services.jobs import start_worker

        start_worker()
    try:
        yield
    finally:
        if settings.task_worker_enabled:
            from backend.services.jobs import stop_worker

            stop_worker()
        from backend.graph.workflow import close_checkpointer

        close_checkpointer()


app = FastAPI(
    title="WorkGuard",
    description="A Verifiable Agent for Cross-Artifact Consistency and Change Impact "
    "Analysis (MVP: date-change chain)",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Workspace-Key"],
)


@app.middleware("http")
async def protect_api(request: Request, call_next):
    """Optional API-key boundary for shared deployments.

    The Feishu callback authenticates with its verification token and cannot
    attach this application-specific header, so it remains on its own verifier.
    """
    path = request.url.path.rstrip("/")
    protected = path.startswith("/api/") and path != "/api/integrations/feishu/webhook"
    if protected and not settings.api_key and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin", "")
        if origin and "*" not in settings.cors_origins and origin not in settings.cors_origins:
            return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
    if protected and settings.api_key:
        supplied = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, settings.api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid API key"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

app.include_router(router)

_ROOT = Path(__file__).resolve().parent.parent
app.mount("/app", StaticFiles(directory=_ROOT / "frontend", html=True), name="frontend")
app.mount(
    "/demo-assets",
    StaticFiles(directory=_ROOT / "demo" / "workspace_alpha"),
    name="demo-assets",
)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/app/")


@app.get("/health")
def health():
    return {"status": "ok", "service": "workguard"}
