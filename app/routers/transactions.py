import datetime as dt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, load_only
from sqlalchemy import select, inspect
from app.deps import get_current_user_id
from app.db import get_db
from app.models import Transaction
from app.rate_limit import limiter
from app.schemas import TransactionOut, ReanalyzeTransactionRequest, ReceiptEvidenceOut
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/transactions", tags=["transactions"])
MAX_LIMIT = 200


def _transactions_meta_available(db: Session) -> bool:
    try:
        columns = {col["name"] for col in inspect(db.get_bind()).get_columns("transactions")}
        return "meta" in columns
    except Exception:
        return False


def _receipt_confidence(confidence: dict | None) -> float | None:
    if not isinstance(confidence, dict):
        return None
    values: list[float] = []
    for key in ("amount", "date"):
        val = confidence.get(key)
        if isinstance(val, (int, float)):
            values.append(float(val))
    if not values:
        return None
    return sum(values) / len(values)


def _build_receipt(tx: Transaction) -> ReceiptEvidenceOut | None:
    meta = getattr(tx, "meta", None)
    if not isinstance(meta, dict):
        meta = {}
    apple_meta = meta.get("apple")
    billing_provider = meta.get("billing_provider")

    receipt_confidence = _receipt_confidence(getattr(tx, "confidence", None))
    has_receipt = bool(apple_meta or billing_provider or (receipt_confidence is not None and receipt_confidence >= 0.5))
    if not has_receipt:
        return None

    purchase_date = None
    if isinstance(apple_meta, dict):
        purchase_date_raw = apple_meta.get("purchase_date_utc")
        if isinstance(purchase_date_raw, str):
            try:
                purchase_date = dt.datetime.fromisoformat(purchase_date_raw)
            except ValueError:
                purchase_date = None

    return ReceiptEvidenceOut(
        has_receipt=True,
        provider="apple" if isinstance(apple_meta, dict) else None,
        billing_provider=billing_provider if isinstance(billing_provider, str) else None,
        order_id=apple_meta.get("order_id") if isinstance(apple_meta, dict) else None,
        original_order_id=apple_meta.get("original_order_id") if isinstance(apple_meta, dict) else None,
        purchase_date_utc=purchase_date,
        country=apple_meta.get("country") if isinstance(apple_meta, dict) else None,
        family_sharing=apple_meta.get("family_sharing") if isinstance(apple_meta, dict) else None,
        app_name=apple_meta.get("app_name") if isinstance(apple_meta, dict) else None,
        subscription_display_name=apple_meta.get("subscription_display_name") if isinstance(apple_meta, dict) else None,
        developer_or_seller=apple_meta.get("developer_or_seller") if isinstance(apple_meta, dict) else None,
    )

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
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    search: str | None = Query(None, max_length=100),
):
    meta_available = _transactions_meta_available(db)
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
    if not meta_available:
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
    results = []
    for t in txs:
        receipt_confidence = _receipt_confidence(getattr(t, "confidence", None))
        receipt = _build_receipt(t) if meta_available else None
        results.append(
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
                receipt=receipt,
                receipt_confidence=receipt_confidence,
            )
        )
    return results


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
