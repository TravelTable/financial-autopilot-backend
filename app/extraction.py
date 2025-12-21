from __future__ import annotations
from datetime import datetime, timezone
import re
from typing import Any

AMOUNT_RE = re.compile(r'(?P<currency>\$|USD|AUD|EUR|GBP)\s?(?P<amount>\d{1,6}(?:[\.,]\d{2})?)', re.I)

CURRENCY_MAP = {"$": "USD", "USD": "USD", "AUD": "AUD", "EUR": "EUR", "GBP": "GBP"}

def _safe_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None

def extract_headers(message: dict) -> dict[str, str]:
    headers = {}
    payload = message.get("payload", {}) or {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name:
            headers[name] = h.get("value") or ""
    return headers

def get_plain_text_parts(payload: dict) -> str:
    import base64
    texts: list[str] = []
    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            try:
                txt = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
                texts.append(txt)
            except Exception:
                pass
        for p in part.get("parts", []) or []:
            walk(p)
    walk(payload or {})
    return "\n".join(texts)

def rules_extract(message: dict) -> dict[str, Any]:
    headers = extract_headers(message)
    subject = headers.get("subject", "")
    from_h = headers.get("from", "")
    snippet = message.get("snippet", "") or ""

    vendor = None
    if from_h:
        vendor = from_h.split("<")[0].strip().strip('"')[:256] or None

    currency = None
    amount = None
    m = AMOUNT_RE.search(subject + " " + snippet)
    if m:
        currency = CURRENCY_MAP.get(m.group("currency").upper(), CURRENCY_MAP.get(m.group("currency"), None))
        amount = _safe_float(m.group("amount"))

    internal_date_ms = int(message.get("internalDate", "0"))
    tx_date = None
    if internal_date_ms:
        tx_date = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc).date()

    blob = (subject + " " + snippet).lower()
    is_sub = any(
        k in blob
        for k in [
            "subscription",
            "renewal",
            "trial",
            "free trial",
            "recurring",
            "membership",
            "subscribe",
            "plan",
            "auto-renew",
            "active subscription",
            "subscribed",
        ]
    )

    cat = None
    if any(k in blob for k in ["uber", "lyft", "taxi"]):
        cat = "Transport"
    elif any(k in blob for k in ["netflix", "spotify", "hulu", "prime video"]):
        cat = "Entertainment"

    return {
        "vendor": vendor,
        "amount": amount,
        "currency": currency,
        "transaction_date": tx_date,
        "category": cat,
        "is_subscription": bool(is_sub),
        "trial_end_date": None,
        "renewal_date": None,
        "confidence": {"vendor": 0.4 if vendor else 0.0, "amount": 0.5 if amount else 0.0, "date": 0.6 if tx_date else 0.0},
    }
