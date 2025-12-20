from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_current_user_id
from app.db import get_db
from app.models import Subscription, SubscriptionStatus

# Transaction model might exist in app.models
# We import it optionally to avoid hard crashes if name differs.
try:
    from app.models import Transaction  # type: ignore
except Exception:  # pragma: no cover
    Transaction = None  # type: ignore

from app.schemas import SubscriptionOut, SubscriptionInsightsOut, EvidenceChargeOut


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


# -----------------------------
# Response models for insights
# -----------------------------




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
# NEW: Insights endpoint for pop-up sheet
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

    cadence_days = meta.get("cadence_days") or meta.get("billing_cycle_days") or s.billing_cycle_days
    cadence_variance_days = meta.get("cadence_variance_days") or meta.get("cadence_variance") or None

    predicted_next = meta.get("predicted_next_renewal_date") or meta.get("predicted_next_renewal") or None
    # predicted_next might be a string; keep it None unless it’s already a date object
    if predicted_next is not None and not isinstance(predicted_next, date):
        predicted_next = None

    predicted_is_estimated = bool(meta.get("predicted_is_estimated", False))

    # Evidence: last N charges for same vendor
    evidence: list[EvidenceChargeOut] = []
    if Transaction is not None:
        target = _normalize_vendor(getattr(s, "vendor_name", "") or "")
        # Pull recent user transactions then filter in Python for safety
        txs = db.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(getattr(Transaction, "transaction_date").desc())
            .limit(200)
        ).scalars().all()

        matched = []
        for tx in txs:
            v = _normalize_vendor(getattr(tx, "vendor", "") or "")
            if v and target and v == target:
                matched.append(tx)

        # Most recent first, keep last 6 as evidence
        for tx in matched[:6]:
            evidence.append(
                EvidenceChargeOut(
                    id=int(getattr(tx, "id")),
                    date=getattr(tx, "transaction_date", None),
                    amount=_safe_float(getattr(tx, "amount", None)),
                    currency=getattr(tx, "currency", None),
                )
            )

        # Fallback: if we found no exact normalized match, try loose “contains”
        if not evidence and target:
            for tx in txs:
                v_raw = (getattr(tx, "vendor", "") or "").strip().lower()
                if v_raw and (target in _normalize_vendor(v_raw) or _normalize_vendor(v_raw) in target):
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
        cadence_days=int(cadence_days) if isinstance(cadence_days, (int, float)) else None,
        cadence_variance_days=float(cadence_variance_days) if isinstance(cadence_variance_days, (int, float)) else None,
        predicted_next_renewal_date=predicted_next,
        predicted_is_estimated=predicted_is_estimated,
        evidence_charges=evidence,
    )
