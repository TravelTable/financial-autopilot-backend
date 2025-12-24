from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
from app.extractors.apple_receipt import (
    build_subscription_key,
    estimate_confidence,
    extract_with_llm as extract_apple_with_llm,
    is_apple_receipt,
    parse_apple_receipt,
)
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
    if isinstance(v, Decimal):
        return float(v)
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

_GENERIC_BILLING_PROVIDERS = {
    "apple",
    "apple app store",
    "apple subscription",
    "app store",
    "itunes",
    "google",
    "google play",
    "amazon",
    "amazon pay",
    "paypal",
    "stripe",
    "microsoft",
}


def _is_generic_billing_provider(vendor: str | None) -> bool:
    if not vendor:
        return False
    return vendor.strip().lower() in _GENERIC_BILLING_PROVIDERS


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
    if _is_generic_billing_provider(extracted.get("vendor")) and len(text) > 200:
        return True

    return False


_NEWSLETTER_HINTS = (
    "weekly digest",
    "daily digest",
    "newsletter",
    "top stories",
    "read online",
    "view online",
    "view in browser",
    "morning roundup",
    "this week",
    "latest news",
)

_FINANCIAL_KEYWORDS = (
    "receipt",
    "invoice",
    "payment",
    "paid",
    "charged",
    "billing",
    "bill",
    "subscription",
    "renewal",
    "order confirmation",
)

_CURRENCY_REGEX = re.compile(r"[$€£¥₹]|\b(?:usd|eur|gbp|cad|aud|jpy|cny|inr|mxn|brl|chf)\b", re.I)
_AMOUNT_REGEX = re.compile(r"\b\d{1,3}(?:[\d,]*)(?:\.\d{2})\b")
_ORDER_ID_REGEX = re.compile(r"\b(order|transaction|invoice|receipt)\s*(?:number|no\.?|#|id)\b", re.I)


def _has_financial_signal(subject: str, snippet: str, text: str) -> bool:
    content = " ".join([subject or "", snippet or "", text or ""]).lower()
    if any(keyword in content for keyword in _FINANCIAL_KEYWORDS):
        return True
    if _CURRENCY_REGEX.search(content) or _AMOUNT_REGEX.search(content):
        return True
    if _ORDER_ID_REGEX.search(content):
        return True
    return False


def _is_newsletter_digest(subject: str, snippet: str, text: str) -> bool:
    content = " ".join([subject or "", snippet or "", text or ""]).lower()
    return any(hint in content for hint in _NEWSLETTER_HINTS)


def _is_bulk_mail(subject: str, snippet: str, text: str) -> bool:
    """
    Only skip obvious newsletters/digests that lack financial signals.
    """
    if _has_financial_signal(subject, snippet, text):
        return False
    return _is_newsletter_digest(subject, snippet, text)


def _subscription_has_concrete_evidence(*, amount: float | None, trial_end: date | None, renewal_date: date | None) -> bool:
    return bool(amount is not None or trial_end or renewal_date)


def _enrich_extraction(
    *,
    headers: dict,
    snippet: str,
    text_plain: str,
    text_html: str,
    extracted: dict,
    llm,
    force_llm: bool = False,
) -> tuple[dict, dict | None, bool, str | None, bool | None]:
    apple_meta = None
    billing_provider = None
    llm_used = False
    llm_error = None
    llm_classification = None
    apple_receipt_found = False
    text = text_plain or text_html or ""

    if is_apple_receipt(headers.get("subject", ""), headers.get("from", ""), text_plain, text_html):
        apple_receipt_found = True
        logger.info("sync_user apple receipt detected")
        apple_receipt = parse_apple_receipt(text_plain, text_html)
        apple_confidence = estimate_confidence(apple_receipt)
        if apple_confidence < 0.5:
            apple_receipt = extract_apple_with_llm(text_plain, text_html) or apple_receipt
            apple_confidence = estimate_confidence(apple_receipt)
        if apple_receipt and not apple_receipt.subscription_display_name and not apple_receipt.app_name:
            apple_receipt = extract_apple_with_llm(text_plain, text_html) or apple_receipt
            apple_confidence = estimate_confidence(apple_receipt)

        if apple_receipt:
            subscription_key = build_subscription_key(apple_receipt)
            subscription_name = apple_receipt.subscription_display_name or apple_receipt.app_name
            extracted.update(
                {
                    "vendor": subscription_name or "Apple App Store",
                    "amount": apple_receipt.amount,
                    "currency": apple_receipt.currency,
                    "transaction_date": apple_receipt.purchase_date_utc,
                    "category": "Subscriptions",
                    "is_subscription": bool(
                        apple_receipt.subscription_display_name
                        or apple_receipt.raw_signals.get("subscription_terms")
                    ),
                }
            )
            billing_provider = "Apple App Store"
            apple_meta = {
                "app_name": apple_receipt.app_name,
                "developer_or_seller": apple_receipt.developer_or_seller,
                "subscription_display_name": apple_receipt.subscription_display_name,
                "amount": str(apple_receipt.amount) if apple_receipt.amount is not None else None,
                "currency": apple_receipt.currency,
                "purchase_date_utc": (
                    apple_receipt.purchase_date_utc.isoformat()
                    if apple_receipt.purchase_date_utc
                    else None
                ),
                "order_id": apple_receipt.order_id,
                "original_order_id": apple_receipt.original_order_id,
                "country": apple_receipt.country,
                "family_sharing": apple_receipt.family_sharing,
                "subscription_key": subscription_key,
                "raw_signals": apple_receipt.raw_signals,
            }

    raw_vendor = extracted.get("vendor")
    if text and (force_llm or (not apple_receipt_found and _is_llm_candidate(
        headers=headers,
        snippet=snippet,
        text=text,
        extracted=extracted,
    ))):
        try:
            llm_classification = _run_async(
                llm.classify_receipt(
                    email_subject=headers.get("subject", ""),
                    email_from=headers.get("from", ""),
                    email_snippet=snippet,
                    email_text=text,
                    email_list_unsubscribe=headers.get("list-unsubscribe"),
                )
            )
            if llm_classification is not False:
                ai = _run_async(
                    llm.extract_transaction(
                        email_subject=headers.get("subject", ""),
                        email_from=headers.get("from", ""),
                        email_snippet=snippet,
                        email_text=text,
                        email_list_unsubscribe=headers.get("list-unsubscribe"),
                    )
                )
                llm_used = True
            else:
                ai = None
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
        except Exception as e:
            llm_error = str(e)

    vendor = extracted.get("vendor")
    if not billing_provider and raw_vendor and vendor and raw_vendor != vendor:
        if _is_generic_billing_provider(raw_vendor):
            billing_provider = raw_vendor

    meta: dict[str, Any] | None = None
    if apple_meta or billing_provider:
        meta = {}
        if apple_meta:
            meta["apple"] = apple_meta
        if billing_provider:
            meta["billing_provider"] = billing_provider

    return extracted, meta, llm_used, llm_error, llm_classification

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
def sync_user(
    self,
    user_id: int,
    google_account_id: int,
    lookback_days: int | None = None,
    force_reprocess: bool = False,
) -> dict:
    task_id = getattr(self.request, "id", None)
    logger.info(
        "sync_user start task_id=%s user_id=%s google_account_id=%s lookback_days=%s force_reprocess=%s",
        task_id,
        user_id,
        google_account_id,
        lookback_days,
        force_reprocess,
    )

    db = _db()
    acct = None
    try:
        acct = (
            db.query(GoogleAccount)
            .filter(GoogleAccount.id == google_account_id, GoogleAccount.user_id == user_id)
            .first()
        )
        if not acct:
            return {"ok": False, "error": "account not found"}

        now = datetime.now(timezone.utc)
        acct.sync_state = "in_progress"
        acct.sync_started_at = now
        acct.sync_completed_at = None
        acct.sync_failed_at = None
        acct.sync_error_message = None
        acct.sync_queued = False
        acct.sync_in_progress = True
        db.commit()

        refresh = token_cipher.decrypt(acct.refresh_token_enc)
        if lookback_days is None:
            q = settings.GMAIL_QUERY
        else:
            days = int(lookback_days)
            if days <= 0:
                q = settings.GMAIL_QUERY
            else:
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
        skipped_bulk_newsletter = 0
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
        tx_exists = (
            select(Transaction.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.google_account_id == acct.id,
                Transaction.gmail_message_id == EmailIndex.gmail_message_id,
            )
            .exists()
        )
        pending_query = select(EmailIndex).where(EmailIndex.google_account_id == acct.id)
        if force_reprocess:
            pending_query = pending_query.where(~tx_exists)
        else:
            pending_query = pending_query.where(EmailIndex.processed.is_(False))

        pending = db.execute(pending_query).scalars().all()
        logger.info("sync_user pending emails=%s force_reprocess=%s", len(pending), force_reprocess)

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

                payload = full.get("payload", {}) or {}
                text_plain = get_plain_text_parts(payload) or ""
                text_html = get_html_parts(payload) or ""
                text = text_plain or text_html or ""
                headers = extract_headers(full)
                snippet = full.get("snippet", "") or ""
                extracted = rules_extract(full, text_plain=text_plain, text_html=text_html)

                if _is_bulk_mail(headers.get("subject") or "", snippet, text):
                    logger.info(
                        "sync_user bulk mail skipped gmail_message_id=%s subject=%s from=%s",
                        idx.gmail_message_id,
                        headers.get("subject"),
                        headers.get("from"),
                    )
                    idx.processed = True
                    idx.processed_at = datetime.now(timezone.utc)
                    skipped_bulk_newsletter += 1
                    processed += 1
                    continue

                apple_meta = None
                billing_provider = None
                if is_apple_receipt(headers.get("subject", ""), headers.get("from", ""), text_plain, text_html):
                    logger.info("sync_user apple receipt detected gmail_message_id=%s", idx.gmail_message_id)
                    apple_receipt = parse_apple_receipt(text_plain, text_html)
                    apple_confidence = estimate_confidence(apple_receipt)
                    if apple_confidence < 0.5:
                        apple_receipt = extract_apple_with_llm(text_plain, text_html) or apple_receipt
                        apple_confidence = estimate_confidence(apple_receipt)
                    if apple_receipt and not apple_receipt.subscription_display_name and not apple_receipt.app_name:
                        apple_receipt = extract_apple_with_llm(text_plain, text_html) or apple_receipt
                        apple_confidence = estimate_confidence(apple_receipt)

                    if apple_receipt:
                        subscription_key = build_subscription_key(apple_receipt)
                        subscription_name = (
                            apple_receipt.subscription_display_name
                            or apple_receipt.app_name
                        )
                        logger.info(
                            "sync_user apple receipt parsed gmail_message_id=%s subscription_key=%s app_name=%s "
                            "subscription_display_name=%s amount=%s",
                            idx.gmail_message_id,
                            subscription_key,
                            apple_receipt.app_name,
                            apple_receipt.subscription_display_name,
                            apple_receipt.amount,
                        )
                        extracted.update(
                            {
                                "vendor": subscription_name or "Apple App Store",
                                "amount": apple_receipt.amount,
                                "currency": apple_receipt.currency,
                                "transaction_date": apple_receipt.purchase_date_utc,
                                "category": "Subscriptions",
                                "is_subscription": bool(
                                    apple_receipt.subscription_display_name
                                    or apple_receipt.raw_signals.get("subscription_terms")
                                ),
                            }
                        )
                        billing_provider = "Apple App Store"
                        apple_meta = {
                            "app_name": apple_receipt.app_name,
                            "developer_or_seller": apple_receipt.developer_or_seller,
                            "subscription_display_name": apple_receipt.subscription_display_name,
                            "amount": str(apple_receipt.amount) if apple_receipt.amount is not None else None,
                            "currency": apple_receipt.currency,
                            "purchase_date_utc": (
                                apple_receipt.purchase_date_utc.isoformat()
                                if apple_receipt.purchase_date_utc
                                else None
                            ),
                            "order_id": apple_receipt.order_id,
                            "original_order_id": apple_receipt.original_order_id,
                            "country": apple_receipt.country,
                            "family_sharing": apple_receipt.family_sharing,
                            "subscription_key": subscription_key,
                            "raw_signals": apple_receipt.raw_signals,
                        }

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
                llm_classification = None
                ai = None
                apple_receipt_found = apple_meta is not None
                raw_vendor = extracted.get("vendor")

                # Optional LLM enrichment (gated)
                if not apple_receipt_found and _is_llm_candidate(
                    headers=headers,
                    snippet=snippet,
                    text=text,
                    extracted=extracted,
                ):
                    try:
                        llm_classification = _run_async(
                            llm.classify_receipt(
                                email_subject=headers.get("subject", ""),
                                email_from=headers.get("from", ""),
                                email_snippet=snippet,
                                email_text=text,
                                email_list_unsubscribe=headers.get("list-unsubscribe"),
                            )
                        )
                        if llm_classification is not False:
                            ai = _run_async(
                                llm.extract_transaction(
                                    email_subject=headers.get("subject", ""),
                                    email_from=headers.get("from", ""),
                                    email_snippet=snippet,
                                    email_text=text,
                                    email_list_unsubscribe=headers.get("list-unsubscribe"),
                                )
                            )
                            llm_used = True
                        else:
                            ai = None
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
                    except Exception as e:
                        llm_error = str(e)
                if llm_error:
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
                is_subscription = bool(extracted.get("is_subscription", False))
                if is_subscription and not _subscription_has_concrete_evidence(
                    amount=amount, trial_end=trial_end, renewal_date=renewal_date
                ):
                    is_subscription = False
                if not billing_provider and raw_vendor and vendor and raw_vendor != vendor:
                    if _is_generic_billing_provider(raw_vendor):
                        billing_provider = raw_vendor

                # Store confidence as JSON, and record provenance (rules vs llm)
                conf_obj = extracted.get("confidence")
                if conf_obj is None or not isinstance(conf_obj, dict):
                    conf_obj = {}

                conf_obj.setdefault("source", "llm+rules" if llm_used else "rules")
                if llm_error:
                    conf_obj["llm_error"] = llm_error
                if llm_classification is False:
                    conf_obj["llm_classification"] = "not_receipt"
                if extracted.get("is_subscription") and not is_subscription:
                    conf_obj["subscription_downgraded"] = "missing_amount_or_dates"

                meta: dict[str, Any] | None = None
                if apple_meta or billing_provider:
                    meta = {}
                    if apple_meta:
                        meta["apple"] = apple_meta
                    if billing_provider:
                        meta["billing_provider"] = billing_provider

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
                        meta=meta,
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

        logger.info(
            "sync_user processing complete processed=%s tx_created=%s skipped_bulk_newsletter=%s",
            processed,
            tx_created,
            skipped_bulk_newsletter,
        )

        # Only recompute if we actually created new transactions
        if tx_created > 0:
            recompute_subscriptions(db, user_id=user_id)
        else:
            logger.info("sync_user recompute_subscriptions skipped (no new tx)")

        now = datetime.now(timezone.utc)
        acct.last_sync_at = now
        acct.sync_completed_at = now
        acct.sync_state = "completed"
        acct.sync_in_progress = False
        acct.sync_queued = False
        acct.sync_failed_at = None
        acct.sync_error_message = None
        db.add(
            AuditLog(
                user_id=user_id,
                action="sync_complete",
                meta={
                    "google_account_id": acct.id,
                    "indexed_new": indexed_new,
                    "skipped_existing": skipped_existing,
                    "skipped_bulk_newsletter": skipped_bulk_newsletter,
                    "processed": processed,
                    "tx_created": tx_created,
                },
            )
        )
        logger.info(
            "sync_user summary indexed_new=%s skipped_existing=%s skipped_bulk_newsletter=%s processed=%s tx_created=%s",
            indexed_new,
            skipped_existing,
            skipped_bulk_newsletter,
            processed,
            tx_created,
        )
        db.commit()

        return {
            "ok": True,
            "indexed_new": indexed_new,
            "skipped_existing": skipped_existing,
            "processed": processed,
            "tx_created": tx_created,
        }
    except Exception as e:
        logger.exception(
            "sync_user failed task_id=%s user_id=%s google_account_id=%s",
            task_id,
            user_id,
            google_account_id,
        )
        if acct:
            now = datetime.now(timezone.utc)
            acct.sync_failed_at = now
            acct.sync_state = "failed"
            acct.sync_error_message = str(e)
            acct.sync_in_progress = False
            acct.sync_queued = False
            db.commit()
        raise

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


@celery_app.task(name="app.worker.tasks.reanalyze_transaction", bind=True)
def reanalyze_transaction(
    self,
    user_id: int,
    transaction_id: int,
    force_llm: bool = False,
) -> dict:
    db = _db()
    try:
        tx = (
            db.query(Transaction)
            .filter(Transaction.id == transaction_id, Transaction.user_id == user_id)
            .first()
        )
        if not tx:
            return {"ok": False, "error": "transaction not found"}

        raw = (
            db.query(EmailRaw)
            .filter(
                EmailRaw.google_account_id == tx.google_account_id,
                EmailRaw.gmail_message_id == tx.gmail_message_id,
            )
            .first()
        )
        if not raw:
            return {"ok": False, "error": "email payload not found"}

        message = {
            "payload": {"headers": raw.headers_json or []},
            "snippet": raw.snippet or "",
            "internalDate": str(raw.internal_date_ms or 0),
        }
        headers = extract_headers(message)
        extracted = rules_extract(message, text_plain=raw.text_plain or "", text_html=raw.text_html or "")
        llm = get_llm()
        extracted, meta, llm_used, llm_error, llm_classification = _enrich_extraction(
            headers=headers,
            snippet=raw.snippet or "",
            text_plain=raw.text_plain or "",
            text_html=raw.text_html or "",
            extracted=extracted,
            llm=llm,
            force_llm=force_llm,
        )

        vendor = extracted.get("vendor") or tx.vendor
        currency = extracted.get("currency") or tx.currency
        amount = _to_float(extracted.get("amount"))
        tx_date = _to_date(extracted.get("transaction_date"))
        trial_end = _to_date(extracted.get("trial_end_date"))
        renewal_date = _to_date(extracted.get("renewal_date"))
        if amount is None:
            amount = tx.amount
        if tx_date is None:
            tx_date = tx.transaction_date

        is_subscription = bool(extracted.get("is_subscription", False))
        if is_subscription and not _subscription_has_concrete_evidence(
            amount=amount, trial_end=trial_end, renewal_date=renewal_date
        ):
            is_subscription = False

        conf_obj = extracted.get("confidence")
        if conf_obj is None or not isinstance(conf_obj, dict):
            conf_obj = {}

        conf_obj.setdefault("source", "llm+rules" if llm_used else "rules")
        if llm_error:
            conf_obj["llm_error"] = llm_error
        if llm_classification is False:
            conf_obj["llm_classification"] = "not_receipt"
        if extracted.get("is_subscription") and not is_subscription:
            conf_obj["subscription_downgraded"] = "missing_amount_or_dates"

        tx.vendor = vendor
        tx.amount = amount
        tx.currency = currency
        tx.transaction_date = tx_date
        tx.category = extracted.get("category") or tx.category
        tx.is_subscription = is_subscription
        tx.trial_end_date = trial_end
        tx.renewal_date = renewal_date
        tx.confidence = conf_obj
        tx.meta = meta or tx.meta

        db.add(
            AuditLog(
                user_id=user_id,
                action="transaction_reanalyzed",
                meta={
                    "transaction_id": tx.id,
                    "force_llm": force_llm,
                },
            )
        )
        db.commit()

        recompute_subscriptions(db, user_id=user_id)
        db.commit()

        return {"ok": True, "transaction_id": tx.id}
    finally:
        db.close()
