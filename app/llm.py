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
            "Look for subscription phrases like 'membership', 'plan', 'auto-renew', "
            "'active subscription', etc. Only set is_subscription when the email "
            "confirms an actual purchase/subscription/trial the user has. Do not "
            "mark marketing offers or solicitations as subscriptions. "
            "Example (promo): 'Try Premium for 30% off' -> is_subscription false. "
            "Example (receipt): 'Your Pro plan is now active' -> is_subscription true. "
            "When you find one, set is_subscription to true and extract any mentioned "
            "trial_end_date or renewal_date. "
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
