"""Tenant management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_super_admin,
    get_current_tenant_admin,
    get_db,
    verify_tenant_access,
)
from app.db.models.tenant import TenantStatus
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.tenant import (
    TenantAIConfigResponse,
    TenantAIConfigUpdate,
    TenantCapabilitiesResponse,
    TenantCapabilitiesUpdate,
    TenantListResponse,
    TenantResponse,
    TenantUpdate,
)
from app.schemas.user import UserResponse
from app.services.tenant_service import TenantService

router = APIRouter()


@router.get("", response_model=list[TenantListResponse], status_code=status.HTTP_200_OK)
def list_tenants(
    status_filter: TenantStatus | None = Query(None, description="Filter by status"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List all tenants (superuser only).

    Args:
        status_filter: Optional status filter
        db: Database session
        current_user: Current superuser

    Returns:
        List of tenants
    """
    tenant_service = TenantService(db)
    tenants = tenant_service.get_all_tenants(status=status_filter)
    return tenants


@router.get("/{tenant_id}", response_model=TenantResponse, status_code=status.HTTP_200_OK)
def get_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get tenant by ID.

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Tenant details with AI config and capabilities
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant_by_id(tenant_id)

    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    return tenant


@router.put("/{tenant_id}", response_model=TenantResponse, status_code=status.HTTP_200_OK)
def update_tenant(
    tenant_id: UUID,
    tenant_data: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Update tenant information.

    Args:
        tenant_id: Tenant UUID
        tenant_data: Updated tenant data
        db: Database session
        current_user: Current admin user

    Returns:
        Updated tenant
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    tenant_service = TenantService(db)

    try:
        tenant = tenant_service.update_tenant(tenant_id, tenant_data)
        return tenant
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.delete("/{tenant_id}", status_code=status.HTTP_200_OK)
def delete_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Delete (deactivate) a tenant (superuser only).

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_user: Current superuser

    Returns:
        Success message
    """
    tenant_service = TenantService(db)

    try:
        tenant_service.delete_tenant(tenant_id)
        return {"message": "Tenant deactivated successfully"}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


# AI Configuration endpoints


@router.get(
    "/{tenant_id}/ai-config",
    response_model=TenantAIConfigResponse,
    status_code=status.HTTP_200_OK,
)
def get_ai_config(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get AI configuration for a tenant.

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_user: Current admin user

    Returns:
        AI configuration
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    tenant_service = TenantService(db)
    config = tenant_service.get_ai_config(tenant_id, with_counts=True)

    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI config not found")

    return config


@router.put(
    "/{tenant_id}/ai-config",
    response_model=TenantAIConfigResponse,
    status_code=status.HTTP_200_OK,
)
def update_ai_config(
    tenant_id: UUID,
    config_data: TenantAIConfigUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Update AI configuration for a tenant.

    Args:
        tenant_id: Tenant UUID
        config_data: Updated configuration
        db: Database session
        current_user: Current admin user

    Returns:
        Updated AI configuration
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    tenant_service = TenantService(db)

    try:
        config = tenant_service.update_ai_config(tenant_id, config_data)
        return config
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


# Capabilities endpoints


@router.get(
    "/{tenant_id}/capabilities",
    response_model=TenantCapabilitiesResponse,
    status_code=status.HTTP_200_OK,
)
def get_capabilities(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get capabilities for a tenant.

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Tenant capabilities
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    tenant_service = TenantService(db)
    capabilities = tenant_service.get_capabilities(tenant_id)

    if not capabilities:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capabilities not found")

    return capabilities


@router.put(
    "/{tenant_id}/capabilities",
    response_model=TenantCapabilitiesResponse,
    status_code=status.HTTP_200_OK,
)
def update_capabilities(
    tenant_id: UUID,
    capabilities_data: TenantCapabilitiesUpdate,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Update capabilities for a tenant (superuser only).

    Args:
        tenant_id: Tenant UUID
        capabilities_data: Updated capabilities
        db: Database session
        current_user: Current superuser

    Returns:
        Updated capabilities
    """
    tenant_service = TenantService(db)

    try:
        capabilities = tenant_service.update_capabilities(tenant_id, capabilities_data)
        return capabilities
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
