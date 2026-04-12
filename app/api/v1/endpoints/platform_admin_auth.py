"""Platform admin authentication endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_current_super_admin, get_db
from app.core.config import settings
from app.core.security import set_auth_cookies, clear_auth_cookies
from app.schemas.platform_admin import (
    CurrentPlatformAdminResponse,
    PlatformAdminCreate,
    PlatformAdminListResponse,
    PlatformAdminLoginRequest,
    PlatformAdminPasswordUpdate,
    PlatformAdminResponse,
    PlatformAdminToken,
)
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.utils.http_rate_limiter import platform_admin_auth_rate_limit, platform_admin_rate_limit

router = APIRouter()

PLATFORM_REFRESH_PATH = "/api/v1/platform/auth/refresh"


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_auth_rate_limit)],
)
def login(login_data: PlatformAdminLoginRequest, response: Response, db: Session = Depends(get_db)):
    """Platform admin login — sets HttpOnly auth cookies and returns token in body."""
    admin_service = PlatformAdminAuthService(db)

    try:
        result = admin_service.login(login_data)
        set_auth_cookies(
            response,
            result["access_token"],
            result["refresh_token"],
            refresh_path=PLATFORM_REFRESH_PATH,
        )
        return {
            "access_token": result["access_token"],
            "token_type": result["token_type"],
            "expires_in": result["expires_in"],
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


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


@router.put(
    "/me/password",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
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
        ) from e


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh_admin_token(request: Request, response: Response, db: Session = Depends(get_db)):
    """Refresh platform admin access token using refresh token cookie."""
    raw_refresh = request.cookies.get("refresh_token")
    if not raw_refresh:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    admin_service = PlatformAdminAuthService(db)
    try:
        result = admin_service.refresh(raw_refresh)
        set_auth_cookies(
            response,
            result["access_token"],
            result["refresh_token"],
            refresh_path=PLATFORM_REFRESH_PATH,
        )
        return {
            "access_token": result["access_token"],
            "token_type": "bearer",
            "expires_in": result["expires_in"],
        }
    except ValueError as e:
        clear_auth_cookies(response, refresh_path=PLATFORM_REFRESH_PATH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout_admin(request: Request, response: Response, db: Session = Depends(get_db)):
    """Logout platform admin — revoke tokens and clear cookies."""
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

    clear_auth_cookies(response, refresh_path=PLATFORM_REFRESH_PATH)
    return {"message": "Logged out"}


# Admin management endpoints (SUPER_ADMIN only)
@router.post(
    "/admins",
    response_model=PlatformAdminResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_super_admin), Depends(platform_admin_rate_limit)],
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
        ) from e


@router.get(
    "/admins",
    response_model=list[PlatformAdminListResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_current_super_admin), Depends(platform_admin_rate_limit)],
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
