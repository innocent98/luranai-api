"""Platform admin tenant management endpoints."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.services.tenant_service import TenantService
from app.services.platform_subscription_service import PlatformSubscriptionService
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
    status_filter: Optional[TenantStatus] = Query(None, description="Filter by tenant status (ACTIVE, INACTIVE, SUSPENDED)"),
    subscription_status: Optional[str] = Query(None, description="Filter by subscription status (trial, active, trial_expired, etc.)"),
    subscription_tier: Optional[str] = Query(None, description="Filter by subscription tier (trial, lite, professional, enterprise)"),
    trial_expiring_days: Optional[int] = Query(None, ge=0, le=90, description="Find trials expiring within N days (0-90)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List all tenants with optional subscription filters (platform admin only).

    This endpoint is for the platform admin dashboard to view and filter tenants.

    Filters:
    - status_filter: Filter by tenant status (ACTIVE, INACTIVE, SUSPENDED)
    - subscription_status: Filter by subscription status (trial, active, trial_expired, past_due, etc.)
    - subscription_tier: Filter by subscription tier (trial, lite, professional, enterprise)
    - trial_expiring_days: Find trials expiring within N days (useful for outreach campaigns)

    Use cases:
    - Find trial_expired tenants for conversion outreach
    - List enterprise customers
    - Identify trials expiring in 7 days for reminder emails
    - Filter past_due subscriptions for payment follow-up

    Args:
        status_filter: Optional tenant status filter
        subscription_status: Optional subscription status filter
        subscription_tier: Optional subscription tier filter
        trial_expiring_days: Optional trial expiration filter (days)
        limit: Maximum results (1-1000)
        offset: Pagination offset
        db: Database session
        current_admin: Current platform admin

    Returns:
        List of tenants matching filter criteria
    """
    # If subscription filters provided, use subscription service
    if subscription_status or subscription_tier or trial_expiring_days is not None:
        subscription_service = PlatformSubscriptionService(db)
        tenants = subscription_service.get_tenants_by_subscription(
            subscription_status=subscription_status,
            subscription_tier=subscription_tier,
            trial_expiring_days=trial_expiring_days,
            limit=limit,
            offset=offset,
        )

        # Apply status filter if provided
        if status_filter:
            tenants = [t for t in tenants if t.status == status_filter]

        # Convert to list response format
        return [TenantListResponse.model_validate(t) for t in tenants]
    else:
        # Use existing tenant service for simple status filtering
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
