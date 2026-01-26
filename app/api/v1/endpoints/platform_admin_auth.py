"""Platform admin authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_platform_admin,
    get_current_super_admin,
    get_db,
)
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.schemas.platform_admin import (
    PlatformAdminLoginRequest,
    PlatformAdminToken,
    PlatformAdminCreate,
    PlatformAdminResponse,
    PlatformAdminPasswordUpdate,
    CurrentPlatformAdminResponse,
    PlatformAdminListResponse,
)
from app.utils.http_rate_limiter import auth_rate_limit

router = APIRouter()


@router.post(
    "/login",
    response_model=PlatformAdminToken,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(auth_rate_limit)]
)
def login(login_data: PlatformAdminLoginRequest, db: Session = Depends(get_db)):
    """
    Platform admin login endpoint.

    Args:
        login_data: Email and password
        db: Database session

    Returns:
        JWT token with expiration

    Raises:
        HTTPException: 401 if authentication fails
    """
    admin_service = PlatformAdminAuthService(db)

    try:
        token = admin_service.login(login_data)
        return token
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/me", response_model=CurrentPlatformAdminResponse, status_code=status.HTTP_200_OK)
def get_current_admin_info(
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get current authenticated platform admin information.

    Args:
        current_admin: Current authenticated admin from JWT

    Returns:
        Current admin details
    """
    return current_admin


@router.put("/me/password", status_code=status.HTTP_200_OK)
def update_password(
    password_data: PlatformAdminPasswordUpdate,
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Update current platform admin's password.

    Args:
        password_data: Current and new password
        current_admin: Current authenticated admin
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 400 if current password is incorrect
    """
    admin_service = PlatformAdminAuthService(db)

    try:
        admin_service.update_admin_password(
            current_admin.id,
            password_data.current_password,
            password_data.new_password,
        )
        return {"message": "Password updated successfully"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# Admin management endpoints (SUPER_ADMIN only)
@router.post(
    "/admins",
    response_model=PlatformAdminResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_super_admin)],
)
def create_platform_admin(
    admin_data: PlatformAdminCreate,
    db: Session = Depends(get_db),
):
    """
    Create a new platform admin (SUPER_ADMIN only).

    Args:
        admin_data: Admin creation data
        db: Database session

    Returns:
        Created admin response

    Raises:
        HTTPException: 400 if admin already exists
        HTTPException: 403 if not super admin
    """
    admin_service = PlatformAdminAuthService(db)

    try:
        admin = admin_service.create_admin(admin_data)
        return admin
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/admins",
    response_model=list[PlatformAdminListResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_current_super_admin)],
)
def list_platform_admins(db: Session = Depends(get_db)):
    """
    List all platform admins (SUPER_ADMIN only).

    Args:
        db: Database session

    Returns:
        List of platform admins

    Raises:
        HTTPException: 403 if not super admin
    """
    admin_service = PlatformAdminAuthService(db)
    admins = admin_service.get_all_admins()
    return admins
