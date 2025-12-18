from fastapi import FastAPI
import logging
import sys
import os

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

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


def _ensure_bigint_internal_date_ms() -> None:
    """
    Gmail 'internalDate' is epoch milliseconds (e.g. 1766020775000),
    which overflows a 32-bit INTEGER. We must use BIGINT.
    This function safely upgrades the DB column type if needed.
    """
    try:
        with engine.begin() as conn:
            # Only attempt the alter if table/column exists.
            # If it's already BIGINT, Postgres will allow it or no-op depending on type.
            conn.execute(text("""
                ALTER TABLE emails_index
                ALTER COLUMN internal_date_ms TYPE BIGINT
                USING internal_date_ms::bigint
            """))
        logger.info("startup: ensured emails_index.internal_date_ms is BIGINT")
    except ProgrammingError as e:
        # Table might not exist yet on first run, or column might not exist if schema differs.
        logger.warning("startup: could not alter emails_index.internal_date_ms to BIGINT (%s)", str(e))
    except Exception as e:
        logger.exception("startup: failed ensuring BIGINT for internal_date_ms: %s", str(e))


# --- CREATE TABLES ON STARTUP ---
@app.on_event("startup")
def on_startup():
    """
    Ensures all database tables exist.
    Safe to run multiple times.
    """
    logger.info("startup: creating tables if needed")
    Base.metadata.create_all(bind=engine)
    logger.info("startup: tables ensured")

    # ✅ Critical schema fix for Gmail internalDate (ms epoch)
    _ensure_bigint_internal_date_ms()


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
    logger.info("debug/logtest hit ✅")
    return {"ok": True, "logged": True}
