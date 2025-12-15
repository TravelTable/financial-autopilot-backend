from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import Subscription, Notification, NotificationType, SubscriptionStatus

def schedule_alerts(db: Session, *, now_utc: datetime | None = None) -> int:
    now = now_utc or datetime.now(timezone.utc)
    count = 0

    subs = db.execute(select(Subscription).where(Subscription.status == SubscriptionStatus.active)).scalars().all()
    for sub in subs:
        if not sub.next_renewal_date:
            continue
        delta_days = (sub.next_renewal_date - now.date()).days
        if delta_days == 1:
            amt = f"{sub.currency or ''} {sub.amount}" if sub.amount is not None else "an amount"
            db.add(Notification(
                user_id=sub.user_id,
                type=NotificationType.renewal,
                title=f"Renewal tomorrow: {sub.vendor_name}",
                body=f"Your {sub.vendor_name} subscription renews tomorrow for {amt}.",
                scheduled_for=now,
                meta={"subscription_id": sub.id, "next_renewal_date": str(sub.next_renewal_date)},
            ))
            count += 1

    db.commit()
    return count
