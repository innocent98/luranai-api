"""Platform admin tenant management endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.services.tenant_service import TenantService
from app.schemas.tenant import (
    TenantResponse,
    TenantListResponse,
    TenantCapabilitiesUpdate,
    TenantCapabilitiesResponse,
)
from app.schemas.platform_admin import PlatformAdminResponse
from app.db.models.tenant import TenantStatus

router = APIRouter()


@router.get("", response_model=list[TenantListResponse], status_code=status.HTTP_200_OK)
def list_all_tenants(
    status_filter: Optional[TenantStatus] = Query(None, description="Filter by status"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List all tenants (platform admin only).

    This endpoint is for the platform admin dashboard to view all registered companies.

    Args:
        status_filter: Optional status filter (ACTIVE, INACTIVE, SUSPENDED)
        db: Database session
        current_admin: Current platform admin

    Returns:
        List of all tenants in the system
    """
    tenant_service = TenantService(db)
    tenants = tenant_service.get_all_tenants(status=status_filter)
    return tenants


@router.get("/{tenant_id}", response_model=TenantResponse, status_code=status.HTTP_200_OK)
def get_tenant_by_id(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Get any tenant by ID (platform admin only).

    This endpoint allows platform admins to view details of any tenant,
    including their AI configuration and capabilities.

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_admin: Current platform admin

    Returns:
        Tenant details with AI config and capabilities
    """
    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant_by_id(tenant_id)

    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    return tenant


@router.delete("/{tenant_id}", status_code=status.HTTP_200_OK)
def deactivate_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Deactivate a tenant (platform admin only).

    This is a soft delete - sets tenant status to INACTIVE.
    Data is preserved for compliance and potential reactivation.

    Use cases:
    - Subscription expired/cancelled
    - Terms of service violation
    - Company requested account closure

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_admin: Current platform admin

    Returns:
        Success message
    """
    tenant_service = TenantService(db)

    try:
        tenant_service.delete_tenant(tenant_id)
        return {"message": "Tenant deactivated successfully"}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.put(
    "/{tenant_id}/capabilities",
    response_model=TenantCapabilitiesResponse,
    status_code=status.HTTP_200_OK,
)
def update_tenant_capabilities(
    tenant_id: UUID,
    capabilities_data: TenantCapabilitiesUpdate,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Update tenant capabilities and subscription limits (platform admin only).

    This endpoint is for managing tenant subscriptions, feature flags, and resource limits.

    Use cases:
    - Subscription tier upgrade/downgrade
    - Enable/disable features based on payment status
    - Increase limits for enterprise customers
    - Apply trial period restrictions

    Args:
        tenant_id: Tenant UUID
        capabilities_data: Updated capabilities
        db: Database session
        current_admin: Current platform admin

    Returns:
        Updated capabilities

    Example:
        {
            "can_upload_knowledge": false,  // Disable for non-payment
            "max_knowledge_mb": 500,        // Upgrade to 500 MB
            "max_conversations_per_month": 10000  // Set conversation limit
        }
    """
    tenant_service = TenantService(db)

    try:
        capabilities = tenant_service.update_capabilities(tenant_id, capabilities_data)
        return capabilities
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
