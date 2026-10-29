"""Tenant self-management endpoints (for tenant admins managing their own tenant)."""

import dataclasses
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_current_user, get_db
from app.db.models.tenant import Tenant, TenantAIConfig
from app.schemas.entitlements import EntitlementsResponse
from app.schemas.onboarding import OnboardingStatus
from app.schemas.tenant import (
    CuratedVoiceResponse,
    TenantAIConfigResponse,
    TenantAIConfigUpdate,
    TenantCapabilitiesResponse,
    TenantResponse,
    TenantUpdate,
    VoiceSettingsResponse,
    VoiceSettingsUpdate,
)
from app.schemas.user import UserResponse
from app.services.entitlements import get_entitlements
from app.services.first_responder import default_first_responder
from app.services.onboarding_service import get_onboarding_status
from app.services.tenant_service import TenantService
from app.services.voice.metering import VoiceMeter
from app.services.voice.voices import CURATED_VOICES, is_curated_voice_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/entitlements", response_model=EntitlementsResponse, status_code=status.HTTP_200_OK)
def get_my_entitlements(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """What the caller's plan includes. Any authenticated tenant user; always the caller's own tenant.

    Resolution order: plan defaults, then the tenant's manual overrides, then an active
    promotional grant's tier (see app/services/entitlements.py).
    """
    tenant = db.get(Tenant, current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return get_entitlements(tenant, db).to_response()


@router.get("/onboarding", response_model=OnboardingStatus, status_code=status.HTTP_200_OK)
def get_my_onboarding(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Activation checklist for the caller's own tenant: knowledge indexed, widget seen on a
    site, a real test conversation, and whether the AI will answer (or what blocks it).
    """
    return get_onboarding_status(db, current_user.tenant_id)


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
    config = tenant_service.get_ai_config(current_user.tenant_id, with_counts=True)

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


@router.get(
    "/voice/settings",
    response_model=VoiceSettingsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_my_voice_settings(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """Everything the tenant's Voice Chat settings page needs, in one call.

    Always returns every field regardless of `granted` — the endpoint reports
    state and the page decides what to render. A conditional shape would be
    harder to type on the client and gives the page less than it needs.
    """
    tenant_service = TenantService(db)
    capabilities = tenant_service.get_capabilities(current_user.tenant_id)

    config = (
        db.query(TenantAIConfig).filter(TenantAIConfig.tenant_id == current_user.tenant_id).first()
    )

    # Usage must DEGRADE, not fail. A settings page that 500s because a cache
    # is down is worse than one missing a number.
    try:
        minutes_used = await VoiceMeter().get_monthly_minutes(current_user.tenant_id)
    except Exception:  # noqa: BLE001 — usage is informational, never load-bearing
        logger.warning(
            "Voice usage read failed for tenant %s", current_user.tenant_id, exc_info=True
        )
        minutes_used = None

    return VoiceSettingsResponse(
        granted=bool(capabilities and capabilities.can_use_voice),
        enabled=True if config is None else config.voice_enabled,
        voice_id=config.voice_id if config else None,
        minutes_used=minutes_used,
        minutes_limit=capabilities.max_voice_minutes_per_month if capabilities else None,
        available_voices=[CuratedVoiceResponse(**dataclasses.asdict(v)) for v in CURATED_VOICES],
    )


@router.put(
    "/voice/settings",
    response_model=VoiceSettingsResponse,
    status_code=status.HTTP_200_OK,
)
async def update_my_voice_settings(
    payload: VoiceSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """Update the tenant's OWN voice preference.

    This endpoint has no code path that reads or writes TenantCapabilities.
    A tenant can switch their own voice off and on and choose a voice; they
    cannot grant themselves the capability. `enabled` is writable even when
    ungranted — a tenant may pre-configure, and the gate still refuses to mint.
    """
    if payload.voice_id is not None and not is_curated_voice_id(payload.voice_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown voice id",
        )

    config = (
        db.query(TenantAIConfig).filter(TenantAIConfig.tenant_id == current_user.tenant_id).first()
    )
    if config is None:
        config = TenantAIConfig(
            tenant_id=current_user.tenant_id,
            first_responder=default_first_responder(db, current_user.tenant_id),
        )
        db.add(config)

    config.voice_enabled = payload.enabled
    config.voice_id = payload.voice_id
    db.commit()

    return await get_my_voice_settings(db=db, current_user=current_user)
