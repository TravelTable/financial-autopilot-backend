from __future__ import annotations
from typing import Any, Protocol
import httpx
from app.config import settings

class LLM(Protocol):
    async def extract_transaction(self, *, email_subject: str, email_from: str, email_snippet: str, email_text: str) -> dict[str, Any] | None:
        ...

class NoopLLM:
    async def extract_transaction(self, *, email_subject: str, email_from: str, email_snippet: str, email_text: str) -> dict[str, Any] | None:
        return None

class OpenAIChatCompletionsLLM:
    async def extract_transaction(self, *, email_subject: str, email_from: str, email_snippet: str, email_text: str) -> dict[str, Any] | None:
        if not settings.OPENAI_API_KEY:
            return None

        system = (
            "Extract structured purchase/subscription info from emails. "
            "Only mark is_subscription=true when the email confirms a real purchase, "
            "active subscription, renewal, or trial start/end. Do NOT mark it true for "
            "marketing/promotional offers, newsletters, or invitations to subscribe. "
            "Look for subscription phrases like 'membership', 'plan', 'auto-renew', "
            "'active subscription', etc., but require transactional context. "
            "If this is an Apple receipt, prefer the app/service name as vendor (not just 'Apple'). "
            "When you find a subscription, extract any mentioned trial_end_date or renewal_date. "
            "Return ONLY JSON with schema: "
            "{vendor, amount, currency, transaction_date (YYYY-MM-DD), category, is_subscription, trial_end_date, renewal_date, confidence:{vendor,amount,date}}"
        )
        user = f"""EMAIL_FROM: {email_from}
EMAIL_SUBJECT: {email_subject}
EMAIL_SNIPPET: {email_snippet}
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
