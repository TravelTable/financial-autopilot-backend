import logging
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from fastapi.encoders import jsonable_encoder

from app.db import get_db
from app.deps import get_current_user_id
from app.models import Subscription, Transaction, EmailIndex

logger = logging.getLogger("app.routers.debug")

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/counts")
def counts(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.user_id == user_id).count()
    subs = db.query(Subscription).filter(Subscription.user_id == user_id).count()
    emails = (
        db.query(EmailIndex)
        .join(EmailIndex.google_account)
        .filter(EmailIndex.google_account.has(user_id=user_id))
        .count()
    )
    return {"user_id": user_id, "transactions": tx, "subscriptions": subs, "emails_index": emails}

@router.get("/subscriptions")
def debug_subscriptions(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    subs = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.updated_at.desc())
        .limit(50)
        .all()
    )
    logger.info("debug_subscriptions user_id=%s count=%s", user_id, len(subs))
    return {"count": len(subs), "subscriptions": jsonable_encoder(subs)}
