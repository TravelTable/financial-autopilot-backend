from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_user_id
from app.db import get_db
from app.schemas import SyncRequest, SyncStatusOut
from app.models import GoogleAccount
from app.worker.tasks import sync_user

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("", response_model=dict)
def start_sync(
    req: SyncRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    acct = db.query(GoogleAccount).filter(GoogleAccount.user_id == user_id).first()
    if not acct:
        raise HTTPException(status_code=400, detail="No Google account connected")

    job = sync_user.delay(
        user_id=user_id,
        google_account_id=acct.id,
        lookback_days=req.lookback_days,
    )
    return {"queued": True, "task_id": job.id}


@router.post("/start", response_model=dict)
def start_sync_alias(
    req: SyncRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    # Alias for older clients calling POST /sync/start
    return start_sync(req=req, user_id=user_id, db=db)


@router.get("/status", response_model=SyncStatusOut)
def sync_status(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    acct = db.query(GoogleAccount).filter(GoogleAccount.user_id == user_id).first()
    if not acct:
        raise HTTPException(status_code=400, detail="No Google account connected")

    return SyncStatusOut(
        last_sync_at=acct.last_sync_at,
        last_history_id=acct.last_history_id,
        queued=False,
    )
