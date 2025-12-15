from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db import get_db
from app.deps import get_current_user_id
from app.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])

@router.get("")
def list_notifications(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    items = db.execute(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
    ).scalars().all()

    return [
        {
            "id": n.id,
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "scheduled_for": n.scheduled_for,
            "delivered_at": n.delivered_at,
            "meta": n.meta,
            "created_at": n.created_at,
        }
        for n in items
    ]
