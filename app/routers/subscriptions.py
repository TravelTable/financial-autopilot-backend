from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_current_user_id
from app.db import get_db
from app.models import Subscription, SubscriptionStatus
from app.schemas import SubscriptionOut, SubscriptionInsightsOut, EvidenceChargeOut

# Transaction model might exist in app.models
# We import it optionally to avoid hard crashes if name differs.
try:
    from app.models import Transaction  # type: ignore
except Exception:  # pragma: no cover
    Transaction = None  # type: ignore


router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# -----------------------------
# Helpers
# -----------------------------

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


def _parse_date_maybe(v: Any) -> Optional[date]:
    """
    JSON meta may store dates as ISO strings. Accept:
    - date
    - "YYYY-MM-DD"
    """
    if v is None:
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
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


# -----------------------------
# Existing list endpoints
# -----------------------------

@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    subs = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(
            Subscription.next_renewal_date.asc().nullslast(),
            Subscription.id.desc(),
        )
    ).scalars().all()

    return [
        SubscriptionOut(
            id=s.id,
            vendor_name=s.vendor_name,
            amount=_safe_float(s.amount),
            currency=s.currency,
            billing_cycle_days=s.billing_cycle_days,
            last_charge_date=s.last_charge_date,
            next_renewal_date=s.next_renewal_date,
            trial_end_date=s.trial_end_date,
            status=getattr(s.status, "value", str(s.status)),
        )
        for s in subs
    ]


@router.get("/", response_model=list[SubscriptionOut])
def list_subscriptions_slash(
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    return list_subscriptions(user_id=user_id, db=db)


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
