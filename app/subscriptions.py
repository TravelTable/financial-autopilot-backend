from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models import Transaction, Subscription, SubscriptionStatus

def recompute_subscriptions(db: Session, *, user_id: int) -> None:
    """
    Rebuild the user's subscriptions.  Detect recurring charges from
    multiple transactions (original logic) *or* from single transactions
    flagged as subscriptions / trials.
    """
    # Fetch all transactions for this user
    txs = db.execute(
        select(Transaction).where(
            Transaction.user_id == user_id
        ).order_by(
            Transaction.transaction_date.desc().nullslast()
        )
    ).scalars().all()

    # Group by vendor (case‑insensitive)
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in txs:
        if tx.vendor and tx.transaction_date:
            groups[tx.vendor.lower()].append(tx)

    # Delete old subscriptions
    before = db.query(Subscription).filter(Subscription.user_id == user_id).count()
    db.query(Subscription).filter(Subscription.user_id == user_id).delete()

    created = 0

    for vendor_key, items in groups.items():
        # First, handle single or non‑recurring subscriptions flagged by the extraction/LLM
        flagged = [
            t for t in items
            if t.is_subscription or t.trial_end_date or t.renewal_date
        ]
        if flagged:
            tx = flagged[0]
            next_renewal = tx.renewal_date or tx.trial_end_date
            # Use the transaction_date as last_charge_date even if None
            last_date = tx.transaction_date
            # Use amount/currency from the flagged transaction
            amount = float(tx.amount) if tx.amount is not None else None
            db.add(Subscription(
                user_id=user_id,
                vendor_name=tx.vendor,
                amount=amount,
                currency=tx.currency,
                billing_cycle_days=None,
                last_charge_date=last_date,
                next_renewal_date=next_renewal,
                trial_end_date=tx.trial_end_date,
                status=SubscriptionStatus.active if not tx.trial_end_date else SubscriptionStatus.active,
                meta={"source": "single_flagged", "count": len(flagged)},
            ))
            created += 1
            # don’t run the recurring logic for this vendor
            continue

        # Original recurring‑charge logic: require ≥2 charges
        if len(items) < 2:
            continue

        dates = sorted({t.transaction_date for t in items if t.transaction_date})
        if len(dates) < 2:
            continue

        # median gap between charges
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        gap = sorted(gaps)[len(gaps) // 2]
        if gap < 20 or gap > 400:
            continue

        last = max(dates)
        next_renewal = last + timedelta(days=gap)

        amounts = [float(t.amount) for t in items if t.amount is not None]
        amount = sorted(amounts)[len(amounts) // 2] if amounts else None
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
        created += 1

    db.commit()

    after = db.query(Subscription).filter(Subscription.user_id == user_id).count()
    print(f"[recompute_subscriptions] deleted {before} old, created {created}, now {after} subscriptions for user {user_id}")
