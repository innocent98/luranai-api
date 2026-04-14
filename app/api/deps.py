from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import redis as redis_lib
from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.platform_admin import PlatformAdminRole
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.db.session import get_db
from app.schemas.customer_auth import CustomerUserResponse
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.user import UserResponse
from app.services.auth_service import AuthService
from app.services.customer_auth_service import CustomerAuthService
from app.services.platform_admin_auth_service import PlatformAdminAuthService
from app.utils.datetime import ensure_utc_aware


def _get_redis() -> redis_lib.Redis:
    """Lazy Redis client for jti revocation checks."""
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


def extract_token(request: Request) -> str | None:
    """Extract JWT from cookie first, then Authorization header."""
    token = request.cookies.get("access_token")
    if token:
        return token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def is_token_revoked(jti: str) -> bool:
    """Check if a jti has been revoked via Redis."""
    try:
        return bool(_get_redis().exists(f"revoked:{jti}"))
    except Exception:
        return False


def revoke_token(jti: str, ttl_seconds: int) -> None:
    """Add a jti to the revocation set with auto-expiring TTL."""
    try:
        _get_redis().setex(f"revoked:{jti}", ttl_seconds, "1")
    except Exception:
        pass


class AuthContext(BaseModel):
    """Context wrapper for dual authentication."""

    auth_type: Literal["tenant_user", "platform_admin"]
    user: UserResponse | None = None
    admin: PlatformAdminResponse | None = None

    @property
    def tenant_id(self) -> UUID | None:
        """Get tenant_id if this is a tenant user."""
        return self.user.tenant_id if self.user else None


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> UserResponse:
    """Get current authenticated user from JWT token."""
    token = extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            raise credentials_exception

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

    except JWTError as err:
        raise credentials_exception from err


async def get_current_active_user(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Get current active user (alias for clarity)."""
    return current_user


async def get_current_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> CustomerUserResponse:
    """Get current authenticated B2C customer from JWT token."""
    token = extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate customer credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate customer credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            raise credentials_exception
        auth_service = CustomerAuthService(db)
        token_data = auth_service.verify_token_data(payload)
        if not token_data:
            raise credentials_exception

        customer = auth_service.get_customer_by_id(token_data.customer_user_id)
        if not customer or not customer.is_active:
            raise credentials_exception
        return customer
    except JWTError as err:
        raise credentials_exception from err


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


async def verify_subscription_active(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Tenant:
    """
    Verify tenant has active subscription.

    Checks:
    - Trial hasn't expired
    - Subscription is active (not canceled/past_due)

    Raises:
        HTTPException 402: Trial expired or subscription inactive
    """
    from app.services.tenant_service import TenantService

    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant(current_user.tenant_id)

    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Check trial expiration
    if tenant.subscription_status in {"trialing", "trial"}:
        now = datetime.now(UTC)
        trial_ends_at = ensure_utc_aware(tenant.trial_ends_at)
        if trial_ends_at and now > trial_ends_at:
            # Auto-update status
            tenant.subscription_status = "trial_expired"
            db.commit()

            raise HTTPException(
                status_code=402,  # Payment Required
                detail={
                    "error": "trial_expired",
                    "message": "Your 30-day trial has expired. Please upgrade to continue using Luran.",
                    "trial_ended": trial_ends_at.isoformat(),
                    "upgrade_url": "/dashboard/upgrade",
                    "subscription_tier": tenant.subscription_tier,
                },
            )

    # Check active subscription expiration (for end-of-period cancellations)
    if tenant.subscription_status == "active":
        now = datetime.now(UTC)
        subscription_ends_at = ensure_utc_aware(tenant.subscription_ends_at)

        # If subscription has an end date and it has passed, auto-expire
        if subscription_ends_at and now > subscription_ends_at:
            tenant.subscription_status = "canceled"
            db.commit()

            raise HTTPException(
                status_code=402,
                detail={
                    "error": "subscription_expired",
                    "message": "Your subscription has ended. Please reactivate to continue using Luran.",
                    "subscription_ended": subscription_ends_at.isoformat(),
                    "upgrade_url": "/dashboard/billing",
                    "subscription_tier": tenant.subscription_tier,
                },
            )

    # Check subscription status
    if tenant.subscription_status in ["trial_expired", "canceled", "past_due"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "subscription_inactive",
                "message": f"Subscription {tenant.subscription_status}. Please update payment.",
                "status": tenant.subscription_status,
                "upgrade_url": "/dashboard/billing",
            },
        )

    return tenant


async def verify_conversation_limit(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """
    Check if tenant has reached monthly conversation limit.

    Raises:
        HTTPException 402: Monthly limit reached
    """
    from app.db.models.conversation import ConversationSession
    from app.services.tenant_service import TenantService

    tenant_service = TenantService(db)
    capabilities = tenant_service.get_capabilities(current_user.tenant_id)

    if not capabilities:
        return

    # Skip if unlimited
    if capabilities.max_conversations_per_month is None:
        return

    # Get start of current month

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Count conversations this month
    monthly_count = (
        db.query(ConversationSession)
        .filter(
            ConversationSession.tenant_id == current_user.tenant_id,
            ConversationSession.created_at >= month_start,
        )
        .count()
    )

    # Check limit
    if monthly_count >= capabilities.max_conversations_per_month:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "conversation_limit_reached",
                "message": f"Monthly conversation limit of {capabilities.max_conversations_per_month} reached.",
                "current_count": monthly_count,
                "limit": capabilities.max_conversations_per_month,
                "resets_at": (month_start + timedelta(days=32)).replace(day=1).isoformat(),
            },
        )


async def get_current_user_or_platform_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthContext:
    """
    Accept both tenant user and platform admin authentication.

    Tries to authenticate as tenant user first, then falls back to platform admin.
    Returns AuthContext indicating which auth type succeeded.

    Raises:
        HTTPException 401: If both auth attempts fail
    """
    token = extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            raise credentials_exception

        # Try tenant user auth first
        try:
            auth_service = AuthService(db)
            token_data = auth_service.verify_token_data(payload)

            if token_data and hasattr(token_data, "user_id"):
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
        admin_token_data = admin_service.verify_token_data(payload)

        if admin_token_data and hasattr(admin_token_data, "admin_id"):
            admin = admin_service.get_admin_by_id(admin_token_data.admin_id)
            if admin and admin.is_active:
                return AuthContext(
                    auth_type="platform_admin",
                    user=None,
                    admin=admin,
                )

        raise credentials_exception

    except JWTError as err:
        raise credentials_exception from err


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
    request: Request,
    db: Session = Depends(get_db),
) -> PlatformAdminResponse:
    """Get current authenticated platform admin from JWT token."""
    token = extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            raise credentials_exception

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

    except JWTError as err:
        raise credentials_exception from err


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
