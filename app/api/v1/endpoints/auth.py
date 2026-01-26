"""Authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.services.auth_service import AuthService
from app.schemas.user import (
    LoginRequest,
    Token,
    UserResponse,
    UserPasswordUpdate,
    CurrentUserResponse,
)
from app.utils.http_rate_limiter import auth_rate_limit

router = APIRouter()


@router.post(
    "/login",
    response_model=Token,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)]
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
        )


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
        )
