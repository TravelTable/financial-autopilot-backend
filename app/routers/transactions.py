from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.deps import get_current_user_id
from app.db import get_db
from app.models import Transaction
from app.schemas import TransactionOut

router = APIRouter(prefix="/transactions", tags=["transactions"])

@router.get("", response_model=list[TransactionOut])
def list_transactions(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    txs = db.execute(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.transaction_date.desc().nullslast(), Transaction.id.desc())).scalars().all()
    return [
        TransactionOut(
            id=t.id,
            gmail_message_id=t.gmail_message_id,
            vendor=t.vendor,
            amount=float(t.amount) if t.amount is not None else None,
            currency=t.currency,
            transaction_date=t.transaction_date,
            category=t.category,
            is_subscription=bool(t.is_subscription),
            trial_end_date=t.trial_end_date,
            renewal_date=t.renewal_date,
        )
        for t in txs
    ]
