import logging
import os

from fastapi import FastAPI

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
import app.models  # register models

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("app.main")

app = FastAPI(
    title="Financial Autopilot Backend",
    version="0.2.0",
)

@app.on_event("startup")
def on_startup():
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
