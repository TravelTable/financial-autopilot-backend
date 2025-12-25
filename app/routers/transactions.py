from __future__ import annotations
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, load_only
from sqlalchemy import select, inspect
from app.deps import get_current_user_id
from app.db import get_db
from app.models import Transaction
from app.rate_limit import limiter
from app.schemas import TransactionOut, ReanalyzeTransactionRequest
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/transactions", tags=["transactions"])
MAX_LIMIT = 200


def _transactions_meta_available(db: Session) -> bool:
    try:
        columns = {col["name"] for col in inspect(db.get_bind()).get_columns("transactions")}
        return "meta" in columns
    except Exception:
        return False

@router.get("", response_model=list[TransactionOut])
@limiter.limit("100/minute")
def list_transactions(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order_by: str = Query("date_desc", pattern="^(date_desc|date_asc|amount_desc|amount_asc)$"),
    min_amount: float | None = Query(None, ge=0),
    max_amount: float | None = Query(None, ge=0),
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = Query(None, max_length=100),
):
    query = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
    )
    filters = []
    if min_amount is not None:
        filters.append(Transaction.amount >= min_amount)
    if max_amount is not None:
        filters.append(Transaction.amount <= max_amount)
    if start_date is not None:
        filters.append(Transaction.transaction_date >= start_date)
    if end_date is not None:
        filters.append(Transaction.transaction_date <= end_date)
    if search:
        like = f"%{search}%"
        filters.append(or_(Transaction.vendor.ilike(like), Transaction.category.ilike(like)))
    if filters:
        query = query.where(and_(*filters))

    if order_by == "date_asc":
        order_clause = Transaction.transaction_date.asc().nullslast()
    elif order_by == "amount_desc":
        order_clause = Transaction.amount.desc().nullslast()
    elif order_by == "amount_asc":
        order_clause = Transaction.amount.asc().nullslast()
    else:
        order_clause = Transaction.transaction_date.desc().nullslast()

    query = query.order_by(order_clause, Transaction.id.desc()).limit(limit).offset(offset)
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
