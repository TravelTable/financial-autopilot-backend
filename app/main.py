from fastapi import FastAPI
import logging
import os
import sys

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

from app.db import engine
from app.models import Base
import app.models  # ensure model registration


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
    Gmail internalDate is epoch milliseconds (e.g. 1766020775000),
    which overflows 32-bit INTEGER. Ensure BIGINT in Postgres.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                ALTER TABLE emails_index
                ALTER COLUMN internal_date_ms TYPE BIGINT
                USING internal_date_ms::bigint
            """))
        logger.info("startup: ensured emails_index.internal_date_ms is BIGINT")
    except ProgrammingError as e:
        logger.warning("startup: could not alter emails_index.internal_date_ms (%s)", str(e))
    except Exception as e:
        logger.exception("startup: failed ensuring BIGINT for internal_date_ms: %s", str(e))


@app.on_event("startup")
def on_startup():
    logger.info("startup: creating tables if needed")
    Base.metadata.create_all(bind=engine)
    logger.info("startup: tables ensured")
    _ensure_bigint_internal_date_ms()


# Routers
app.include_router(auth.router)
app.include_router(sync.router)

app.include_router(transactions.router)
app.include_router(subscriptions.router)

app.include_router(analytics.router)
app.include_router(notifications.router)

app.include_router(refunds.router)
app.include_router(privacy.router)

app.include_router(debug.router)


@app.get("/health", tags=["system"])
def health():
    return {
        "ok": True,
        "service": "financial-autopilot-backend",
        "version": "0.2.0",
    }
