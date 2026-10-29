"""Referral-program marketer authentication endpoints (``/api/v1/auth/marketer/*``).

Marketers sign in through the shared tenant login page, which falls back to ``POST /login`` here.
Sessions live in their own cookies (``marketer_access_token`` / ``marketer_refresh_token``) and no
token is ever returned in a response body.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.api.deps import get_db, revoke_token
from app.core.config import settings
from app.core.security import (
    MARKETER_ACCESS_COOKIE,
    MARKETER_REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)
from app.schemas.marketer_auth import (
    MarketerForgotPasswordRequest,
    MarketerLoginRequest,
    MarketerSetPasswordRequest,
)
from app.services.referral.marketer_auth_service import (
    AccountSuspendedError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
    MarketerAuthService,
    marketer_id_from_claims,
    send_password_reset,
)
from app.utils.http_rate_limiter import marketer_auth_rate_limit

# The refresh cookie must reach BOTH /refresh and /logout (logout revokes the refresh token
# server-side), so it is scoped to the marketer auth prefix rather than /refresh alone.
MARKETER_REFRESH_PATH = "/api/v1/auth/marketer"

FORGOT_PASSWORD_MESSAGE = "If an active account exists for that email, a reset link has been sent."

router = APIRouter()


def _set_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    set_auth_cookies(
        response,
        access_token,
        refresh_token,
        refresh_path=MARKETER_REFRESH_PATH,
        access_cookie=MARKETER_ACCESS_COOKIE,
        refresh_cookie=MARKETER_REFRESH_COOKIE,
    )


def _clear_cookies(response: Response) -> None:
    clear_auth_cookies(
        response,
        refresh_path=MARKETER_REFRESH_PATH,
        access_cookie=MARKETER_ACCESS_COOKIE,
        refresh_cookie=MARKETER_REFRESH_COOKIE,
    )


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(marketer_auth_rate_limit)],
)
def login(payload: MarketerLoginRequest, response: Response, db: Session = Depends(get_db)):
    """Marketer login: sets the marketer cookies; the body carries no token."""
    try:
        result = MarketerAuthService(db).login(payload.email, payload.password)
    except InvalidCredentialsError as exc:
        # Identical for unknown email, no password yet and wrong password.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "Incorrect email or password"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AccountSuspendedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": exc.code,
                "message": "This account has been suspended. Contact Luran support.",
            },
        ) from exc
    _set_cookies(response, result["access_token"], result["refresh_token"])
    return {"role": "marketer", "redirect": "/marketer"}


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    """Rotate the refresh token and issue a new access token (cookies only)."""
    raw_refresh = request.cookies.get(MARKETER_REFRESH_COOKIE)
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "No refresh token"},
        )
    try:
        result = MarketerAuthService(db).refresh(raw_refresh)
    except InvalidRefreshTokenError as exc:
        # No Set-Cookie on failure: a tab that lost a refresh race must not wipe the winner's pair.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "Invalid or expired refresh token"},
        ) from exc
    _set_cookies(response, result["access_token"], result["refresh_token"])
    return {"token_type": "bearer", "expires_in": result["expires_in"]}


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Revoke the session server-side and clear the marketer cookies. Always 200."""
    access = request.cookies.get(MARKETER_ACCESS_COOKIE)
    if access:
        try:
            claims = jwt.decode(access, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            jti = claims.get("jti")
            # Only ever blacklist a marketer token's jti.
            if jti and marketer_id_from_claims(claims) is not None:
                remaining = max(int(claims.get("exp", 0) - datetime.now(UTC).timestamp()), 0)
                revoke_token(jti, remaining)
        except JWTError:
            pass

    raw_refresh = request.cookies.get(MARKETER_REFRESH_COOKIE)
    if raw_refresh:
        MarketerAuthService(db).revoke_refresh_token(raw_refresh)

    _clear_cookies(response)
    return {"message": "Logged out"}


@router.post(
    "/set-password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(marketer_auth_rate_limit)],
)
def set_password(payload: MarketerSetPasswordRequest, db: Session = Depends(get_db)):
    """Redeem an invite or reset token (one-time) to set the password. Does not sign in."""
    try:
        MarketerAuthService(db).set_password(payload.token, payload.new_password)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": "This link is invalid or has expired."},
        ) from exc
    return {"message": "Password set. You can now sign in."}


@router.post(
    "/forgot-password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(marketer_auth_rate_limit)],
)
def forgot_password(
    payload: MarketerForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Always the same generic 200. The email (token + SMTP) is sent after the response."""
    target = MarketerAuthService(db).reset_target(payload.email)
    if target is not None:
        background_tasks.add_task(send_password_reset, *target)
    return {"message": FORGOT_PASSWORD_MESSAGE}
