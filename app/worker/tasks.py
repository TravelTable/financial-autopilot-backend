# app/routers/subscriptions.py
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.deps import get_current_user_id
from app.db import get_db
from app.models import Subscription, SubscriptionStatus
from app.schemas import SubscriptionOut

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    subs = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(
            Subscription.next_renewal_date.asc().nullslast(),
            Subscription.id.desc()
        )
    ).scalars().all()
    return [
        SubscriptionOut(
            id=s.id,
            vendor_name=s.vendor_name,
            amount=float(s.amount) if s.amount is not None else None,
            currency=s.currency,
            billing_cycle_days=s.billing_cycle_days,
            last_charge_date=s.last_charge_date,
            next_renewal_date=s.next_renewal_date,
            trial_end_date=s.trial_end_date,
            status=getattr(s.status, "value", str(s.status)),
        )
        for s in subs
    ]

@router.get("/", response_model=list[SubscriptionOut])
def list_subscriptions_slash(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    # call through to the regular handler to avoid duplicating logic
    return list_subscriptions(user_id=user_id, db=db)

@router.post("/{subscription_id}/ignore", response_model=dict)
def ignore_subscription(
    subscription_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    s = db.query(Subscription).filter(
        Subscription.id == subscription_id,
        Subscription.user_id == user_id
    ).first()
    if not s:
        raise HTTPException(status_code=404, detail="Not found")
    s.status = SubscriptionStatus.ignored
    db.commit()
    return {"ok": True}
