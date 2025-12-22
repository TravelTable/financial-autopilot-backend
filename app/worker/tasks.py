from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import schedule_alerts
from app.config import settings
from app.db import SessionLocal
from app.extraction import extract_headers, get_html_parts, get_plain_text_parts, rules_extract
from app.gmail_client import build_gmail_service, get_message, list_messages
from app.llm import get_llm
from app.models import AuditLog, EmailIndex, EmailRaw, GoogleAccount, Transaction
from app.security import token_cipher
from app.subscriptions import recompute_subscriptions
from app.worker.celery_app import celery_app

logger = logging.getLogger("app.worker.tasks")


def _db() -> Session:
    return SessionLocal()


def _to_date(v: Any) -> Optional[date]:
    """
    Convert various date-like inputs into a `date`:
    - date -> date
    - datetime -> datetime.date()
    - "YYYY-MM-DD" -> date
    - ISO datetime string -> date
    - epoch seconds/ms -> date
    """
    if v is None:
        return None

    if isinstance(v, datetime):
        return v.date()

    if isinstance(v, date):
        return v

    if isinstance(v, (int, float)):
        # treat as epoch seconds or milliseconds
        try:
            ts = float(v)
            if ts > 1e12:  # ms
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except Exception:
            return None

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None

        # Try date-only first
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            pass

        # Then full ISO datetime
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except Exception:
            pass

        # Try numeric string epoch
        try:
            ts = float(s)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except Exception:
            return None

    return None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


_SUBJECT_HINTS = (
    "receipt",
    "invoice",
    "subscription",
    "trial",
    "renewal",
    "payment",
    "charged",
    "your order",
    "order confirmation",
    "thank you for",
    "billing",
    "membership",
    "plan",
    "auto-renew",
    "subscribe",
    "active subscription",
)

_TRANSACTION_HINTS = (
    "receipt",
    "invoice",
    "payment received",
    "order confirmation",
    "charged",
    "payment",
    "billing",
    "purchase",
    "order",
)


def _is_marketing_message(headers: dict) -> bool:
    if "list-unsubscribe" in headers or "list-unsubscribe-post" in headers:
        return True
    precedence = (headers.get("precedence") or "").lower()
    return precedence in {"bulk", "list", "junk"}


def _is_llm_candidate(*, headers: dict, snippet: str, text: str, extracted: dict) -> bool:
    """
    Gate LLM calls so we only use it when it likely helps.
    - Saves cost
    - Reduces hallucinations
    """
    if not text:
        return False

    subj = (headers.get("subject") or "").lower()
    snip = (snippet or "").lower()
    has_marketing_headers = _is_marketing_message(headers)
    has_transaction_hints = any(h in subj for h in _TRANSACTION_HINTS) or any(h in snip for h in _TRANSACTION_HINTS)

    if has_marketing_headers and not has_transaction_hints:
        has_amount = extracted.get("amount") not in (None, "")
        has_dates = extracted.get("trial_end_date") or extracted.get("renewal_date")
        if not (has_amount or has_dates):
            return False

    # Strong hints in subject/snippet
    if any(h in subj for h in _SUBJECT_HINTS) or any(h in snip for h in _SUBJECT_HINTS):
        return True

    # If rules flagged it as subscription/trial/renewal, LLM can add structured details
    if extracted.get("is_subscription") or extracted.get("trial_end_date") or extracted.get("renewal_date"):
        return True

    # If rules missed core fields, LLM may help (only if we have enough text)
    missing_vendor = not extracted.get("vendor")
    missing_amount = extracted.get("amount") in (None, "")
    missing_date = not extracted.get("transaction_date")
    if (missing_vendor or missing_amount or missing_date) and len(text) > 200:
        return True

    return False


def _run_async(coro):
    """
    Run an async coroutine from a sync Celery worker safely.

    Celery tasks are typically sync. We'll run async extraction when needed.
    """
    try:
        loop = asyncio.get_running_loop()
        # If we already have a running loop (rare in Celery), schedule thread-safe
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result()
    except RuntimeError:
        # No running loop
        return asyncio.run(coro)


def _gmail_get_message_with_retry(svc, message_id: str, *, format: str = "full", tries: int = 3):
    """
    Gmail API can occasionally fail transiently. Simple backoff retry.
    """
    delay = 0.8
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            return get_message(svc, message_id, format=format)
        except Exception as e:
            last_err = e
            if attempt == tries:
                break
            time.sleep(delay)
            delay *= 1.8
    raise last_err  # type: ignore[misc]


@celery_app.task(name="app.worker.tasks.sync_user", bind=True)
def sync_user(self, user_id: int, google_account_id: int, lookback_days: int | None = None) -> dict:
    task_id = getattr(self.request, "id", None)
    logger.info(
        "sync_user start task_id=%s user_id=%s google_account_id=%s lookback_days=%s",
        task_id,
        user_id,
        google_account_id,
        lookback_days,
    )

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
        if lookback_days is None:
            q = settings.GMAIL_QUERY
        else:
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

                full = _gmail_get_message_with_retry(svc, mid, format="full")
                headers = extract_headers(full)
                payload = full.get("payload", {}) or {}
                text_plain = get_plain_text_parts(payload) or ""
                text_html = get_html_parts(payload) or ""
                snippet = full.get("snippet", "") or ""

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
                db.add(
                    EmailRaw(
                        google_account_id=acct.id,
                        gmail_message_id=mid,
                        gmail_thread_id=full.get("threadId"),
                        internal_date_ms=internal_ms,
                        headers_json=payload.get("headers", []) or [],
                        snippet=snippet,
                        text_plain=text_plain,
                        text_html=text_html,
                    )
                )
                indexed_new += 1

            db.commit()
            if not page_token:
                break

        logger.info(
            "sync_user indexing complete indexed_new=%s skipped_existing=%s",
            indexed_new,
            skipped_existing,
        )

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
        batch_count = 0

        for idx in pending:
            try:
                # Crash-retry safety: if we already wrote a transaction for this email, mark processed and skip.
                existing_tx = (
                    db.query(Transaction)
                    .filter(
                        Transaction.user_id == user_id,
                        Transaction.google_account_id == acct.id,
                        Transaction.gmail_message_id == idx.gmail_message_id,
                    )
                    .first()
                )
                if existing_tx:
                    idx.processed = True
                    idx.processed_at = datetime.now(timezone.utc)
                    processed += 1
                    continue

                full = _gmail_get_message_with_retry(svc, idx.gmail_message_id, format="full")
                extracted = rules_extract(full)

                payload = full.get("payload", {}) or {}
                text_plain = get_plain_text_parts(payload) or ""
                text_html = get_html_parts(payload) or ""
                text = text_plain or text_html or ""
                headers = extract_headers(full)
                snippet = full.get("snippet", "") or ""

                raw_exists = (
                    db.query(EmailRaw)
                    .filter(
                        EmailRaw.google_account_id == acct.id,
                        EmailRaw.gmail_message_id == idx.gmail_message_id,
                    )
                    .first()
                )
                if not raw_exists:
                    internal_ms_raw = full.get("internalDate", "0")
                    try:
                        internal_ms = int(internal_ms_raw)
                    except Exception:
                        internal_ms = 0
                    db.add(
                        EmailRaw(
                            google_account_id=acct.id,
                            gmail_message_id=idx.gmail_message_id,
                            gmail_thread_id=full.get("threadId"),
                            internal_date_ms=internal_ms,
                            headers_json=payload.get("headers", []) or [],
                            snippet=snippet,
                            text_plain=text_plain,
                            text_html=text_html,
                        )
                    )

                llm_used = False
                llm_error = None

                # Optional LLM enrichment (gated)
                if _is_llm_candidate(headers=headers, snippet=snippet, text=text, extracted=extracted):
                    try:
                        ai = _run_async(
                            llm.extract_transaction(
                                email_subject=headers.get("subject", ""),
                                email_from=headers.get("from", ""),
                                email_snippet=snippet,
                                email_text=text,
                            )
                        )
                        llm_used = True
                        if isinstance(ai, dict):
                            # Only overwrite if AI provides a meaningful value
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
                    except Exception as e:
                        llm_error = str(e)
                        db.add(
                            AuditLog(
                                user_id=user_id,
                                action="llm_extract_error",
                                meta={"gmail_message_id": idx.gmail_message_id, "error": llm_error},
                            )
                        )

                # Normalize types before insert
                vendor = extracted.get("vendor")
                currency = extracted.get("currency")
                amount = _to_float(extracted.get("amount"))
                tx_date = _to_date(extracted.get("transaction_date"))
                trial_end = _to_date(extracted.get("trial_end_date"))
                renewal_date = _to_date(extracted.get("renewal_date"))

                # Store confidence as JSON, and record provenance (rules vs llm)
                conf_obj = extracted.get("confidence")
                if conf_obj is None or not isinstance(conf_obj, dict):
                    conf_obj = {}

                conf_obj.setdefault("source", "llm+rules" if llm_used else "rules")
                if llm_error:
                    conf_obj["llm_error"] = llm_error
                if _is_marketing_message(headers):
                    conf_obj["marketing_header"] = True

                is_subscription = bool(extracted.get("is_subscription", False))
                if conf_obj.get("marketing_header") and is_subscription:
                    if not (amount or trial_end or renewal_date):
                        is_subscription = False
                        conf_obj["marketing_suspect"] = True

                db.add(
                    Transaction(
                        user_id=user_id,
                        google_account_id=acct.id,
                        gmail_message_id=idx.gmail_message_id,
                        vendor=vendor,
                        amount=amount,
                        currency=currency,
                        transaction_date=tx_date,
                        category=extracted.get("category"),
                        is_subscription=is_subscription,
                        trial_end_date=trial_end,
                        renewal_date=renewal_date,
                        confidence=conf_obj,
                    )
                )
                tx_created += 1

                idx.processed = True
                idx.processed_at = datetime.now(timezone.utc)
                processed += 1

                batch_count += 1
                if batch_count >= 25:
                    db.commit()
                    batch_count = 0

            except Exception as e:
                # Avoid infinite retry loops on one bad email:
                # log and mark processed so the queue can move on.
                db.add(
                    AuditLog(
                        user_id=user_id,
                        action="email_process_error",
                        meta={"gmail_message_id": idx.gmail_message_id, "error": str(e)},
                    )
                )
                try:
                    idx.processed = True
                    idx.processed_at = datetime.now(timezone.utc)
                except Exception:
                    pass
                db.commit()

        # Flush any remaining batch
        db.commit()

        logger.info("sync_user processing complete processed=%s tx_created=%s", processed, tx_created)

        # Only recompute if we actually created new transactions
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

        return {
            "ok": True,
            "indexed_new": indexed_new,
            "skipped_existing": skipped_existing,
            "processed": processed,
            "tx_created": tx_created,
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
