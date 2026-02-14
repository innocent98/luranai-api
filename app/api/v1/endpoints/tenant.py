"""Tenant self-management endpoints (for tenant admins managing their own tenant)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.schemas.tenant import (
    TenantAIConfigResponse,
    TenantAIConfigUpdate,
    TenantCapabilitiesResponse,
    TenantResponse,
    TenantUpdate,
)
from app.schemas.user import UserResponse
from app.services.tenant_service import TenantService

router = APIRouter()


@router.get("", response_model=TenantResponse, status_code=status.HTTP_200_OK)
def get_my_tenant(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get current tenant details (my company).

    Returns complete tenant information including AI configuration and capabilities.
    Tenant ID is automatically determined from the authenticated user's JWT token.

    Args:
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        Current tenant details with AI config and capabilities
    """
    tenant_service = TenantService(db)
    tenant = tenant_service.get_tenant_by_id(current_user.tenant_id)

    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    return tenant


@router.put("", response_model=TenantResponse, status_code=status.HTTP_200_OK)
def update_my_tenant(
    tenant_data: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Update current tenant information (my company).

    Allows updating basic company information like display name and description.
    Cannot update: name (immutable), status (platform admin only), or API key.

    Args:
        tenant_data: Updated tenant data
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        Updated tenant details

    Example:
        {
            "display_name": "New Company Name Inc.",
            "description": "Updated company description"
        }
    """
    tenant_service = TenantService(db)

    try:
        tenant = tenant_service.update_tenant(current_user.tenant_id, tenant_data)
        return tenant
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


# AI Configuration endpoints


@router.get(
    "/ai-config",
    response_model=TenantAIConfigResponse,
    status_code=status.HTTP_200_OK,
)
def get_my_ai_config(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get current tenant's AI configuration.

    Returns AI chatbot behavior settings including brand voice, escalation rules,
    and response generation parameters.

    Args:
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        AI configuration for current tenant
    """
    tenant_service = TenantService(db)
    config = tenant_service.get_ai_config(current_user.tenant_id)

    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI config not found")

    return config


@router.put(
    "/ai-config",
    response_model=TenantAIConfigResponse,
    status_code=status.HTTP_200_OK,
)
def update_my_ai_config(
    config_data: TenantAIConfigUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Update current tenant's AI configuration.

    Customize your AI chatbot's behavior, personality, and response rules.
    Changes take effect immediately on the next chat message.

    Args:
        config_data: Updated AI configuration
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        Updated AI configuration

    Example:
        {
            "brand_voice": {
                "tone": "friendly",
                "formality_level": "casual",
                "emoji_usage": true
            },
            "escalation_keywords": ["speak to human", "manager"],
            "max_response_tokens": 450,
            "custom_instructions": "Always mention our 24/7 hotline."
        }
    """
    tenant_service = TenantService(db)

    try:
        config = tenant_service.update_ai_config(current_user.tenant_id, config_data)
        return config
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


# Capabilities endpoints (read-only for tenant admins)


@router.get(
    "/capabilities",
    response_model=TenantCapabilitiesResponse,
    status_code=status.HTTP_200_OK,
)
def get_my_capabilities(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get current tenant's capabilities and subscription limits (read-only).

    View what features are enabled and resource limits for your subscription tier.
    To change capabilities, contact platform support or upgrade your subscription.

    Args:
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        Capabilities and limits for current tenant
    """
    tenant_service = TenantService(db)
    capabilities = tenant_service.get_capabilities(current_user.tenant_id)

    if not capabilities:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capabilities not found")

    return capabilities
