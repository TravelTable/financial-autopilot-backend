import os
import logging
import time
from pathlib import Path
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from sqlalchemy import text
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.routers import (
    auth,
    sync,
    transactions,
    subscriptions,
    refunds,
    privacy,
    analytics,
    notifications,
    debug,  # ✅ debug router
)

# --- DATABASE ---
import app.models  # noqa: F401  # ensures models are registered
from app.config import settings
from app.db import engine
from app.rate_limit import limiter

from alembic import command
from alembic.config import Config

# ---------------- Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    force=True,  # important on Railway/uvicorn
)

logger = logging.getLogger("app.main")

MIGRATIONS_ON_STARTUP = os.getenv("MIGRATIONS_ON_STARTUP", "true").lower() in {"1", "true", "yes"}
MIGRATION_LOCK_ID = int(os.getenv("MIGRATION_LOCK_ID", "4815162342"))

app = FastAPI(
    title="Financial Autopilot Backend",
    version="0.2.0",
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# Simple request logger (helps confirm which service is serving what)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s (%.2fms)",
        request.method,
        request.url.path,
        getattr(response, "status_code", "?"),
        elapsed_ms,
    )
    return response


@app.exception_handler(RateLimitExceeded)
def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"code": "rate_limited", "message": "Too many requests", "details": exc.detail},
    )


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Invalid request",
            "details": exc.errors(),
        },
    )


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, str(exc))
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "Internal Server Error", "details": None},
    )


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": "http_error",
            "message": exc.detail if isinstance(exc.detail, str) else "Request failed",
            "details": exc.detail if not isinstance(exc.detail, str) else None,
        },
    )


# --- CREATE TABLES ON STARTUP ---
@app.on_event("startup")
def on_startup():
    FastAPICache.init(InMemoryBackend(), prefix="fastapi-cache")
    if not MIGRATIONS_ON_STARTUP:
        logger.info("startup: migrations disabled")
        return

    with engine.connect() as connection:
        lock_acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": MIGRATION_LOCK_ID},
        ).scalar()

        if not lock_acquired:
            logger.info("startup: migrations skipped (another instance holds lock)")
            return

        try:
            logger.info("startup: running database migrations")
            alembic_cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
            alembic_cfg.set_main_option(
                "sqlalchemy.url",
                settings.DATABASE_URL.replace("postgresql+psycopg2", "postgresql"),
            )
            command.upgrade(alembic_cfg, "head")
            logger.info("startup: migrations complete")
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )


# --- Core ---
app.include_router(auth.router)
app.include_router(sync.router)

# --- Data ---
app.include_router(transactions.router)
app.include_router(subscriptions.router)

# --- Intelligence ---
app.include_router(analytics.router)
app.include_router(notifications.router)

# --- Automation ---
app.include_router(refunds.router)

# --- Trust & Privacy ---
app.include_router(privacy.router)

# --- Debug ---
app.include_router(debug.router)

# --- Versioned API ---
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth.router)
v1_router.include_router(sync.router)
v1_router.include_router(transactions.router)
v1_router.include_router(subscriptions.router)
v1_router.include_router(analytics.router)
v1_router.include_router(notifications.router)
v1_router.include_router(refunds.router)
v1_router.include_router(privacy.router)
v1_router.include_router(debug.router)
app.include_router(v1_router)


@app.get("/health", tags=["system"])
def health():
    return {
        "ok": True,
        "service": "financial-autopilot-backend",
        "version": "0.2.0",
    }


@app.get("/readiness", tags=["system"])
def readiness():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False})
