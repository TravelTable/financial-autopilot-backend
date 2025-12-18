from __future__ import annotations

import logging
import sys
import os
from datetime import datetime, timedelta, timezone, date
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.worker.celery_app import celery_app
from app.db import SessionLocal
from app.config import settings
from app.models import GoogleAccount, EmailIndex, Transaction, AuditLog, Subscription
from app.security import token_cipher
from app.gmail_client import build_gmail_service, list_messages, get_message
from app.extraction import rules_extract, extract_headers, get_plain_text_parts
from app.llm import get_llm
from app.subscriptions import recompute_subscriptions
from app.alerts import schedule_alerts


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )


_configure_logging()
logger = logging.getLogger("celery.tasks")


def _db() -> Session:
    return SessionLocal()


def _to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        # allow "YYYY-MM-DD" and ISO datetime strings
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            return None
    return None


@celery_app.task(name="app.worker.tasks.sync_user", bind=True)
def sync_user(self, user_id: int, google_account_id: int, lookback_days: int | None = None) -> dict:
    # NOTE: anything logged here appears in the *worker* service logs in Railway
    logger.info("sync_user start user_id=%s google_account_id=%s lookback_days=%s", user_id, google_account_id, lookback_days)

    db = _db()
    try:
        acct = (
            db.query(GoogleAccount)
            .filter(
                GoogleAccount.id == google_account_id,
                GoogleAccount.user_id == user_id,
            )
            .first()
        )

        if not acct:
            logger.warning("sync_user account not found user_id=%s google_account_id=%s", user_id, google_account_id)
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

        indexed = 0
        scanned = 0
        page_token = None

        # 1) Index Gmail messages into EmailIndex table
        while True:
            resp = list_messages(svc, q, page_token=page_token, max_results=100)
            msgs = resp.get("messages", []) or []
            page_token = resp.get("nextPageToken")

            scanned += len(msgs)

            for m in msgs:
                mid = m.get("id")
                if not mid:
                    continue

                exists = (
                    db.query(EmailIndex)
                    .filter(
                        EmailIndex.google_account_id == acct.id,
                        EmailIndex.gmail_message_id == mid,
                    )
                    .first()
                )
                if exists:
                    continue

                full = get_message(svc, mid, format="full")
                headers = extract_headers(full)

                db.add(
                    EmailIndex(
                        google_account_id=acct.id,
                        gmail_message_id=mid,
                        gmail_thread_id=full.get("threadId"),
                        internal_date_ms=int(full.get("internalDate", "0")),
                        from_email=headers.get("from"),
                        subject=headers.get("subject"),
                        processed=False,
                    )
                )
                indexed += 1

            db.commit()

            if not page_token:
                break

        logger.info("sync_user index complete scanned=%s indexed_new=%s", scanned, indexed)

        # 2) Process pending EmailIndex rows into Transaction rows
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

                # If we have text, try LLM enrichment (best effort)
                if text:
                    import asyncio

                    ai = asyncio.run(
                        llm.extract_transaction(
                            email_subject=headers.get("subject", "") or "",
                            email_from=headers.get("from", "") or "",
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

                # Avoid inserting duplicates if you re-run sync and EmailIndex is weird
                already_tx = (
                    db.query(Transaction.id)
                    .filter(
                        Transaction.user_id == user_id,
                        Transaction.google_account_id == acct.id,
                        Transaction.gmail_message_id == idx.gmail_message_id,
                    )
                    .first()
                )

                if not already_tx:
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
                logger.exception("sync_user email_process_error gmail_message_id=%s", idx.gmail_message_id)
                db.add(
                    AuditLog(
                        user_id=user_id,
                        action="email_process_error",
                        meta={
                            "gmail_message_id": idx.gmail_message_id,
                            "error": str(e),
                        },
                    )
                )
                db.commit()

        logger.info("sync_user processing complete processed=%s tx_created=%s", processed, tx_created)

        # 3) Recompute subscriptions
        try:
            recompute_subscriptions(db, user_id=user_id)
            db.commit()
            logger.info("sync_user recompute_subscriptions ok")
        except Exception:
            logger.exception("sync_user recompute_subscriptions failed")
            db.rollback()

        # 4) Log counts (this tells us instantly if the backend is producing data)
        tx_count = db.query(func.count(Transaction.id)).filter(Transaction.user_id == user_id).scalar() or 0
        sub_count = db.query(func.count(Subscription.id)).filter(Subscription.user_id == user_id).scalar() or 0

        logger.info("sync_user counts user_id=%s transactions=%s subscriptions=%s", user_id, tx_count, sub_count)

        acct.last_sync_at = datetime.now(timezone.utc)

        db.add(
            AuditLog(
                user_id=user_id,
                action="sync_complete",
                meta={
                    "google_account_id": acct.id,
                    "scanned": scanned,
                    "indexed": indexed,
                    "pending": len(pending),
                    "processed": processed,
                    "tx_created": tx_created,
                    "tx_count": tx_count,
                    "sub_count": sub_count,
                },
            )
        )
        db.commit()

        return {
            "ok": True,
            "scanned": scanned,
            "indexed": indexed,
            "pending": len(pending),
            "processed": processed,
            "tx_created": tx_created,
            "tx_count": tx_count,
            "sub_count": sub_count,
        }

    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.run_alert_scheduler")
def run_alert_scheduler() -> dict:
    db = _db()
    try:
        n = schedule_alerts(db)
        db.add(
            AuditLog(
                user_id=None,
                action="alerts_scheduled",
                meta={"count": n},
            )
        )
        db.commit()
        logger.info("run_alert_scheduler scheduled=%s", n)
        return {"ok": True, "scheduled": n}
    finally:
        db.close()
