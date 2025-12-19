from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.worker.celery_app import celery_app
from app.db import SessionLocal
from app.config import settings
from app.models import GoogleAccount, EmailIndex, Transaction, AuditLog
from app.security import token_cipher
from app.gmail_client import build_gmail_service, list_messages, get_message
from app.extraction import rules_extract, extract_headers, get_plain_text_parts
from app.llm import get_llm
from app.subscriptions import recompute_subscriptions
from app.alerts import schedule_alerts

logger = logging.getLogger("app.worker.tasks")


def _db() -> Session:
    return SessionLocal()


def _to_date(v):
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


@celery_app.task(name="app.worker.tasks.sync_user", bind=True)
def sync_user(self, user_id: int, google_account_id: int, lookback_days: int | None = None) -> dict:
    task_id = getattr(self.request, "id", None)
    logger.info("sync_user start task_id=%s user_id=%s google_account_id=%s lookback_days=%s", task_id, user_id, google_account_id, lookback_days)

    db = _db()
    try:
        acct = (
            db.query(GoogleAccount)
            .filter(GoogleAccount.id == google_account_id, GoogleAccount.user_id == user_id)
            .first()
        )
        if not acct:
            return {"ok": False, "error": "account not found"}

        refresh = token_cipher.decrypt(acct.refresh_token_enc)
        days = int(lookback_days or settings.SYNC_LOOKBACK_DAYS)
        since_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()

        q = f"{settings.GMAIL_QUERY} after:{since_date.strftime('%Y/%m/%d')}"
        logger.info("sync_user gmail query=%s", q)

        svc = build_gmail_service(
            acct.access_token,
            refresh,
            settings.GOOGLE_CLIENT_ID,
            settings.GOOGLE_CLIENT_SECRET,
        )

        indexed_new = 0
        skipped_existing = 0
        page_token = None
        page = 0

        # -------- Index emails --------
        while True:
            page += 1
            resp = list_messages(svc, q, page_token=page_token, max_results=100)
            msgs = resp.get("messages", []) or []
            page_token = resp.get("nextPageToken")
            logger.info("sync_user page=%s fetched=%s has_next=%s", page, len(msgs), bool(page_token))

            for m in msgs:
                mid = m.get("id")
                if not mid:
                    continue

                exists = (
                    db.query(EmailIndex)
                    .filter(EmailIndex.google_account_id == acct.id, EmailIndex.gmail_message_id == mid)
                    .first()
                )
                if exists:
                    skipped_existing += 1
                    continue

                full = get_message(svc, mid, format="full")
                headers = extract_headers(full)

                internal_ms_raw = full.get("internalDate", "0")
                try:
                    internal_ms = int(internal_ms_raw)
                except Exception:
                    internal_ms = 0

                db.add(
                    EmailIndex(
                        google_account_id=acct.id,
                        gmail_message_id=mid,
                        gmail_thread_id=full.get("threadId"),
                        internal_date_ms=internal_ms,
                        from_email=headers.get("from"),
                        subject=headers.get("subject"),
                        processed=False,
                    )
                )
                indexed_new += 1

            db.commit()
            if not page_token:
                break

        logger.info("sync_user indexing complete indexed_new=%s skipped_existing=%s", indexed_new, skipped_existing)

        # -------- Process pending --------
        llm = get_llm()
        pending = (
            db.execute(
                select(EmailIndex).where(
                    EmailIndex.google_account_id == acct.id,
                    EmailIndex.processed.is_(False),
                )
            )
            .scalars()
            .all()
        )
        logger.info("sync_user pending emails=%s", len(pending))

        processed = 0
        tx_created = 0

        for idx in pending:
            try:
                full = get_message(svc, idx.gmail_message_id, format="full")
                extracted = rules_extract(full)

                payload = full.get("payload", {}) or {}
                text = get_plain_text_parts(payload)
                headers = extract_headers(full)

                if text:
                    import asyncio
                    ai = asyncio.run(
                        llm.extract_transaction(
                            email_subject=headers.get("subject", ""),
                            email_from=headers.get("from", ""),
                            email_snippet=full.get("snippet", "") or "",
                            email_text=text,
                        )
                    )
                    if isinstance(ai, dict):
                        for k in [
                            "vendor",
                            "amount",
                            "currency",
                            "transaction_date",
                            "category",
                            "is_subscription",
                            "trial_end_date",
                            "renewal_date",
                            "confidence",
                        ]:
                            if ai.get(k) not in (None, "", {}):
                                extracted[k] = ai[k]

                db.add(
                    Transaction(
                        user_id=user_id,
                        google_account_id=acct.id,
                        gmail_message_id=idx.gmail_message_id,
                        vendor=extracted.get("vendor"),
                        amount=extracted.get("amount"),
                        currency=extracted.get("currency"),
                        transaction_date=_to_date(extracted.get("transaction_date")),
                        category=extracted.get("category"),
                        is_subscription=bool(extracted.get("is_subscription", False)),
                        trial_end_date=_to_date(extracted.get("trial_end_date")),
                        renewal_date=_to_date(extracted.get("renewal_date")),
                        confidence=extracted.get("confidence"),
                    )
                )
                tx_created += 1

                idx.processed = True
                idx.processed_at = datetime.now(timezone.utc)
                processed += 1
                db.commit()

            except Exception as e:
                db.add(
                    AuditLog(
                        user_id=user_id,
                        action="email_process_error",
                        meta={"gmail_message_id": idx.gmail_message_id, "error": str(e)},
                    )
                )
                db.commit()

        logger.info("sync_user processing complete processed=%s tx_created=%s", processed, tx_created)

        # ✅ Only recompute if we actually created new transactions
        if tx_created > 0:
            recompute_subscriptions(db, user_id=user_id)
        else:
            logger.info("sync_user recompute_subscriptions skipped (no new tx)")

        acct.last_sync_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                user_id=user_id,
                action="sync_complete",
                meta={
                    "google_account_id": acct.id,
                    "indexed_new": indexed_new,
                    "skipped_existing": skipped_existing,
                    "processed": processed,
                    "tx_created": tx_created,
                },
            )
        )
        db.commit()

        tx_total = db.query(Transaction).filter(Transaction.user_id == user_id).count()
        sub_total = db.query("subscriptions").from_statement(
            select(func.count()).select_from(__import__("app.models").models.Subscription).where(__import__("app.models").models.Subscription.user_id == user_id)
        ).scalar() if False else None  # (kept simple; debug router will show counts)

        logger.info("sync_user done user_id=%s tx_created=%s tx_total=%s", user_id, tx_created, tx_total)

        return {
            "ok": True,
            "indexed_new": indexed_new,
            "skipped_existing": skipped_existing,
            "processed": processed,
            "tx_created": tx_created,
            "tx_total": tx_total,
        }

    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.run_alert_scheduler")
def run_alert_scheduler() -> dict:
    db = _db()
    try:
        n = schedule_alerts(db)
        db.add(AuditLog(user_id=None, action="alerts_scheduled", meta={"count": n}))
        db.commit()
        return {"ok": True, "scheduled": n}
    finally:
        db.close()
