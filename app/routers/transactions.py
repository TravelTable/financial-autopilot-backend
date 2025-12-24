from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, load_only
from sqlalchemy import select, inspect
from app.deps import get_current_user_id
from app.db import get_db
from app.models import Transaction
from app.schemas import TransactionOut, ReanalyzeTransactionRequest
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _transactions_meta_available(db: Session) -> bool:
    try:
        columns = {col["name"] for col in inspect(db.get_bind()).get_columns("transactions")}
        return "meta" in columns
    except Exception:
        return False

@router.get("", response_model=list[TransactionOut])
def list_transactions(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    query = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date.desc().nullslast(), Transaction.id.desc())
    )
    if not _transactions_meta_available(db):
        query = query.options(
            load_only(
                Transaction.id,
                Transaction.gmail_message_id,
                Transaction.vendor,
                Transaction.amount,
                Transaction.currency,
                Transaction.transaction_date,
                Transaction.category,
                Transaction.is_subscription,
                Transaction.trial_end_date,
                Transaction.renewal_date,
            )
        )
    txs = db.execute(query).scalars().all()
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


@router.post("/{transaction_id}/reanalyze", response_model=dict)
def reanalyze_transaction(
    transaction_id: int,
    req: ReanalyzeTransactionRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    tx = (
        db.query(Transaction)
        .filter(Transaction.id == transaction_id, Transaction.user_id == user_id)
        .first()
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    job = celery_app.send_task(
        "app.worker.tasks.reanalyze_transaction",
        kwargs={
            "user_id": user_id,
            "transaction_id": transaction_id,
            "force_llm": req.force_llm,
        },
    )
    return {"queued": True, "task_id": job.id}
