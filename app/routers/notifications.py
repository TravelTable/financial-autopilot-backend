from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, select

from app.db import get_db
from app.deps import get_current_user_id
from app.models import Notification
from app.rate_limit import limiter

router = APIRouter(prefix="/notifications", tags=["notifications"])
MAX_LIMIT = 200

@router.get("")
@limiter.limit("60/minute")
def list_notifications(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order_by: str = Query("created_at_desc", pattern="^(created_at_desc|created_at_asc)$"),
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    search: str | None = Query(None, max_length=100),
):
    query = select(Notification).where(Notification.user_id == user_id)
    filters = []
    if start_date is not None:
        filters.append(Notification.created_at >= start_date)
    if end_date is not None:
        filters.append(Notification.created_at <= end_date)
    if search:
        like = f"%{search}%"
        filters.append(or_(Notification.title.ilike(like), Notification.body.ilike(like)))
    if filters:
        query = query.where(and_(*filters))
    if order_by == "created_at_asc":
        order_clause = Notification.created_at.asc()
    else:
        order_clause = Notification.created_at.desc()

    items = db.execute(query.order_by(order_clause).limit(limit).offset(offset)).scalars().all()

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
