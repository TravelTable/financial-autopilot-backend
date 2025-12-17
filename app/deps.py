# app/deps.py
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.security import decode_token

security = HTTPBearer(auto_error=False)

def get_current_user_id(creds: HTTPAuthorizationCredentials | None = Depends(security)) -> int:
    if creds is None:
        # log missing header for debugging
        print("⚠️ Missing Authorization header")
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = creds.credentials
    # show first 10 and last 6 chars so you can match logs without exposing the full token
    masked = f"{token[:10]}…{token[-6:]}" if len(token) > 16 else "<redacted>"
    print(f"🔐 Authorization token: {masked}")
    try:
        payload = decode_token(token)
    except Exception as e:
        print(f"🚫 Invalid token: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")
    return int(payload["uid"])
