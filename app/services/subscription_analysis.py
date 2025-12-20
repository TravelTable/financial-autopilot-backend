# app/services/subscription_analysis.py

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import statistics
from sqlalchemy.orm import Session

from app.models import Subscription, User  # keep User for type/route compatibility
from app.models_advanced import SubscriptionPriceHistory, UserSettings


def record_price_point(
    db: Session,
    subscription: Subscription,
    amount: float,
    currency: str,
    effective_date: date,
) -> SubscriptionPriceHistory:
    """
    Store a price point for a subscription. Idempotent per subscription per day.
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


def _get_user_price_increase_threshold(db: Session, user_id: int, default: float = 10.0) -> float:
    """
    Read per-user threshold if available; otherwise fall back to default.
    """
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if settings is None:
        return default

    val = getattr(settings, "price_increase_threshold", None)
    try:
        return float(val) if val is not None else default
    except Exception:
        return default


def detect_price_increase(
    db: Session,
    subscription: Subscription,
    threshold: Optional[float] = None,
) -> Tuple[bool, float, float]:
    """
    Returns (has_increased, old_price, new_price).

    - old_price = median of historical prices excluding the newest point
    - new_price = newest recorded price point
    - threshold = % increase needed to trigger. If None, uses UserSettings.price_increase_threshold
    """
    history: List[SubscriptionPriceHistory] = (
        db.query(SubscriptionPriceHistory)
        .filter(SubscriptionPriceHistory.subscription_id == subscription.id)
        .order_by(SubscriptionPriceHistory.effective_date.asc())
        .all()
    )
    if len(history) < 2:
        return False, 0.0, 0.0

    old_prices = [h.amount for h in history[:-1] if h.amount is not None]
    new_price = history[-1].amount

    if not old_prices or new_price is None:
        return False, 0.0, float(new_price or 0.0)

    old_price = float(statistics.median(old_prices))

    if old_price <= 0:
        return False, old_price, float(new_price)

    # percent increase relative to baseline
    delta_pct = (float(new_price) - old_price) * 100.0 / old_price

    if threshold is None:
        # derive from user settings if available
        user_id = getattr(subscription, "user_id", None)
        if isinstance(user_id, int):
            threshold = _get_user_price_increase_threshold(db, user_id=user_id, default=10.0)
        else:
            threshold = 10.0

    has_increased = delta_pct >= float(threshold)
    return has_increased, old_price, float(new_price)


def price_increase_insight(
    db: Session,
    subscription: Subscription,
    threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Convenience wrapper that returns a structured "insight" dict (for storing in Subscription.meta
    or returning to the UI). Returns None if no increase is detected.

    Example output:
    {
      "type": "price_increase",
      "old_price": 9.99,
      "new_price": 12.99,
      "percent": 30.03,
      "threshold_percent": 10.0,
      "reason": "Price increased from 9.99 to 12.99"
    }
    """
    has_increased, old_price, new_price = detect_price_increase(db, subscription, threshold=threshold)
    if not has_increased or old_price <= 0:
        return None

    pct = (new_price - old_price) * 100.0 / old_price

    # normalize threshold
    used_threshold = threshold
    if used_threshold is None:
        user_id = getattr(subscription, "user_id", None)
        used_threshold = _get_user_price_increase_threshold(db, user_id=user_id, default=10.0) if isinstance(user_id, int) else 10.0

    return {
        "type": "price_increase",
        "old_price": float(old_price),
        "new_price": float(new_price),
        "percent": float(pct),
        "threshold_percent": float(used_threshold),
        "reason": f"Price increased from {old_price:.2f} to {new_price:.2f}",
    }


def _normalize_vendor_name(name: Optional[str]) -> str:
    """
    Conservative normalization to reduce obvious duplicates without over-merging.
    """
    if not name:
        return ""
    s = name.strip().lower()
    # keep alnum and spaces
    cleaned = []
    for ch in s:
        if ch.isalnum() or ch == " ":
            cleaned.append(ch)
    s = "".join(cleaned)
    s = " ".join(s.split())
    return s


def find_duplicate_subscriptions(db: Session, user: User) -> List[List[Subscription]]:
    """
    Very simple duplicate detection:
    groups subscriptions for the same user with same vendor and similar price.

    Returns a list of "groups", each group containing 2+ subscriptions that look like duplicates.
    """
    # Prefer enum comparison if available; otherwise fall back to string.
    try:
        from app.models import SubscriptionStatus  # type: ignore
        active_status = SubscriptionStatus.active
    except Exception:
        active_status = "active"

    subs: List[Subscription] = (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == active_status)
        .all()
    )

    buckets: Dict[tuple, List[Subscription]] = {}

    for s in subs:
        vendor_id = getattr(s, "vendor_id", None)
        amount = getattr(s, "amount", None)
        currency = getattr(s, "currency", None)

        # Fallback vendor key if vendor_id is missing
        vendor_name = _normalize_vendor_name(getattr(s, "vendor_name", None))

        if amount is None or currency is None:
            continue

        # Prefer vendor_id if present; else use normalized name
        vendor_key = vendor_id if vendor_id is not None else vendor_name
        if vendor_key is None or vendor_key == "":
            continue

        key = (vendor_key, round(float(amount), 2), str(currency))
        buckets.setdefault(key, []).append(s)

    duplicates = [group for group in buckets.values() if len(group) > 1]
    return duplicates


def duplicate_groups_insights(duplicate_groups: List[List[Subscription]]) -> List[Dict[str, Any]]:
    """
    Convert duplicate groups into structured "insight" dicts you can store in meta / show in UI.

    Example output item:
    {
      "type": "duplicate",
      "subscription_ids": [12, 15],
      "vendor_name": "Spotify",
      "amount": 11.99,
      "currency": "AUD",
      "reason": "Possible duplicate subscriptions: same vendor and price"
    }
    """
    insights: List[Dict[str, Any]] = []

    for group in duplicate_groups:
        if not group:
            continue

        first = group[0]
        ids = [int(getattr(s, "id")) for s in group if getattr(s, "id", None) is not None]
        vendor_name = getattr(first, "vendor_name", None)
        amount = getattr(first, "amount", None)
        currency = getattr(first, "currency", None)

        insights.append(
            {
                "type": "duplicate",
                "subscription_ids": ids,
                "vendor_name": vendor_name,
                "amount": float(amount) if amount is not None else None,
                "currency": currency,
                "reason": "Possible duplicate subscriptions: same vendor and price",
            }
        )

    return insights
