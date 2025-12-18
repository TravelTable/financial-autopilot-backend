from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db import get_db
from app.deps import get_current_user_id
from app.models import Transaction, Subscription, GoogleAccount

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/stats")
def stats(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    tx_count = db.query(func.count(Transaction.id)).filter(Transaction.user_id == user_id).scalar() or 0
    sub_count = db.query(func.count(Subscription.id)).filter(Subscription.user_id == user_id).scalar() or 0

    acct = db.query(GoogleAccount).filter(GoogleAccount.user_id == user_id).first()

    sample = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date.desc().nullslast())
        .limit(5)
        .all()
    )

    return {
        "user_id": user_id,
        "transactions": tx_count,
        "subscriptions": sub_count,
        "last_sync_at": getattr(acct, "last_sync_at", None),
        "last_history_id": getattr(acct, "last_history_id", None),
        "sample_transactions": [
            {
                "id": t.id,
                "vendor": t.vendor,
                "amount": float(t.amount) if t.amount is not None else None,
                "currency": t.currency,
                "date": t.transaction_date,
                "is_subscription": getattr(t, "is_subscription", None),
                "trial_end_date": getattr(t, "trial_end_date", None),
                "renewal_date": getattr(t, "renewal_date", None),
            }
            for t in sample
        ],
    }
