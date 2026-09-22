"""Authentication endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.security import set_auth_cookies, clear_auth_cookies
from app.schemas.auth import (
    PasswordResetRequest,
    PasswordResetConfirm,
    ForcePasswordChangeRequest,
)
from app.schemas.user import (
    CurrentUserResponse,
    LoginRequest,
    UserPasswordUpdate,
    UserResponse,
)
from app.services.auth_service import AccountPendingDeletionError, AuthService
from app.utils.http_rate_limiter import auth_rate_limit

router = APIRouter()


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def login(login_data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Login endpoint — sets HttpOnly auth cookies and returns token in body."""
    auth_service = AuthService(db)

    try:
        result = auth_service.login(login_data)
        set_auth_cookies(response, result["access_token"], result["refresh_token"])
        return {
            "access_token": result["access_token"],
            "token_type": result["token_type"],
            "expires_in": result["expires_in"],
            "must_change_password": result["must_change_password"],
        }
    except AccountPendingDeletionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_pending_deletion", "message": str(e)},
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


@router.get("/me", response_model=CurrentUserResponse, status_code=status.HTTP_200_OK)
def get_current_user_info(current_user: UserResponse = Depends(get_current_user)):
    """
    Get current authenticated user information.

    Args:
        current_user: Current authenticated user from JWT

    Returns:
        Current user details
    """
    return current_user


@router.put("/me/password", status_code=status.HTTP_200_OK)
def update_password(
    password_data: UserPasswordUpdate,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update current user's password.

    Args:
        password_data: Current and new password
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 400 if current password is incorrect
    """
    auth_service = AuthService(db)

    try:
        auth_service.update_user_password(
            current_user.id, password_data.current_password, password_data.new_password
        )
        return {"message": "Password updated successfully"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.post(
    "/forgot-password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def forgot_password(
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
):
    """Request a password reset link."""
    auth_service = AuthService(db)
    return auth_service.request_password_reset(payload.email)


@router.post(
    "/reset-password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def reset_password(
    payload: PasswordResetConfirm,
    db: Session = Depends(get_db),
):
    """Reset password using a reset token."""
    auth_service = AuthService(db)
    try:
        return auth_service.reset_password(payload.token, payload.new_password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post(
    "/force-change-password",
    status_code=status.HTTP_200_OK,
)
def force_change_password(
    payload: ForcePasswordChangeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Change password for users with must_change_password flag."""
    auth_service = AuthService(db)
    try:
        return auth_service.force_change_password(current_user.id, payload.new_password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh_token(request: Request, response: Response, db: Session = Depends(get_db)):
    """Refresh access token using refresh token cookie."""
    raw_refresh = request.cookies.get("refresh_token")
    if not raw_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    auth_service = AuthService(db)
    try:
        result = auth_service.refresh(raw_refresh)
        set_auth_cookies(response, result["access_token"], result["refresh_token"])
        return {
            "access_token": result["access_token"],
            "token_type": "bearer",
            "expires_in": result["expires_in"],
        }
    except ValueError as e:
        clear_auth_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Logout — revoke tokens and clear cookies."""
    import hashlib

    from app.api.deps import extract_token, revoke_token
    from app.db.models.refresh_token import RefreshToken
    from jose import jwt as jose_jwt

    # Revoke current access token's jti
    token = extract_token(request)
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
    raw_refresh = request.cookies.get("refresh_token")
    if raw_refresh:
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        record = db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if record:
            record.revoked_at = datetime.now(UTC)
            db.commit()

    clear_auth_cookies(response)
    return {"message": "Logged out"}
