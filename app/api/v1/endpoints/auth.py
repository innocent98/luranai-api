"""Authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.schemas.auth import (
    PasswordResetRequest,
    PasswordResetConfirm,
    LoginResponse,
    ForcePasswordChangeRequest,
)
from app.schemas.user import (
    CurrentUserResponse,
    LoginRequest,
    Token,
    UserPasswordUpdate,
    UserResponse,
)
from app.services.auth_service import AuthService
from app.utils.http_rate_limiter import auth_rate_limit

router = APIRouter()


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)],
)
def login(login_data: LoginRequest, db: Session = Depends(get_db)):
    """
    Login endpoint to authenticate users and generate JWT token.

    Args:
        login_data: Email and password
        db: Database session

    Returns:
        JWT token with expiration

    Raises:
        HTTPException: 401 if authentication fails
    """
    auth_service = AuthService(db)

    try:
        token = auth_service.login(login_data)
        return token
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
