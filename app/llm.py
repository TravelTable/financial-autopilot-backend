from __future__ import annotations
from typing import Any, Protocol
import httpx
from app.config import settings

class LLM(Protocol):
    async def extract_transaction(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> dict[str, Any] | None:
        ...

    async def classify_receipt(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> bool | None:
        ...

class NoopLLM:
    async def extract_transaction(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> dict[str, Any] | None:
        return None

    async def classify_receipt(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> bool | None:
        return True

class OpenAIChatCompletionsLLM:
    async def classify_receipt(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> bool | None:
        if not settings.OPENAI_API_KEY:
            return None

        system = (
            "You are a classifier. Determine whether the email is a receipt or confirmation "
            "for a purchase/subscription the user already has. "
            "Return only 'true' or 'false'. "
            "Promotions, newsletters, social notifications, or trial invitations are false. "
            "If LIST_UNSUBSCRIBE is present, return false unless there is a clear charge with an amount "
            "or an explicit renewal/trial end date."
        )
        user = f"""EMAIL_FROM: {email_from}
EMAIL_SUBJECT: {email_subject}
EMAIL_SNIPPET: {email_snippet}
LIST_UNSUBSCRIBE: {email_list_unsubscribe or ""}
EMAIL_TEXT: {email_text[:4000]}
"""

        payload = {
            "model": settings.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }

        url = settings.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip().lower()
        if content in {"true", "false"}:
            return content == "true"
        return None

    async def extract_transaction(
        self,
        *,
        email_subject: str,
        email_from: str,
        email_snippet: str,
        email_text: str,
        email_list_unsubscribe: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.OPENAI_API_KEY:
            return None

        system = (
            "Extract structured purchase/subscription info from emails. "
            "Look for subscription phrases like 'membership', 'plan', 'auto-renew', "
            "'active subscription', etc. Only set is_subscription when the email "
            "confirms an actual purchase/subscription/trial the user has. Do not "
            "mark marketing offers or solicitations as subscriptions. "
            "Example (promo): 'Try Premium for 30% off' -> is_subscription false. "
            "Example (promo): 'Start your plan today' -> is_subscription false. "
            "Example (receipt): 'Your Pro plan is now active' -> is_subscription true. "
            "If LIST_UNSUBSCRIBE is present, treat the email as promotional unless it clearly "
            "confirms a charge with an amount or explicit renewal/trial date. "
            "Ignore mass promotions or newsletters even if they mention pricing. "
            "If the email is an Apple, iTunes, Google Play, Amazon, PayPal, or Microsoft receipt, "
            "extract the subscription or app/service name as the vendor (e.g. 'Disney+' or "
            "'YouTube Premium') instead of the platform name. "
            "Ignore promotional offers. "
            "When you find one, set is_subscription to true and extract any mentioned "
            "trial_end_date or renewal_date. "
            "Return ONLY JSON with schema: "
            "{vendor, amount, currency, transaction_date (YYYY-MM-DD), category, is_subscription, trial_end_date, renewal_date, confidence:{vendor,amount,date}}"
        )
        user = f"""EMAIL_FROM: {email_from}
EMAIL_SUBJECT: {email_subject}
EMAIL_SNIPPET: {email_snippet}
LIST_UNSUBSCRIBE: {email_list_unsubscribe or ""}
EMAIL_TEXT: {email_text[:6000]}
"""

        payload = {
            "model": settings.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }

        url = settings.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

        import json
        try:
            return json.loads(content)
        except Exception:
            return None

def get_llm() -> LLM:
    if settings.LLM_PROVIDER == "openai_chat_completions":
        return OpenAIChatCompletionsLLM()
    return NoopLLM()
