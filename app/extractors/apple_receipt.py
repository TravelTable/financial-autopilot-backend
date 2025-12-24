from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import html
import json
import re
from typing import Any

import httpx
from dateutil import parser as date_parser

from app.config import settings


@dataclass
class ParsedAppleReceipt:
    app_name: str | None
    developer_or_seller: str | None
    subscription_display_name: str | None
    amount: Decimal | None
    currency: str | None
    purchase_date_utc: datetime | None
    order_id: str | None
    original_order_id: str | None
    country: str | None
    family_sharing: bool | None
    raw_signals: dict[str, Any]


_APPLE_DOMAINS = (
    "email.apple.com",
    "itunes.com",
    "apple.com",
)

_STRONG_SIGNAL_PATTERNS = (
    re.compile(r"\bapple receipt\b", re.IGNORECASE),
    re.compile(r"\bapp store\b", re.IGNORECASE),
    re.compile(r"\bitunes store\b", re.IGNORECASE),
    re.compile(r"\bapple id\b", re.IGNORECASE),
    re.compile(r"\border id\b", re.IGNORECASE),
    re.compile(r"\bdocument no\b", re.IGNORECASE),
    re.compile(r"\binvoice\b", re.IGNORECASE),
)

_SUBSCRIPTION_TERMS = re.compile(
    r"\b(subscription|auto-renew|renewal|trial|billing|plan)\b", re.IGNORECASE
)

_AMOUNT_PATTERNS = (
    re.compile(
        r"(?P<label>Total|Amount|Price|Billed|Subtotal)\s*[:\-]?\s*(?P<currency>[A-Z]{3}|[A-Z]\$|\$|€|£)\s*(?P<amount>\d+[.,]\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<currency>[A-Z]{3}|[A-Z]\$|\$|€|£)\s*(?P<amount>\d+[.,]\d{2})\s*(?P<label>Total|Amount|Price|Billed)?",
        re.IGNORECASE,
    ),
)

_ORDER_ID_PATTERNS = (
    re.compile(r"(Order ID|Order Number|Order No\.|Order)\s*[:#]?\s*([A-Z0-9\-]+)", re.IGNORECASE),
)

_ORIGINAL_ORDER_ID_PATTERNS = (
    re.compile(r"Original Order ID\s*[:#]?\s*([A-Z0-9\-]+)", re.IGNORECASE),
)

_DATE_PATTERNS = (
    re.compile(r"(Order Date|Purchase Date|Date)\s*[:#]?\s*(.+)", re.IGNORECASE),
)

_APP_NAME_PATTERNS = (
    re.compile(r"(App|Purchased|Product|Item)\s*[:#]?\s*(.+)", re.IGNORECASE),
)

_SUBSCRIPTION_NAME_PATTERNS = (
    re.compile(r"(Subscription|In-App Purchase|Plan)\s*[:#]?\s*(.+)", re.IGNORECASE),
)

_SELLER_PATTERNS = (
    re.compile(r"(Seller|Developer)\s*[:#]?\s*(.+)", re.IGNORECASE),
)

_COUNTRY_PATTERNS = (
    re.compile(r"(Country/Region|Country)\s*[:#]?\s*(.+)", re.IGNORECASE),
)


def _normalize_text(body_text: str, html_text: str | None) -> str:
    raw = body_text or ""
    if html_text:
        raw += "\n" + _strip_html(html_text)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    return raw.strip()


def _strip_html(source: str) -> str:
    if not source:
        return ""
    text = re.sub(r"<script.*?</script>", " ", source, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _extract_domain(from_email: str) -> str | None:
    if not from_email:
        return None
    match = re.search(r"@([A-Za-z0-9\.-]+\.[A-Za-z]{2,})", from_email)
    if not match:
        return None
    return match.group(1).lower()


def is_apple_receipt(subject: str, from_email: str, body_text: str, html_text: str | None) -> bool:
    signals: list[str] = []
    combined = "\n".join([subject or "", body_text or "", html_text or ""])
    combined = combined.lower()

    domain = _extract_domain(from_email or "")
    if domain and any(domain.endswith(d) for d in _APPLE_DOMAINS):
        signals.append("from_domain")

    for pattern in _STRONG_SIGNAL_PATTERNS:
        if pattern.search(combined):
            signals.append(pattern.pattern)

    if "receipt" in combined and "apple" in combined:
        signals.append("apple_receipt_phrase")

    return len(set(signals)) >= 2


def parse_apple_receipt(body_text: str, html_text: str | None) -> ParsedAppleReceipt | None:
    if not body_text and not html_text:
        return None

    normalized = _normalize_text(body_text, html_text)
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]

    raw_signals: dict[str, Any] = {}
    amount = currency = None
    order_id = None
    original_order_id = None
    app_name = None
    subscription_display = None
    developer_or_seller = None
    country = None
    family_sharing = None
    purchase_date = None

    for line in lines:
        if amount is None:
            for pattern in _AMOUNT_PATTERNS:
                match = pattern.search(line)
                if match:
                    amount_raw = match.group("amount")
                    currency_raw = match.group("currency")
                    amount = _to_decimal(amount_raw)
                    currency = _normalize_currency(currency_raw)
                    raw_signals["amount_line"] = line
                    break

        if order_id is None:
            for pattern in _ORDER_ID_PATTERNS:
                match = pattern.search(line)
                if match:
                    order_id = match.group(2)
                    raw_signals["order_id_line"] = line
                    break

        if original_order_id is None:
            for pattern in _ORIGINAL_ORDER_ID_PATTERNS:
                match = pattern.search(line)
                if match:
                    original_order_id = match.group(1)
                    raw_signals["original_order_id_line"] = line
                    break

        if purchase_date is None:
            for pattern in _DATE_PATTERNS:
                match = pattern.search(line)
                if match:
                    raw_signals["purchase_date_line"] = line
                    purchase_date = _parse_date(match.group(2))
                    break

        if app_name is None:
            for pattern in _APP_NAME_PATTERNS:
                match = pattern.search(line)
                if match:
                    value = match.group(2).strip()
                    app_name = _clean_value(value)
                    raw_signals["app_name_line"] = line
                    break

        if subscription_display is None:
            for pattern in _SUBSCRIPTION_NAME_PATTERNS:
                match = pattern.search(line)
                if match:
                    value = match.group(2).strip()
                    subscription_display = _clean_value(value)
                    raw_signals["subscription_line"] = line
                    break

        if developer_or_seller is None:
            for pattern in _SELLER_PATTERNS:
                match = pattern.search(line)
                if match:
                    developer_or_seller = _clean_value(match.group(2).strip())
                    raw_signals["seller_line"] = line
                    break

        if country is None:
            for pattern in _COUNTRY_PATTERNS:
                match = pattern.search(line)
                if match:
                    country = _clean_value(match.group(2).strip())
                    raw_signals["country_line"] = line
                    break

        if family_sharing is None and "family sharing" in line.lower():
            family_sharing = True
            raw_signals["family_sharing_line"] = line

    if family_sharing is None and "family sharing" in normalized.lower():
        family_sharing = False

    if not any([amount, order_id, app_name, subscription_display, purchase_date]):
        return None

    if _SUBSCRIPTION_TERMS.search(normalized):
        raw_signals["subscription_terms"] = True

    return ParsedAppleReceipt(
        app_name=app_name,
        developer_or_seller=developer_or_seller,
        subscription_display_name=subscription_display,
        amount=amount,
        currency=currency,
        purchase_date_utc=purchase_date,
        order_id=order_id,
        original_order_id=original_order_id,
        country=country,
        family_sharing=family_sharing,
        raw_signals=raw_signals,
    )


def estimate_confidence(parsed: ParsedAppleReceipt | None) -> float:
    if not parsed:
        return 0.0
    score = 0.0
    if parsed.amount is not None:
        score += 0.2
    if parsed.currency:
        score += 0.1
    if parsed.order_id:
        score += 0.2
    if parsed.app_name or parsed.subscription_display_name:
        score += 0.25
    if parsed.purchase_date_utc:
        score += 0.15
    if parsed.raw_signals.get("subscription_terms"):
        score += 0.1
    return min(1.0, score)


def extract_with_llm(body_text: str, html_text: str | None) -> ParsedAppleReceipt | None:
    if not settings.OPENAI_API_KEY:
        return None

    prompt = (
        "Extract Apple receipt details from the email text and return STRICT JSON with keys: "
        "app_name, developer_or_seller, subscription_display_name, amount, currency, "
        "purchase_date_utc, order_id, original_order_id, country, family_sharing, raw_signals, confidence. "
        "Use null for unknown fields. Preserve exact app/subscription names from the text. "
        "Return confidence from 0 to 1. Do not invent data."
    )
    email_text = _normalize_text(body_text, html_text)
    payload = {
        "model": settings.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": email_text[:6000]},
        ],
        "temperature": 0,
    }

    url = settings.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception:
        return None

    confidence = parsed.get("confidence", 0)
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0
    if confidence < 0.75:
        return None

    amount = _to_decimal(parsed.get("amount"))
    purchase_date = _parse_date(parsed.get("purchase_date_utc"))

    return ParsedAppleReceipt(
        app_name=_clean_value(parsed.get("app_name")),
        developer_or_seller=_clean_value(parsed.get("developer_or_seller")),
        subscription_display_name=_clean_value(parsed.get("subscription_display_name")),
        amount=amount,
        currency=_normalize_currency(parsed.get("currency")),
        purchase_date_utc=purchase_date,
        order_id=_clean_value(parsed.get("order_id")),
        original_order_id=_clean_value(parsed.get("original_order_id")),
        country=_clean_value(parsed.get("country")),
        family_sharing=parsed.get("family_sharing"),
        raw_signals=parsed.get("raw_signals") or {},
    )


def build_subscription_key(parsed: ParsedAppleReceipt) -> str | None:
    app = _normalize_key_part(parsed.app_name)
    sub = _normalize_key_part(parsed.subscription_display_name)
    if not app:
        return None
    if sub:
        return f"apple:{app}:{sub}"
    return f"apple:{app}"


def _normalize_key_part(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", value).strip().lower()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or None


def _normalize_currency(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    if value in {"$", "USD"}:
        return "USD"
    if value in {"€", "EUR"}:
        return "EUR"
    if value in {"£", "GBP"}:
        return "GBP"
    if value in {"A$", "AUD"}:
        return "AUD"
    if value in {"C$", "CAD"}:
        return "CAD"
    return value if len(value) <= 3 else None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        cleaned = str(value).strip().replace(",", "")
        return Decimal(cleaned)
    except Exception:
        return None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = date_parser.parse(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_value(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text or None
