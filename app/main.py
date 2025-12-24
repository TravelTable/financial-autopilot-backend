import os
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

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

# Simple request logger (helps confirm which service is serving what)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        response = await call_next(request)
        logger.info("%s %s -> %s", request.method, request.url.path, getattr(response, "status_code", "?"))
        return response
    except Exception as e:
        logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


# --- CREATE TABLES ON STARTUP ---
@app.on_event("startup")
def on_startup():
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


@app.get("/health", tags=["system"])
def health():
    return {
        "ok": True,
        "service": "financial-autopilot-backend",
        "version": "0.2.0",
    }
