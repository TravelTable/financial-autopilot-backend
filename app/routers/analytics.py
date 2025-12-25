# app/routers/analytics.py

from collections import defaultdict
from datetime import date, timedelta
from typing import List

from fastapi import APIRouter, Depends
from fastapi_cache.decorator import cache
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user_id  # use the existing dependency
from app.models import Transaction
from app.schemas import (
    SpendingOverviewOut,
    SpendingByCategoryOut,
    SpendingByVendorOut,
    SpendingSeriesPointOut,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
@cache(expire=300)
def get_spending_summary(
    month: int,
    year: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Basic numeric summary for a month – this is what your AI layer will read.
    """
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    txs: List[Transaction] = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.transaction_date >= start,
            Transaction.transaction_date < end,
        )
        .all()
    )

    total = sum(t.amount for t in txs if t.amount is not None)

    by_category = {}
    for t in txs:
        cat = (t.category or "Uncategorized").strip()
        by_category.setdefault(cat, 0.0)
        if t.amount is not None:
            by_category[cat] += t.amount

    by_vendor = {}
    for t in txs:
        # Transactions store canonical vendor name in .vendor
        name = (t.vendor or "Unknown").strip()
        by_vendor.setdefault(name, 0.0)
        if t.amount is not None:
            by_vendor[name] += t.amount

    return {
        "month": month,
        "year": year,
        "total": total,
        "by_category": by_category,
        "by_vendor": by_vendor,
        "transaction_count": len(txs),
    }


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _receipt_confidence(tx: Transaction) -> float | None:
    confidence = getattr(tx, "confidence", None)
    if not isinstance(confidence, dict):
        return None
    values = []
    for key in ("amount", "date"):
        val = confidence.get(key)
        if isinstance(val, (int, float)):
            values.append(float(val))
    if not values:
        return None
    return sum(values) / len(values)


def _has_receipt_evidence(tx: Transaction) -> bool:
    meta = getattr(tx, "meta", None)
    if isinstance(meta, dict):
        if meta.get("apple") or meta.get("billing_provider"):
            return True
    confidence = _receipt_confidence(tx)
    return confidence is not None and confidence >= 0.5


def _is_subscription_like(tx: Transaction) -> bool:
    if getattr(tx, "is_subscription", False):
        return True
    category = (getattr(tx, "category", None) or "").strip().lower()
    if category in {"subscription", "subscriptions"}:
        return True
    meta = getattr(tx, "meta", None)
    if isinstance(meta, dict):
        apple_meta = meta.get("apple")
        if isinstance(apple_meta, dict):
            if apple_meta.get("subscription_display_name") or apple_meta.get("app_name"):
                return True
            raw_signals = apple_meta.get("raw_signals") or {}
            if isinstance(raw_signals, dict) and raw_signals.get("subscription_terms"):
                return True
    return False


@router.get("/overview", response_model=SpendingOverviewOut)
def get_spending_overview(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    start_date: date | None = None,
    end_date: date | None = None,
    top_n: int = 10,
):
    """
    Advanced receipt-aware spending overview for subscriptions + general spending.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=180)

    query = db.query(Transaction).filter(Transaction.user_id == user_id)
    if start_date:
        query = query.filter(Transaction.transaction_date >= start_date)
    if end_date:
        query = query.filter(Transaction.transaction_date <= end_date)

    txs: List[Transaction] = query.all()

    total_spend = 0.0
    subscription_spend = 0.0
    general_spend = 0.0
    receipt_spend = 0.0
    receipt_count = 0
    subscription_count = 0
    general_count = 0

    category_totals = defaultdict(float)
    vendor_stats = defaultdict(lambda: {"total": 0.0, "count": 0, "receipt_count": 0})
    monthly_stats = defaultdict(lambda: {"total": 0.0, "subscription": 0.0, "general": 0.0, "count": 0, "receipt_count": 0})

    amounts: list[float] = []

    for tx in txs:
        amount = _safe_float(getattr(tx, "amount", None))
        is_subscription = _is_subscription_like(tx)
        has_receipt = _has_receipt_evidence(tx)

        if amount is not None:
            total_spend += amount
            amounts.append(amount)
            if is_subscription:
                subscription_spend += amount
            else:
                general_spend += amount
            if has_receipt:
                receipt_spend += amount

        if has_receipt:
            receipt_count += 1

        if is_subscription:
            subscription_count += 1
        else:
            general_count += 1

        category = (getattr(tx, "category", None) or "Uncategorized").strip()
        if amount is not None:
            category_totals[category] += amount

        vendor = (getattr(tx, "vendor", None) or "Unknown").strip()
        vendor_stats[vendor]["count"] += 1
        if amount is not None:
            vendor_stats[vendor]["total"] += amount
        if has_receipt:
            vendor_stats[vendor]["receipt_count"] += 1

        tx_date = getattr(tx, "transaction_date", None)
        if tx_date:
            key = (tx_date.year, tx_date.month)
            monthly_stats[key]["count"] += 1
            if amount is not None:
                monthly_stats[key]["total"] += amount
                if is_subscription:
                    monthly_stats[key]["subscription"] += amount
                else:
                    monthly_stats[key]["general"] += amount
            if has_receipt:
                monthly_stats[key]["receipt_count"] += 1

    by_category = [
        SpendingByCategoryOut(category=cat, total=total)
        for cat, total in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ][:top_n]

    by_vendor = []
    for vendor, stats in sorted(vendor_stats.items(), key=lambda item: item[1]["total"], reverse=True)[:top_n]:
        count = stats["count"]
        receipt_rate = stats["receipt_count"] / count if count else 0.0
        by_vendor.append(
            SpendingByVendorOut(
                vendor=vendor,
                total=stats["total"],
                transaction_count=count,
                receipt_coverage_rate=receipt_rate,
            )
        )

    monthly_series = []
    for (year, month), stats in sorted(monthly_stats.items()):
        count = stats["count"]
        receipt_rate = stats["receipt_count"] / count if count else 0.0
        monthly_series.append(
            SpendingSeriesPointOut(
                year=year,
                month=month,
                total=stats["total"],
                subscription_total=stats["subscription"],
                general_total=stats["general"],
                transaction_count=count,
                receipt_coverage_rate=receipt_rate,
            )
        )

    transaction_count = len(txs)
    receipt_coverage_rate = receipt_count / transaction_count if transaction_count else 0.0
    subscription_share = subscription_spend / total_spend if total_spend else 0.0

    average_transaction = sum(amounts) / len(amounts) if amounts else None
    largest_transaction = max(amounts) if amounts else None

    return SpendingOverviewOut(
        start_date=start_date,
        end_date=end_date,
        total_spend=total_spend,
        subscription_spend=subscription_spend,
        general_spend=general_spend,
        subscription_share=subscription_share,
        transaction_count=transaction_count,
        subscription_count=subscription_count,
        general_count=general_count,
        receipt_transaction_count=receipt_count,
        receipt_coverage_rate=receipt_coverage_rate,
        receipt_spend=receipt_spend,
        average_transaction=average_transaction,
        largest_transaction=largest_transaction,
        by_category=by_category,
        by_vendor=by_vendor,
        monthly_series=monthly_series,
    )
