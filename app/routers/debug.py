from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.deps import get_current_user_id
from app.db import get_db
from app.models import Subscription, Transaction, EmailIndex, GoogleAccount, User

router = APIRouter(prefix="/debug", tags=["debug"])


def _require_debug_enabled():
    if os.getenv("DEBUG_ROUTES", "0") != "1":
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/db")
def debug_db(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    _require_debug_enabled()

    # show only safe/sanitized db info
    db_url = os.getenv("DATABASE_URL", "")
    parsed = urlparse(db_url) if db_url else None

    counts = {
        "users": db.execute(select(func.count()).select_from(User)).scalar_one(),
        "google_accounts": db.execute(select(func.count()).select_from(GoogleAccount)).scalar_one(),
        "emails_index": db.execute(select(func.count()).select_from(EmailIndex)).scalar_one(),
        "transactions": db.execute(select(func.count()).select_from(Transaction)).scalar_one(),
        "subscriptions": db.execute(select(func.count()).select_from(Subscription)).scalar_one(),
        "subscriptions_for_me": db.execute(
            select(func.count()).select_from(Subscription).where(Subscription.user_id == user_id)
        ).scalar_one(),
    }

    return {
        "debug_enabled": True,
        "user_id": user_id,
        "db": {
            "scheme": parsed.scheme if parsed else None,
            "host": parsed.hostname if parsed else None,
            "path": parsed.path if parsed else None,
        },
        "counts": counts,
    }


@router.get("/subscriptions/sample")
def debug_subscriptions_sample(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    _require_debug_enabled()

    subs = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
        .limit(10)
    ).scalars().all()

    return {
        "user_id": user_id,
        "count": len(subs),
        "items": [
            {
                "id": s.id,
                "vendor_name": s.vendor_name,
                "amount": float(s.amount) if s.amount is not None else None,
                "currency": s.currency,
                "status": getattr(s.status, "value", str(s.status)),
                "next_renewal_date": s.next_renewal_date.isoformat() if s.next_renewal_date else None,
            }
            for s in subs
        ],
    }
