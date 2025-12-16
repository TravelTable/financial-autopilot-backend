from datetime import datetime, timedelta, timezone
from typing import Any
import logging
import httpx
from app.config import settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

logger = logging.getLogger(__name__)

class GoogleOAuthError(RuntimeError):
    pass

async def exchange_server_auth_code(server_auth_code: str) -> dict[str, Any]:
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET or not settings.GOOGLE_REDIRECT_URI:
        raise GoogleOAuthError("Missing Google OAuth env vars (client id/secret/redirect uri).")

    data = {
        "code": server_auth_code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data=data)

        if resp.status_code != 200:
            # Railway will capture this log output.
            # Avoid printing the auth code or secret. This is safe to log:
            logger.error(
                "Google token exchange failed: status=%s body=%s redirect_uri=%s client_id_prefix=%s",
                resp.status_code,
                resp.text,
                settings.GOOGLE_REDIRECT_URI,
                (settings.GOOGLE_CLIENT_ID[:12] + "...") if settings.GOOGLE_CLIENT_ID else "missing",
            )
            raise GoogleOAuthError(f"Token exchange failed: {resp.status_code} {resp.text}")

        token_json = resp.json()

    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(token_json.get("expires_in", 3600)))
    token_json["_expiry_utc"] = expiry
    return token_json

async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code != 200:
            logger.error("Google userinfo failed: status=%s body=%s", resp.status_code, resp.text)
            raise GoogleOAuthError(f"Userinfo failed: {resp.status_code} {resp.text}")
        return resp.json()
