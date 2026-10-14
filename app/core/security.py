"""Security utilities: JWT tokens, password hashing, cookie auth, token revocation."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(
    subject: str | Any,
    expires_delta: timedelta | None = None,
    additional_claims: dict[str, Any] | None = None,
) -> str:
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode: dict[str, Any] = {
        "exp": expire,
        "sub": str(subject),
        "jti": str(uuid.uuid4()),
    }

    if additional_claims:
        to_encode.update(additional_claims)

    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return str(encoded_jwt)


def create_refresh_token() -> tuple[str, str]:
    """Generate a refresh token and its SHA-256 hash.

    Returns:
        (raw_token, token_hash) — store token_hash in DB, send raw_token to client.
    """
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


# The authenticated customer (B2C) session uses its own cookie names so it never
# overwrites, or is overwritten by, the tenant dashboard session on the same API host.
CUSTOMER_ACCESS_COOKIE = "customer_access_token"
CUSTOMER_REFRESH_COOKIE = "customer_refresh_token"


def set_auth_cookies(
    response: Any,
    access_token: str,
    refresh_token: str,
    refresh_path: str = "/api/v1/auth/refresh",
    access_cookie: str = "access_token",
    refresh_cookie: str = "refresh_token",
) -> None:
    """Set HttpOnly Secure auth cookies on a FastAPI Response."""
    response.set_cookie(
        key=access_cookie,
        value=access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key=refresh_cookie,
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path=refresh_path,
    )


def clear_auth_cookies(
    response: Any,
    refresh_path: str = "/api/v1/auth/refresh",
    access_cookie: str = "access_token",
    refresh_cookie: str = "refresh_token",
) -> None:
    """Clear auth cookies on a FastAPI Response."""
    response.delete_cookie(key=access_cookie, path="/")
    response.delete_cookie(key=refresh_cookie, path=refresh_path)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bool(pwd_context.verify(plain_password, hashed_password))


def get_password_hash(password: str) -> str:
    return str(pwd_context.hash(password))
