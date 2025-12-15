# app/services/subscription_analysis.py
from typing import List, Tuple
from datetime import date
import statistics
from sqlalchemy.orm import Session
from app.models import Subscription, Transaction, User  # adjust import to your models
from app.models_advanced import SubscriptionPriceHistory, UserSettings, Vendor


def record_price_point(
    db: Session,
    subscription: Subscription,
    amount: float,
    currency: str,
    effective_date: date,
) -> SubscriptionPriceHistory:
    """
    Store a price point for a subscription. Idempotent per day.
    """
    existing = (
        db.query(SubscriptionPriceHistory)
        .filter(
            SubscriptionPriceHistory.subscription_id == subscription.id,
            SubscriptionPriceHistory.effective_date == effective_date,
        )
        .first()
    )
    if existing:
        return existing

    ph = SubscriptionPriceHistory(
        subscription_id=subscription.id,
        amount=amount,
        currency=currency,
        effective_date=effective_date,
    )
    db.add(ph)
    db.commit()
    db.refresh(ph)
    return ph


def detect_price_increase(
    db: Session, subscription: Subscription
) -> Tuple[bool, float, float]:
    """
    Returns (has_increased, old_price, new_price).
    """
    history: List[SubscriptionPriceHistory] = (
        db.query(SubscriptionPriceHistory)
        .filter(SubscriptionPriceHistory.subscription_id == subscription.id)
        .order_by(SubscriptionPriceHistory.effective_date.asc())
        .all()
    )
    if len(history) < 2:
        return False, 0.0, 0.0

    # old price = median of all but last
    old_prices = [h.amount for h in history[:-1]]
    new_price = history[-1].amount
    old_price = statistics.median(old_prices)

    if old_price <= 0:
        return False, old_price, new_price

    delta_pct = (new_price - old_price) * 100.0 / old_price
    threshold = 10.0  # default 10%; you’ll usually load this from UserSettings
    has_increased = delta_pct >= threshold
    return has_increased, old_price, new_price


def find_duplicate_subscriptions(db: Session, user: User) -> List[List[Subscription]]:
    """
    Very simple duplicate detection:
    groups subscriptions for the same user with same vendor and similar price.
    """
    subs: List[Subscription] = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active")
        .all()
    )
    buckets = {}
    for s in subs:
        vendor_id = getattr(s, "vendor_id", None)
        amount = getattr(s, "amount", None)
        currency = getattr(s, "currency", None)
        if vendor_id is None or amount is None or currency is None:
            continue
        key = (vendor_id, round(amount, 2), currency)
        buckets.setdefault(key, []).append(s)

    duplicates = [group for group in buckets.values() if len(group) > 1]
    return duplicates
