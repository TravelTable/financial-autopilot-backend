from datetime import datetime, timedelta, timezone
from typing import Any
import httpx
from app.config import settings

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthError(RuntimeError):
    pass


async def exchange_server_auth_code(server_auth_code: str) -> dict[str, Any]:
    """
    Exchanges a Google serverAuthCode for access/refresh tokens.
    Logs full Google error responses to stdout so Railway captures them.
    """

    # ---- ENV VAR SANITY CHECK ----
    if (
        not settings.GOOGLE_CLIENT_ID
        or not settings.GOOGLE_CLIENT_SECRET
        or not settings.GOOGLE_REDIRECT_URI
    ):
        print("❌ Missing Google OAuth env vars")
        print("GOOGLE_CLIENT_ID:", bool(settings.GOOGLE_CLIENT_ID))
        print("GOOGLE_CLIENT_SECRET:", bool(settings.GOOGLE_CLIENT_SECRET))
        print("GOOGLE_REDIRECT_URI:", settings.GOOGLE_REDIRECT_URI)
        raise GoogleOAuthError(
            "Missing Google OAuth env vars (client id/secret/redirect uri)."
        )

    print("▶️ Starting Google token exchange")
    print("Redirect URI:", settings.GOOGLE_REDIRECT_URI)
    print(
        "Client ID prefix:",
        settings.GOOGLE_CLIENT_ID[:12] + "..."
        if settings.GOOGLE_CLIENT_ID
        else "missing",
    )

    data = {
        "code": server_auth_code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data=data)

        # ---- CRITICAL LOGGING (Railway WILL SHOW THIS) ----
        print("⬅️ Google token response status:", resp.status_code)
        print("⬅️ Google token response body:", resp.text)

        if resp.status_code != 200:
            raise GoogleOAuthError(
                f"Token exchange failed: {resp.status_code} {resp.text}"
            )

        token_json = resp.json()

    expiry = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_json.get("expires_in", 3600))
    )
    token_json["_expiry_utc"] = expiry

    print("✅ Google token exchange succeeded")
    return token_json


async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """
    Fetches Google user profile info using access token.
    """

    print("▶️ Fetching Google userinfo")

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        print("⬅️ Google userinfo status:", resp.status_code)
        print("⬅️ Google userinfo body:", resp.text)

        if resp.status_code != 200:
            raise GoogleOAuthError(
                f"Userinfo failed: {resp.status_code} {resp.text}"
            )

        return resp.json()
