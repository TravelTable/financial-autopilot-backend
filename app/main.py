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
)

# --- DATABASE ---
from app.db import engine
from app.models import Base  # Base = declarative_base()
import app.models  # IMPORTANT: ensures all models (User, etc.) are registered

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
    Base.metadata.create_all(bind=engine)

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


@app.get("/health", tags=["system"])
def health():
    return {
        "ok": True,
        "service": "financial-autopilot-backend",
        "version": "0.2.0",
    }
