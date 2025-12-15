from fastapi import FastAPI

from app.routers import auth, sync, transactions, subscriptions, refunds, privacy
from app.routers import analytics, notifications  # direct imports = no __init__.py issues

app = FastAPI(
    title="Financial Autopilot Backend",
    version="0.2.0",
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


@app.get("/health", tags=["system"])
def health():
    return {
        "ok": True,
        "service": "financial-autopilot-backend",
        "version": "0.2.0",
    }
