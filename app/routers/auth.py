from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.schemas import AuthGoogleIn, AuthOut
from app.db import get_db
from app.models import User, GoogleAccount, AuditLog
from app.security import create_access_token, token_cipher
from app.google_oauth import exchange_server_auth_code, fetch_userinfo, GoogleOAuthError

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/google", response_model=AuthOut)
async def auth_google(payload: AuthGoogleIn, db: Session = Depends(get_db)):
    if not payload.server_auth_code and not payload.access_token:
        raise HTTPException(status_code=400, detail="Provide server_auth_code (recommended) or access_token (debug only)")

    try:
        if payload.server_auth_code:
            token_json = await exchange_server_auth_code(payload.server_auth_code)
            access_token = token_json.get("access_token")
            refresh_token = token_json.get("refresh_token")
            scope = token_json.get("scope", "")
            expiry = token_json.get("_expiry_utc")
            if not access_token:
                raise HTTPException(status_code=400, detail="No access_token returned by Google")
            info = await fetch_userinfo(access_token)
        else:
            access_token = payload.access_token
            refresh_token = None
            scope = ""
            expiry = None
            info = await fetch_userinfo(access_token)

        email = info.get("email")
        google_user_id = info.get("sub") or info.get("id")
        if not email or not google_user_id:
            raise HTTPException(status_code=400, detail="Google userinfo missing email/sub")

        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(email=email)
            db.add(user)
            db.commit()
            db.refresh(user)

        acct = db.query(GoogleAccount).filter(GoogleAccount.user_id == user.id, GoogleAccount.google_user_id == google_user_id).first()
        if not acct:
            if not refresh_token:
                raise HTTPException(status_code=400, detail="Missing refresh_token. Ensure consent + offline access in the client.")
            acct = GoogleAccount(
                user_id=user.id,
                google_user_id=google_user_id,
                email=email,
                access_token=access_token,
                refresh_token_enc=token_cipher.encrypt(refresh_token),
                scope=scope,
                token_expiry_utc=expiry,
            )
            db.add(acct)
        else:
            acct.access_token = access_token
            if refresh_token:
                acct.refresh_token_enc = token_cipher.encrypt(refresh_token)
            acct.scope = scope or acct.scope
            acct.token_expiry_utc = expiry or acct.token_expiry_utc

        db.add(AuditLog(user_id=user.id, action="google_auth", meta={"email": email}))
        db.commit()

        app_token = create_access_token(subject=email, user_id=user.id)
        return AuthOut(access_token=app_token, user_email=email)

    except GoogleOAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
