"""Customer-facing authentication endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jose import JWTError
from jose import jwt as jose_jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_db
from app.core.config import settings
from app.core.security import (
    CUSTOMER_ACCESS_COOKIE,
    CUSTOMER_REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
)
from app.schemas.customer_auth import (
    CustomerActionResponse,
    CustomerEmailVerificationRequest,
    CustomerForgotPasswordRequest,
    CustomerLoginRequest,
    CustomerPasswordUpdateRequest,
    CustomerProfileUpdateRequest,
    CustomerRegisterRequest,
    CustomerResendVerificationRequest,
    CustomerResetPasswordRequest,
    CustomerUserResponse,
)
from app.services.customer_auth_service import CustomerAuthService
from app.utils.http_rate_limiter import auth_rate_limit

# The refresh cookie must reach BOTH /refresh and /logout (logout revokes the refresh token
# server-side), so it is scoped to the customer auth prefix rather than /refresh alone.
CUSTOMER_REFRESH_PATH = "/api/v1/customer/auth"
# Pre-migration refresh cookie path (shared `refresh_token` name), expired on sight.
LEGACY_CUSTOMER_REFRESH_PATH = "/api/v1/customer/auth/refresh"


def _set_customer_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    set_auth_cookies(
        response,
        access_token,
        refresh_token,
        refresh_path=CUSTOMER_REFRESH_PATH,
        access_cookie=CUSTOMER_ACCESS_COOKIE,
        refresh_cookie=CUSTOMER_REFRESH_COOKIE,
    )


def _clear_customer_cookies(response: Response, request: Request) -> None:
    """Expire the customer cookies, plus the pre-migration shared-name ones."""
    clear_auth_cookies(
        response,
        refresh_path=CUSTOMER_REFRESH_PATH,
        access_cookie=CUSTOMER_ACCESS_COOKIE,
        refresh_cookie=CUSTOMER_REFRESH_COOKIE,
    )
    _expire_legacy_customer_cookies(response, request)


def _expire_legacy_customer_cookies(response: Response, request: Request) -> None:
    """Expire only the pre-migration shared-name customer cookies.

    The legacy refresh cookie is scoped to the customer-only refresh path, so it is always
    safe to expire. The legacy ``access_token`` cookie lives at ``/`` and is shared with the
    tenant dashboard, so it is only expired when it actually holds a customer JWT.
    """
    response.delete_cookie(key="refresh_token", path=LEGACY_CUSTOMER_REFRESH_PATH)
    legacy = request.cookies.get("access_token")
    if legacy:
        try:
            claims = jose_jwt.decode(legacy, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        except JWTError:
            return
        if claims.get("auth_type") == "customer":
            response.delete_cookie(key="access_token", path="/")


router = APIRouter()


@router.post(
    "/register",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(auth_rate_limit)],
)
def register_customer(payload: CustomerRegisterRequest, db: Session = Depends(get_db)):
    service = CustomerAuthService(db)
    try:
        return service.register_customer(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/verify-email",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def verify_customer_email(
    payload: CustomerEmailVerificationRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.verify_email(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/resend-verification",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def resend_customer_verification(
    payload: CustomerResendVerificationRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.resend_verification(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def login_customer(
    payload: CustomerLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Login customer — sets HttpOnly auth cookies and returns the customer (no token in body)."""
    service = CustomerAuthService(db)
    try:
        result = service.login(payload)
        _expire_legacy_customer_cookies(response, request)
        _set_customer_cookies(response, result["access_token"], result["refresh_token"])
        return {
            "token_type": result["token_type"],
            "expires_in": result["expires_in"],
            "customer": result["customer"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post(
    "/forgot-password",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def forgot_customer_password(
    payload: CustomerForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    return service.request_password_reset(payload)


@router.post(
    "/reset-password",
    response_model=CustomerActionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def reset_customer_password(
    payload: CustomerResetPasswordRequest,
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.reset_password(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/me", response_model=CustomerUserResponse, status_code=status.HTTP_200_OK)
def get_me(current_customer: CustomerUserResponse = Depends(get_current_customer)):
    return current_customer


@router.put("/me", response_model=CustomerUserResponse, status_code=status.HTTP_200_OK)
def update_profile(
    payload: CustomerProfileUpdateRequest,
    current_customer: CustomerUserResponse = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.update_profile(current_customer.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/me/password", response_model=CustomerActionResponse, status_code=status.HTTP_200_OK)
def update_customer_password(
    payload: CustomerPasswordUpdateRequest,
    current_customer: CustomerUserResponse = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    service = CustomerAuthService(db)
    try:
        return service.update_password(current_customer.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh_customer_token(request: Request, response: Response, db: Session = Depends(get_db)):
    """Refresh customer access token using refresh token cookie."""
    raw_refresh = request.cookies.get(CUSTOMER_REFRESH_COOKIE) or request.cookies.get(
        "refresh_token"
    )
    if not raw_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    service = CustomerAuthService(db)
    try:
        result = service.refresh(raw_refresh)
        _expire_legacy_customer_cookies(response, request)
        _set_customer_cookies(response, result["access_token"], result["refresh_token"])
        return {
            "token_type": "bearer",
            "expires_in": result["expires_in"],
        }
    except ValueError as exc:
        _clear_customer_cookies(response, request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout_customer(request: Request, response: Response, db: Session = Depends(get_db)):
    """Logout customer — revoke tokens and clear cookies."""
    import hashlib

    from app.api.deps import extract_customer_token, revoke_token
    from app.db.models.refresh_token import RefreshToken

    # Revoke current access token's jti
    token = extract_customer_token(request)
    if token:
        try:
            payload = jose_jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            jti = payload.get("jti")
            exp = payload.get("exp", 0)
            if jti:
                remaining = max(int(exp - datetime.now(UTC).timestamp()), 0)
                revoke_token(jti, remaining)
        except Exception:
            pass

    # Revoke refresh token
    raw_refresh = request.cookies.get(CUSTOMER_REFRESH_COOKIE) or request.cookies.get(
        "refresh_token"
    )
    if raw_refresh:
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        record = db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if record:
            record.revoked_at = datetime.now(UTC)
            db.commit()

    _clear_customer_cookies(response, request)
    return {"message": "Logged out"}
