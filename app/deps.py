from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.security import decode_token

security = HTTPBearer(auto_error=False)

def get_current_user_id(creds: HTTPAuthorizationCredentials | None = Depends(security)) -> int:
    if creds is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    try:
        payload = decode_token(creds.credentials)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    return int(payload["uid"])
