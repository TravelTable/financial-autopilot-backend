from fastapi import FastAPI
import logging
import sys
import os

from app.routers import (
    auth,
    sync,
    transactions,
    subscriptions,
    refunds,
    privacy,
    analytics,
    notifications,
    debug,
)

# --- DATABASE ---
from app.db import engine
from app.models import Base  # Base = declarative_base()
import app.models  # IMPORTANT: ensures all models (User, etc.) are registered


def configure_logging() -> None:
    """
    Railway-friendly logging:
    - logs to stdout
    - unbuffered / immediate visibility
    - respects LOG_LEVEL env var
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )


configure_logging()
logger = logging.getLogger("app.main")

app = FastAPI(
    title="Financial Autopilot Backend",
    version="0.2.0",
)


# --- CREATE TABLES ON STARTUP ---
@app.on_event("startup")
def on_startup():
    """
    Ensures all database tables exist.
    Safe to run multiple times.
    Fixes: psycopg2.errors.UndefinedTable
    """
    logger.info("startup: creating tables if needed")
    Base.metadata.create_all(bind=engine)
    logger.info("startup: tables ensured")


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


@app.get("/debug/logtest", tags=["debug"])
def logtest():
    """
    Hit this endpoint to confirm logs show up in the *web service* logs.
    """
    logger.info("debug/logtest hit ✅")
    return {"ok": True, "logged": True}
