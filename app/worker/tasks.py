from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select
from celery import shared_task

from app.db import SessionLocal
from app.config import settings
from app.models import GoogleAccount, EmailIndex, Transaction, AuditLog
from app.security import token_cipher
from app.gmail_client import build_gmail_service, list_messages, get_message
from app.extraction import rules_extract, extract_headers, get_plain_text_parts
from app.llm import get_llm
from app.subscriptions import recompute_subscriptions
from app.alerts import schedule_alerts

def _db() -> Session:
    return SessionLocal()

@shared_task(name="app.worker.tasks.sync_user")
def sync_user(user_id: int, google_account_id: int, lookback_days: int | None = None) -> dict:
    db = _db()
    try:
        acct = db.query(GoogleAccount).filter(GoogleAccount.id == google_account_id, GoogleAccount.user_id == user_id).first()
        if not acct:
            return {"ok": False, "error": "account not found"}

        refresh = token_cipher.decrypt(acct.refresh_token_enc)
        days = int(lookback_days or settings.SYNC_LOOKBACK_DAYS)
        since_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        q = f"{settings.GMAIL_QUERY} after:{since_date.strftime('%Y/%m/%d')}"

        svc = build_gmail_service(acct.access_token, refresh, settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET)

        indexed = 0
        page_token = None
        while True:
            resp = list_messages(svc, q, page_token=page_token, max_results=100)
            msgs = resp.get("messages", []) or []
            page_token = resp.get("nextPageToken")

            for m in msgs:
                mid = m.get("id")
                if not mid:
                    continue
                exists = db.query(EmailIndex).filter(EmailIndex.google_account_id == acct.id, EmailIndex.gmail_message_id == mid).first()
                if exists:
                    continue

                full = get_message(svc, mid, format="full")
                headers = extract_headers(full)
                db.add(EmailIndex(
                    google_account_id=acct.id,
                    gmail_message_id=mid,
                    gmail_thread_id=full.get("threadId"),
                    internal_date_ms=int(full.get("internalDate", "0")),
                    from_email=headers.get("from"),
                    subject=headers.get("subject"),
                    processed=False,
                ))
                indexed += 1

            db.commit()
            if not page_token:
                break

        llm = get_llm()
        pending = db.execute(select(EmailIndex).where(EmailIndex.google_account_id == acct.id, EmailIndex.processed == False)).scalars().all()  # noqa: E712
        processed = 0

        for idx in pending:
            try:
                full = get_message(svc, idx.gmail_message_id, format="full")
                extracted = rules_extract(full)

                payload = full.get("payload", {}) or {}
                text = get_plain_text_parts(payload)
                headers = extract_headers(full)

                # Optional AI merge
                if text:
                    import asyncio
                    ai = asyncio.run(llm.extract_transaction(
                        email_subject=headers.get("subject",""),
                        email_from=headers.get("from",""),
                        email_snippet=full.get("snippet","") or "",
                        email_text=text,
                    ))
                    if isinstance(ai, dict):
                        for k in ["vendor","amount","currency","transaction_date","category","is_subscription","trial_end_date","renewal_date","confidence"]:
                            if k in ai and ai[k] not in (None, "", {}):
                                extracted[k] = ai[k]

                def to_date(v):
                    if v is None:
                        return None
                    if hasattr(v, "isoformat"):
                        return v
                    if isinstance(v, str):
                        try:
                            return datetime.fromisoformat(v).date()
                        except Exception:
                            return None
                    return None

                db.add(Transaction(
                    user_id=user_id,
                    google_account_id=acct.id,
                    gmail_message_id=idx.gmail_message_id,
                    vendor=extracted.get("vendor"),
                    amount=extracted.get("amount"),
                    currency=extracted.get("currency"),
                    transaction_date=to_date(extracted.get("transaction_date")),
                    category=extracted.get("category"),
                    is_subscription=bool(extracted.get("is_subscription", False)),
                    trial_end_date=to_date(extracted.get("trial_end_date")),
                    renewal_date=to_date(extracted.get("renewal_date")),
                    confidence=extracted.get("confidence"),
                ))

                idx.processed = True
                idx.processed_at = datetime.now(timezone.utc)
                processed += 1
                db.commit()
            except Exception as e:
                db.add(AuditLog(user_id=user_id, action="email_process_error", meta={"gmail_message_id": idx.gmail_message_id, "error": str(e)}))
                db.commit()

        recompute_subscriptions(db, user_id=user_id)

        acct.last_sync_at = datetime.now(timezone.utc)
        db.add(AuditLog(user_id=user_id, action="sync_complete", meta={"google_account_id": acct.id, "indexed": indexed, "processed": processed}))
        db.commit()

        return {"ok": True, "indexed": indexed, "processed": processed}
    finally:
        db.close()

@shared_task(name="app.worker.tasks.run_alert_scheduler")
def run_alert_scheduler() -> dict:
    db = _db()
    try:
        n = schedule_alerts(db)
        db.add(AuditLog(user_id=None, action="alerts_scheduled", meta={"count": n}))
        db.commit()
        return {"ok": True, "scheduled": n}
    finally:
        db.close()
