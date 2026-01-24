from typing import Generator, Optional, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.config import settings
from app.db.session import get_db
from app.services.auth_service import AuthService
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.schemas.user import TokenData, UserResponse
from app.schemas.platform_admin import PlatformAdminResponse
from app.db.models.user import UserRole
from app.db.models.platform_admin import PlatformAdminRole

security = HTTPBearer()


class AuthContext(BaseModel):
    """Context wrapper for dual authentication."""
    auth_type: Literal["tenant_user", "platform_admin"]
    user: Optional[UserResponse] = None
    admin: Optional[PlatformAdminResponse] = None

    @property
    def tenant_id(self) -> Optional[UUID]:
        """Get tenant_id if this is a tenant user."""
        return self.user.tenant_id if self.user else None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> UserResponse:
    """Get current authenticated user from JWT token."""
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        # Verify token data
        auth_service = AuthService(db)
        token_data = auth_service.verify_token_data(payload)

        if not token_data:
            raise credentials_exception

        # Fetch user from database
        user = auth_service.get_user_by_id(token_data.user_id)

        if not user:
            raise credentials_exception

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account is inactive",
            )

        return user

    except JWTError:
        raise credentials_exception


async def get_current_active_user(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Get current active user (alias for clarity)."""
    return current_user


async def get_current_tenant_admin(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Verify current user is a tenant admin."""
    if current_user.role != UserRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Tenant admin access required.",
        )
    return current_user


async def get_current_user_or_platform_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    Accept both tenant user and platform admin authentication.

    Tries to authenticate as tenant user first, then falls back to platform admin.
    Returns AuthContext indicating which auth type succeeded.

    Raises:
        HTTPException 401: If both auth attempts fail
    """
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        # Try tenant user auth first
        try:
            auth_service = AuthService(db)
            token_data = auth_service.verify_token_data(payload)

            if token_data and hasattr(token_data, 'user_id'):
                user = auth_service.get_user_by_id(token_data.user_id)
                if user and user.is_active:
                    return AuthContext(
                        auth_type="tenant_user",
                        user=user,
                        admin=None,
                    )
        except Exception:
            pass  # Try platform admin auth

        # Fall back to platform admin auth
        admin_service = PlatformAdminAuthService(db)
        token_data = admin_service.verify_token_data(payload)

        if token_data and hasattr(token_data, 'admin_id'):
            admin = admin_service.get_admin_by_id(token_data.admin_id)
            if admin and admin.is_active:
                return AuthContext(
                    auth_type="platform_admin",
                    user=None,
                    admin=admin,
                )

        raise credentials_exception

    except JWTError:
        raise credentials_exception


def verify_tenant_access(tenant_id: UUID, current_user: UserResponse) -> bool:
    """
    Verify user has access to specified tenant.

    Tenant users can only access their own tenant's data.
    For cross-tenant access, use platform admin endpoints instead.

    Args:
        tenant_id: Tenant UUID to check
        current_user: Current authenticated user

    Returns:
        True if user has access

    Raises:
        HTTPException: If user doesn't have access
    """
    # Users must belong to the tenant they're accessing
    if current_user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You don't have permission to access this tenant.",
        )

    return True


# Platform admin dependencies
async def get_current_platform_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> PlatformAdminResponse:
    """Get current authenticated platform admin from JWT token."""
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        # Verify token data
        admin_service = PlatformAdminAuthService(db)
        token_data = admin_service.verify_token_data(payload)

        if not token_data:
            raise credentials_exception

        # Fetch admin from database
        admin = admin_service.get_admin_by_id(token_data.admin_id)

        if not admin:
            raise credentials_exception

        if not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Platform admin account is inactive",
            )

        return admin

    except JWTError:
        raise credentials_exception


async def get_current_super_admin(
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
) -> PlatformAdminResponse:
    """Verify current platform admin is a SUPER_ADMIN."""
    if current_admin.role != PlatformAdminRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Super admin access required.",
        )
    return current_admin
