from typing import Generator, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.services.auth_service import AuthService
from app.schemas.user import TokenData, UserResponse
from app.db.models.user import UserRole

security = HTTPBearer()


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


async def get_current_superuser(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Verify current user is a superuser."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Superuser access required.",
        )
    return current_user


async def get_current_tenant_admin(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Verify current user is a tenant admin or superuser."""
    if current_user.role not in [UserRole.TENANT_ADMIN, UserRole.SUPER_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Admin access required.",
        )
    return current_user


def verify_tenant_access(tenant_id: UUID, current_user: UserResponse) -> bool:
    """
    Verify user has access to specified tenant.

    Args:
        tenant_id: Tenant UUID to check
        current_user: Current authenticated user

    Returns:
        True if user has access

    Raises:
        HTTPException: If user doesn't have access
    """
    # Super admins have access to all tenants
    if current_user.is_superuser:
        return True

    # Users must belong to the tenant they're accessing
    if current_user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. You don't have permission to access this tenant.",
        )

    return True
