from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import get_settings


password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hash.hash("aegis-nonexistent-account-timing-sentinel")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def create_access_token(subject: str, role: str) -> tuple[str, int]:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": subject,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": str(uuid.uuid4()),
        "iat": datetime.now(timezone.utc),
        "exp": expires,
    }
    token = jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm="HS256")
    return token, settings.access_token_minutes * 60


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "iss", "aud", "iat", "exp", "jti"]},
        )
    except InvalidTokenError as exc:
        raise ValueError("invalid or expired token") from exc
    if not payload.get("sub"):
        raise ValueError("token subject is missing")
    return payload
