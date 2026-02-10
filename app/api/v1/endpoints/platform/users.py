"""Platform user management API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_platform_admin, get_db
from app.db.models.user import UserRole
from app.schemas.platform_admin import PlatformAdminResponse
from app.services.user_management_service import UserManagementService
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()


class UserDeactivationRequest(BaseModel):
    """Request model for user deactivation."""

    reason: str


@router.get(
    "/search", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)]
)
def search_users(
    email: str | None = Query(None, description="Email filter (partial match)"),
    tenant_id: str | None = Query(None, description="Tenant ID filter"),
    role: str | None = Query(None, description="Role filter"),
    is_active: bool | None = Query(None, description="Active status filter"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Results offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Search users across all tenants.

    Returns:
        Paginated list of users with tenant information
    """
    try:
        user_service = UserManagementService(db)

        # Parse tenant_id if provided
        parsed_tenant_id = UUID(tenant_id) if tenant_id else None

        # Parse role if provided
        parsed_role = UserRole(role) if role else None

        results = user_service.search_users(
            email=email,
            tenant_id=parsed_tenant_id,
            role=parsed_role,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )

        return results

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parameter: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search users: {str(e)}",
        ) from e


@router.get(
    "/{user_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(platform_admin_rate_limit)]
)
def get_user_details(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get detailed information about a specific user.

    Returns:
        User details including tenant info and recent activity
    """
    try:
        user_service = UserManagementService(db)
        details = user_service.get_user_details(user_id=user_id)

        return details

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user details: {str(e)}",
        ) from e


@router.get(
    "/{user_id}/activity",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_user_activity(
    user_id: UUID,
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get activity summary for a specific user.

    Returns:
        User activity summary from audit logs
    """
    try:
        user_service = UserManagementService(db)
        activity = user_service.get_user_activity(
            user_id=user_id,
            days=days,
        )

        return activity

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user activity: {str(e)}",
        ) from e


@router.post(
    "/{user_id}/deactivate",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def deactivate_user(
    user_id: UUID,
    request: UserDeactivationRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Deactivate a user account.

    Returns:
        Deactivation status and details
    """
    try:
        user_service = UserManagementService(db)
        result = user_service.deactivate_user_platform_admin(
            user_id=user_id,
            reason=request.reason,
            admin_id=current_admin.id,
        )

        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate user: {str(e)}",
        ) from e


@router.get(
    "/analytics/role-distribution",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_role_distribution(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_platform_admin),
):
    """
    Get distribution of users by role.

    Returns:
        User role distribution statistics
    """
    try:
        user_service = UserManagementService(db)
        distribution = user_service.get_role_distribution()

        return distribution

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch role distribution: {str(e)}",
        ) from e
