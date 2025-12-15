from __future__ import annotations
from sqlalchemy.orm import Session
from app.models import Transaction, Vendor

DEFAULT_SUBJECT = "Request for Refund / Cancellation"

def template_refund_email(*, vendor: str, amount: str, date_str: str, reason: str, tone: str) -> tuple[str, str]:
    if tone == "strict":
        body = f"""Hello {vendor} Support,

I am requesting a refund/cancellation for the charge of {amount} on {date_str}.
Reason: {reason}

Please confirm the refund/cancellation and any reference number.

Regards,"""
    elif tone == "friendly":
        body = f"""Hi {vendor} Team,

Could you please help with a refund/cancellation for {amount} from {date_str}?
Reason: {reason}

Thanks,
—"""
    else:
        body = f"""Hello {vendor} Support,

I’d like to request a refund/cancellation for the charge of {amount} on {date_str}.
Reason: {reason}

Please confirm once processed.

Thank you,
—"""
    return DEFAULT_SUBJECT, body

def create_refund_draft(db: Session, *, user_id: int, transaction_id: int, reason: str, tone: str) -> dict:
    tx = db.query(Transaction).filter(Transaction.id == transaction_id, Transaction.user_id == user_id).first()
    if not tx:
        raise ValueError("Transaction not found")

    vendor_name = tx.vendor or "Support"
    amount_str = f"{tx.currency or ''} {float(tx.amount):.2f}" if tx.amount is not None else "the recent charge"
    date_str = tx.transaction_date.isoformat() if tx.transaction_date else "the recent date"

    to_email = None
    if tx.vendor_id:
        v = db.query(Vendor).filter(Vendor.id == tx.vendor_id).first()
        if v and v.support_email:
            to_email = v.support_email

    subject, body = template_refund_email(vendor=vendor_name, amount=amount_str, date_str=date_str, reason=reason, tone=tone)

    return {
        "to_email": to_email,
        "subject": subject,
        "body": body,
        "facts_used": {
            "vendor": vendor_name,
            "amount": amount_str,
            "date": date_str,
            "gmail_message_id": tx.gmail_message_id,
        },
    }
