from datetime import datetime, timedelta, timezone
from jose import jwt
from cryptography.fernet import Fernet
from app.config import settings

def create_access_token(*, subject: str, user_id: int) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.JWT_EXPIRES_MINUTES)
    payload = {
        "sub": subject,
        "uid": user_id,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")

def decode_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=["HS256"],
        audience=settings.JWT_AUDIENCE,
        issuer=settings.JWT_ISSUER,
    )

class TokenCipher:
    def __init__(self):
        self.fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode("utf-8"))
    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")

token_cipher = TokenCipher()
