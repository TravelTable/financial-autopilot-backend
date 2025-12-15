from fastapi import FastAPI
from app.routers import auth, sync, transactions, subscriptions, refunds, privacy

app = FastAPI(title="Financial Autopilot Backend", version="0.1.0")

app.include_router(auth.router)
app.include_router(sync.router)
app.include_router(transactions.router)
app.include_router(subscriptions.router)
app.include_router(refunds.router)
app.include_router(privacy.router)

@app.get("/health")
def health():
    return {"ok": True}
