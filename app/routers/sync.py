from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_current_user_id
from app.db import get_db
from app.schemas import SyncRequest, SyncStatusOut
from app.models import GoogleAccount
from app.worker.celery_app import celery_app  # ✅ use celery directly

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

    acct.sync_state = "queued"
    acct.sync_queued = True
    acct.sync_in_progress = False
    acct.sync_error_message = None
    db.commit()

    # ✅ Send by name so the API process does NOT need to import tasks.py
    job = celery_app.send_task(
        "app.worker.tasks.sync_user",
        kwargs={
            "user_id": user_id,
            "google_account_id": acct.id,
            "lookback_days": req.lookback_days,
            "force_reprocess": req.force_reprocess,
        },
    )
    return {"queued": True, "task_id": job.id}


@router.post("/start", response_model=dict)
def start_sync_alias(
    req: SyncRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
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
        state=acct.sync_state,
        started_at=acct.sync_started_at,
        completed_at=acct.sync_completed_at,
        failed_at=acct.sync_failed_at,
        error_message=acct.sync_error_message,
        queued=acct.sync_queued,
        in_progress=acct.sync_in_progress,
    )
