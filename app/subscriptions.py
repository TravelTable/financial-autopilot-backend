from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import Transaction, Subscription, SubscriptionStatus

def recompute_subscriptions(db: Session, *, user_id: int) -> None:
    txs = db.execute(
        select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.transaction_date.desc().nullslast())
    ).scalars().all()

    groups = defaultdict(list)
    for tx in txs:
        if tx.vendor and tx.transaction_date:
            groups[tx.vendor.lower()].append(tx)

    db.query(Subscription).filter(Subscription.user_id == user_id).delete()

    for _, items in groups.items():
        if len(items) < 2:
            continue

        dates = sorted({t.transaction_date for t in items if t.transaction_date})
        if len(dates) < 2:
            continue

        gaps = [(dates[i] - dates[i-1]).days for i in range(1, len(dates))]
        gap = sorted(gaps)[len(gaps)//2]
        if gap < 20 or gap > 400:
            continue

        last = max(dates)
        next_renewal = last + timedelta(days=gap)

        amounts = [float(t.amount) for t in items if t.amount is not None]
        amount = sorted(amounts)[len(amounts)//2] if amounts else None
        currency = next((t.currency for t in items if t.currency), None)

        db.add(Subscription(
            user_id=user_id,
            vendor_name=items[0].vendor,
            amount=amount,
            currency=currency,
            billing_cycle_days=gap,
            last_charge_date=last,
            next_renewal_date=next_renewal,
            trial_end_date=None,
            status=SubscriptionStatus.active,
            meta={"source": "recurring_heuristic_v1", "count": len(items)},
        ))

    db.commit()
