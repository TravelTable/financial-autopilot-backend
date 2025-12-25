import datetime as dt
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_, select, inspect
from sqlalchemy.orm import Session, load_only

from app.deps import get_current_user_id
from app.db import get_db
from app.models import Subscription, SubscriptionStatus
from app.rate_limit import limiter
from app.schemas import SubscriptionOut, SubscriptionInsightsOut, EvidenceChargeOut

# Transaction model might exist in app.models
# We import it optionally to avoid hard crashes if name differs.
try:
    from app.models import Transaction  # type: ignore
except Exception:  # pragma: no cover
    Transaction = None  # type: ignore


router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])
MAX_LIMIT = 200


# -----------------------------
# Helpers
# -----------------------------

def _transactions_meta_available(db: Session) -> bool:
    try:
        columns = {col["name"] for col in inspect(db.get_bind()).get_columns("transactions")}
        return "meta" in columns
    except Exception:
        return False

def _normalize_vendor(s: str) -> str:
    """
    Normalize vendor strings so evidence matching works more reliably.
    Keep it conservative to avoid over-merging unrelated vendors.
    """
    if not s:
        return ""
    s = s.strip().lower()

    # common noise
    for token in ["*", "  ", "\t", "\n"]:
        s = s.replace(token, " ")

    # remove very common billing noise tokens
    noise = [
        "apple.com/bill",
        "apple.com",
        "bill",
        "payment",
        "purchase",
        "receipt",
        "invoice",
    ]
    # NOTE: we do NOT remove company names like "spotify" etc.
    for n in noise:
        s = s.replace(n, " ")

    # keep alnum + spaces
    cleaned = []
    for ch in s:
        if ch.isalnum() or ch == " ":
            cleaned.append(ch)
    s = "".join(cleaned)

    # collapse whitespace
    s = " ".join(s.split())
    return s


def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _get_meta(s: Subscription) -> dict:
    meta = getattr(s, "meta", None)
    if isinstance(meta, dict):
        return meta
    return {}


def _parse_date_maybe(v: Any) -> Optional[dt.date]:
    """
    JSON meta may store dates as ISO strings. Accept:
    - date
    - "YYYY-MM-DD"
    """
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v.strip()[:10])
        except Exception:
            return None
    return None


def _parse_int_maybe(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip()))
        except Exception:
            return None
    return None


def _parse_float_maybe(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except Exception:
            return None
    return None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _subscription_key_from_meta(meta: dict) -> Optional[str]:
    key = meta.get("subscription_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def _tx_matches_subscription(
    tx: Any,
    *,
    subscription_key: Optional[str],
    vendor_key: str,
    meta_available: bool,
) -> bool:
    if subscription_key and meta_available:
        meta = getattr(tx, "meta", None)
        if isinstance(meta, dict):
            apple_meta = meta.get("apple")
            if isinstance(apple_meta, dict) and apple_meta.get("subscription_key") == subscription_key:
                return True
    if vendor_key:
        tx_vendor = _normalize_vendor(getattr(tx, "vendor", "") or "")
        return tx_vendor == vendor_key
    return False


def _extract_product_fields(
    tx: Any,
    *,
    meta_available: bool,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not meta_available:
        return None, None, None
    meta = getattr(tx, "meta", None)
    if not isinstance(meta, dict):
        return None, None, None
    apple_meta = meta.get("apple")
    if isinstance(apple_meta, dict):
        product_name = (
            apple_meta.get("subscription_display_name")
            or apple_meta.get("app_name")
        )
        product_id = apple_meta.get("original_order_id") or apple_meta.get("order_id")
        return product_name, product_id, "apple"
    return None, None, None


def _subscription_transactions(
    subscription: Subscription,
    txs: list[Any],
    *,
    meta_available: bool,
) -> list[Any]:
    meta = _get_meta(subscription)
    subscription_key = _subscription_key_from_meta(meta)
    vendor_key = _normalize_vendor(getattr(subscription, "vendor_name", "") or "")
    matched = [
        tx for tx in txs
        if _tx_matches_subscription(
            tx,
            subscription_key=subscription_key,
            vendor_key=vendor_key,
            meta_available=meta_available,
        )
    ]
        matched.sort(
            key=lambda x: getattr(x, "transaction_date", None) or dt.date.min,
            reverse=True,
        )
    return matched


def _compute_amounts(
    subscription: Subscription,
    txs: list[Any],
    *,
    meta_available: bool,
) -> dict[str, Optional[float]]:
    matched = _subscription_transactions(subscription, txs, meta_available=meta_available)
    amounts = [
        _safe_float(getattr(tx, "amount", None))
        for tx in matched
        if _safe_float(getattr(tx, "amount", None)) is not None
    ]
    latest_amount = amounts[0] if amounts else None
    previous_amount = amounts[1] if len(amounts) > 1 else None
    estimated_amount = None
    if latest_amount is None and amounts:
        sample = amounts[:6]
        estimated_amount = _median(sample)
    return {
        "latest_amount": latest_amount,
        "previous_amount": previous_amount,
        "estimated_amount": estimated_amount,
    }


def _compute_product_fields(
    subscription: Subscription,
    txs: list[Any],
    *,
    meta_available: bool,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    matched = _subscription_transactions(subscription, txs, meta_available=meta_available)
    for tx in matched:
        product_name, product_id, provider = _extract_product_fields(tx, meta_available=meta_available)
        if product_name or product_id or provider:
            return product_name, product_id, provider
    return None, None, None


# -----------------------------
# Existing list endpoints
# -----------------------------

@router.get("", response_model=list[SubscriptionOut])
@limiter.limit("60/minute")
def list_subscriptions(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order_by: str = Query(
        "next_renewal_date",
        pattern="^(next_renewal_date|next_renewal_date_desc|last_charge_date|amount_desc|amount_asc)$",
    ),
    min_amount: float | None = Query(None, ge=0),
    max_amount: float | None = Query(None, ge=0),
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    search: str | None = Query(None, max_length=100),
):
    query = select(Subscription).where(Subscription.user_id == user_id)
    filters = []
    if min_amount is not None:
        filters.append(Subscription.amount >= min_amount)
    if max_amount is not None:
        filters.append(Subscription.amount <= max_amount)
    if start_date is not None:
        filters.append(Subscription.last_charge_date >= start_date)
    if end_date is not None:
        filters.append(Subscription.last_charge_date <= end_date)
    if search:
        like = f"%{search}%"
        filters.append(or_(Subscription.vendor_name.ilike(like)))
    if filters:
        query = query.where(and_(*filters))

    if order_by == "next_renewal_date_desc":
        order_clause = Subscription.next_renewal_date.desc().nullslast()
    elif order_by == "last_charge_date":
        order_clause = Subscription.last_charge_date.desc().nullslast()
    elif order_by == "amount_desc":
        order_clause = Subscription.amount.desc().nullslast()
    elif order_by == "amount_asc":
        order_clause = Subscription.amount.asc().nullslast()
    else:
        order_clause = Subscription.next_renewal_date.asc().nullslast()

    subs = db.execute(
        query.order_by(order_clause, Subscription.id.desc()).limit(limit).offset(offset)
    ).scalars().all()

    txs: list[Any] = []
    meta_available = False
    if Transaction is not None:
        meta_available = _transactions_meta_available(db)
        tx_query = (
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.transaction_date.desc().nullslast())
            .limit(500)
        )
        if not meta_available:
            tx_query = tx_query.options(
                load_only(
                    Transaction.id,
                    Transaction.user_id,
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
        txs = db.execute(tx_query).scalars().all()

    results: list[SubscriptionOut] = []
    for s in subs:
        amounts = _compute_amounts(s, txs, meta_available=meta_available)
        latest_amount = amounts["latest_amount"]
        previous_amount = amounts["previous_amount"]
        estimated_amount = amounts["estimated_amount"]
        next_amount = latest_amount or estimated_amount
        amount_is_estimated = latest_amount is None and estimated_amount is not None
        price_increased = (
            latest_amount is not None
            and previous_amount is not None
            and latest_amount > previous_amount
        )
        price_change_pct = None
        if latest_amount is not None and previous_amount not in (None, 0):
            price_change_pct = ((latest_amount - previous_amount) / previous_amount) * 100.0

        product_name, product_id, provider = _compute_product_fields(
            s,
            txs,
            meta_available=meta_available,
        )
        subheader = None
        if provider:
            subheader = product_name or product_id

        results.append(
            SubscriptionOut(
                id=s.id,
                vendor_name=s.vendor_name,
                subheader=subheader,
                amount=_safe_float(s.amount),
                currency=s.currency,
                billing_cycle_days=s.billing_cycle_days,
                last_charge_date=s.last_charge_date,
                next_renewal_date=s.next_renewal_date,
                trial_end_date=s.trial_end_date,
                status=getattr(s.status, "value", str(s.status)),
                next_amount=next_amount,
                amount_is_estimated=amount_is_estimated,
                price_increased=price_increased,
                previous_amount=previous_amount,
                price_change_pct=price_change_pct,
                product_name=product_name,
                product_id=product_id,
                provider=provider,
            )
        )
    return results


@router.get("/", response_model=list[SubscriptionOut])
@limiter.limit("60/minute")
def list_subscriptions_slash(
    request: Request,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    order_by: str = Query(
        "next_renewal_date",
        pattern="^(next_renewal_date|next_renewal_date_desc|last_charge_date|amount_desc|amount_asc)$",
    ),
    min_amount: float | None = Query(None, ge=0),
    max_amount: float | None = Query(None, ge=0),
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = Query(None, max_length=100),
):
    return list_subscriptions(
        request=request,
        user_id=user_id,
        db=db,
        limit=limit,
        offset=offset,
        order_by=order_by,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
        search=search,
    )



@router.post("/{subscription_id}/ignore", response_model=dict)
def ignore_subscription(
    subscription_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    s = db.query(Subscription).filter(
        Subscription.id == subscription_id,
        Subscription.user_id == user_id
    ).first()

    if not s:
        raise HTTPException(status_code=404, detail="Not found")

    s.status = SubscriptionStatus.ignored
    db.commit()
    return {"ok": True}


# -----------------------------
# Insights endpoint for pop-up sheet
# -----------------------------

@router.get("/{subscription_id}/insights", response_model=SubscriptionInsightsOut)
def subscription_insights(
    subscription_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    s = db.query(Subscription).filter(
        Subscription.id == subscription_id,
        Subscription.user_id == user_id
    ).first()

    if not s:
        raise HTTPException(status_code=404, detail="Not found")

    meta = _get_meta(s)

    # Confidence + reasons stored in meta from recompute_subscriptions (or computed fallback)
    confidence = float(meta.get("confidence", 0.65))
    reasons = meta.get("reasons") or meta.get("reasoning") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]

    cadence_days = (
        meta.get("cadence_days")
        or meta.get("billing_cycle_days")
        or s.billing_cycle_days
    )
    cadence_variance_days = meta.get("cadence_variance_days") or meta.get("cadence_variance") or None

    predicted_next = meta.get("predicted_next_renewal_date") or meta.get("predicted_next_renewal") or None
    predicted_next = _parse_date_maybe(predicted_next)

    predicted_is_estimated = bool(meta.get("predicted_is_estimated", False))

    # Evidence charges:
    # Prefer exact transaction ids written by recompute_subscriptions into meta["evidence_tx_ids"].
    evidence: list[EvidenceChargeOut] = []
    evidence_tx_ids = meta.get("evidence_tx_ids")

    if Transaction is not None and isinstance(evidence_tx_ids, list) and evidence_tx_ids:
        ids: list[int] = []
        for raw in evidence_tx_ids:
            val = _parse_int_maybe(raw)
            if val is not None:
                ids.append(val)

        if ids:
            txs = (
                db.execute(
                    select(Transaction)
                    .where(
                        Transaction.user_id == user_id,
                        Transaction.id.in_(ids),
                    )
                    .order_by(Transaction.transaction_date.desc().nullslast())
                )
                .scalars()
                .all()
            )

            for tx in txs[:6]:
                evidence.append(
                    EvidenceChargeOut(
                        id=int(getattr(tx, "id")),
                        date=getattr(tx, "transaction_date", None),
                        amount=_safe_float(getattr(tx, "amount", None)),
                        currency=getattr(tx, "currency", None),
                    )
                )

    # Fallback for older subscriptions that don't have evidence ids yet:
    if Transaction is not None and not evidence:
        target = _normalize_vendor(getattr(s, "vendor_name", "") or "")
        txs = db.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.transaction_date.desc().nullslast())
            .limit(250)
        ).scalars().all()

        matched = []
        for tx in txs:
            v = _normalize_vendor(getattr(tx, "vendor", "") or "")
            if v and target and v == target:
                matched.append(tx)

        for tx in matched[:6]:
            evidence.append(
                EvidenceChargeOut(
                    id=int(getattr(tx, "id")),
                    date=getattr(tx, "transaction_date", None),
                    amount=_safe_float(getattr(tx, "amount", None)),
                    currency=getattr(tx, "currency", None),
                )
            )

        if not evidence and target:
            for tx in txs:
                v_raw = (getattr(tx, "vendor", "") or "").strip().lower()
                v_norm = _normalize_vendor(v_raw)
                if v_norm and (target in v_norm or v_norm in target):
                    evidence.append(
                        EvidenceChargeOut(
                            id=int(getattr(tx, "id")),
                            date=getattr(tx, "transaction_date", None),
                            amount=_safe_float(getattr(tx, "amount", None)),
                            currency=getattr(tx, "currency", None),
                        )
                    )
                if len(evidence) >= 6:
                    break

    # If we still have no reasons, generate minimal explainable reasons
    if not reasons:
        reasons = []
        if cadence_days:
            reasons.append(f"Detected recurring pattern (~{cadence_days} days).")
        if s.last_charge_date:
            reasons.append("Recent charge found.")
        if s.trial_end_date:
            reasons.append("Trial end date detected.")
        if not reasons:
            reasons.append("Detected from email receipts and charge history.")

    return SubscriptionInsightsOut(
        id=s.id,
        vendor_name=s.vendor_name,
        status=getattr(s.status, "value", str(s.status)),
        amount=_safe_float(s.amount),
        currency=s.currency,
        billing_cycle_days=s.billing_cycle_days,
        last_charge_date=s.last_charge_date,
        next_renewal_date=s.next_renewal_date,
        trial_end_date=s.trial_end_date,
        confidence=confidence,
        reasons=[str(r) for r in reasons],
        cadence_days=_parse_int_maybe(cadence_days),
        cadence_variance_days=_parse_float_maybe(cadence_variance_days),
        predicted_next_renewal_date=predicted_next,
        predicted_is_estimated=predicted_is_estimated,
        evidence_charges=evidence,
    )
