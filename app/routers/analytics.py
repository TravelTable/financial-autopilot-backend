# app/routers/analytics.py
from datetime import date
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Transaction
from app.auth_dep import get_current_user  # whatever you use for auth

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
def get_spending_summary(
    month: int,
    year: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
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
            Transaction.user_id == current_user.id,
            Transaction.date >= start,
            Transaction.date < end,
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
        name = (t.vendor_name or t.raw_merchant or "Unknown").strip()
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
