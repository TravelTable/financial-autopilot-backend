# app/services/anomaly_detector.py
from typing import List
from datetime import datetime
import statistics
from sqlalchemy.orm import Session
from app.models import Transaction, User  # adjust to your project
from app.models_advanced import TransactionAnomaly, UserSettings, Vendor


SCAM_KEYWORDS = [
    "gift card",
    "bitcoin",
    "crypto",
    "urgent",
    "suspended",
    "account locked",
    "reset password",
    "verification code",
]


def _amount_z_score(amount: float, amounts: List[float]) -> float:
    if len(amounts) < 5:
        return 0.0
    mean = statistics.mean(amounts)
    stdev = statistics.pstdev(amounts)
    if stdev <= 0:
        return 0.0
    return abs((amount - mean) / stdev)


def score_transaction_anomaly(
    db: Session, user: User, tx: Transaction
) -> TransactionAnomaly | None:
    """
    Simple anomaly scoring:
    - large deviation from user's historical transaction amounts
    - suspicious keywords in description/subject
    """
    # don't rescore resolved anomalies
    if getattr(tx, "anomaly", None):
        if tx.anomaly.resolved:
            return tx.anomaly

    # gather history
    history: List[Transaction] = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id)
        .order_by(Transaction.date.asc())
        .all()
    )
    if not history:
        return None

    past_amounts = [h.amount for h in history if h.id != tx.id and h.amount is not None]
    if not past_amounts:
        return None

    z = _amount_z_score(tx.amount, past_amounts) if tx.amount is not None else 0.0

    # basic keyword scan
    desc = (tx.description or "") + " " + (tx.raw_merchant or "")
    desc_lower = desc.lower()

    keyword_hit = any(kw in desc_lower for kw in SCAM_KEYWORDS)

    score = 0.0
    label = None
    reason_parts = []

    if z >= 3:
        score += 0.6
        label = "unusual_amount"
        reason_parts.append(f"amount is {z:.1f}σ from your normal spending")

    if keyword_hit:
        score += 0.5
        if label:
            label = "possible_scam"
        else:
            label = "keyword_suspicious"
        reason_parts.append("suspicious wording in description/email")

    if score <= 0 or not label:
        return None

    reason = "; ".join(reason_parts)[:512]

    anomaly = TransactionAnomaly(
        transaction_id=tx.id,
        score=min(score, 1.0),
        label=label,
        reason=reason,
        created_at=datetime.utcnow(),
    )
    db.add(anomaly)
    db.commit()
    db.refresh(anomaly)
    return anomaly
