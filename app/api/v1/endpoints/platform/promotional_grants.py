"""Platform admin endpoints for promotional grants."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.promotional_grant import (
    AutoSignupConfigResponse,
    AutoSignupConfigUpdateRequest,
    AutoSignupConfigUpdateResponse,
    BulkGrantRequest,
    BulkGrantResponse,
    CampaignListResponse,
    CampaignStats,
    CampaignStatsBySource,
    CampaignSummary,
    PreviewRequest,
    PreviewResponse,
    PreviewTenantSummary,
    PromotionalGrantResponse,
    RevokeGrantRequest,
    RevokeGrantResponse,
)
from app.services.auto_signup_grant_config_service import (
    AutoSignupConfigValidationError,
    AutoSignupGrantConfigService,
)
from app.services.promotional_grant_service import (
    PromoGrantNotFoundError,
    PromoGrantStateError,
    PromoGrantValidationError,
    PromotionalGrantService,
)
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()

PREVIEW_SAMPLE_LIMIT = 10
BULK_MAX_PER_REQUEST = 5000


@router.post(
    "/preview",
    response_model=PreviewResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def preview_cohort(
    request: PreviewRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """Preview the cohort of tenants matching the given filters (platform admin only).

    Returns the matched count, a sample of up to 10 tenants, and a signed
    preview token that can be passed to the bulk-create endpoint.

    Args:
        request: Cohort filter criteria and exclusion settings
        db: Database session
        current_admin: Current platform super admin

    Returns:
        PreviewResponse with matched_count, sample_tenants, and preview_token
    """
    service = PromotionalGrantService(db)
    matched = service.build_cohort(
        subscription_statuses=request.filters.subscription_statuses,
        churned_within_days=request.filters.churned_within_days,
        exclude_already_granted=request.exclude_already_granted,
        exclude_previously_granted=request.exclude_previously_granted,
    )
    sample = [
        PreviewTenantSummary(
            tenant_id=t.id,
            name=t.name,
            display_name=t.display_name,
            subscription_status=t.subscription_status,
            churned_at=t.trial_ends_at or t.subscription_ends_at,
        )
        for t in matched[:PREVIEW_SAMPLE_LIMIT]
    ]
    token = service.encode_preview_token([t.id for t in matched])
    return PreviewResponse(
        matched_count=len(matched),
        sample_tenants=sample,
        preview_token=token,
    )


@router.post(
    "/",
    response_model=BulkGrantResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
async def bulk_create_grants(
    request: BulkGrantRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    try:
        tenant_ids = service.decode_preview_token(request.preview_token)
    except PromoGrantValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    if len(tenant_ids) > BULK_MAX_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bulk operations are capped at {BULK_MAX_PER_REQUEST} tenants per request",
        )

    result = service.bulk_create_grants(
        tenant_ids=tenant_ids,
        campaign_code=request.campaign_code,
        campaign_name=request.campaign_name,
        granted_tier=request.granted_tier,
        duration_days=request.duration_days,
        granted_by_admin_id=current_admin.id,
        granted_by_admin_name=current_admin.email,
        reason=request.reason,
        force_replace_active=request.force_replace_active,
    )

    # Enqueue 'you've been granted Pro' emails in the background.
    # The send is idempotent (7-day window) so duplicate enqueues are safe.
    granted_ids: list = result.get("granted_ids", [])
    if granted_ids:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            from app.core.config import settings

            redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await redis.enqueue_job(
                "send_promo_granted_emails_batch",
                [str(gid) for gid in granted_ids],
            )
        except Exception as e:
            # Don't fail the API call if the queue is unavailable; the grants
            # are already committed. If the queue is down, granted emails are
            # not sent — re-running the campaign on the same cohort will pick
            # them up (send_promo_granted_email is idempotent over 7 days).
            from loguru import logger

            logger.error(f"Failed to enqueue promo granted emails: {e}")

    return BulkGrantResponse(
        campaign_code=request.campaign_code,
        granted=result["granted"],
        skipped_already_granted=result["skipped_already_granted"],
        failed=result["failed"],
        grant_ids_sample=result["grant_ids_sample"],
    )


@router.post(
    "/{grant_id}/revoke",
    response_model=RevokeGrantResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def revoke_grant(
    grant_id: UUID,
    request: RevokeGrantRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    try:
        result = service.revoke_grant(
            grant_id=grant_id,
            reason=request.reason,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
        )
        return RevokeGrantResponse(**result)
    except PromoGrantNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PromoGrantStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except PromoGrantValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.get(
    "/tenant/{tenant_id}",
    response_model=list[PromotionalGrantResponse],
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_grants_for_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    grants = (
        db.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant_id)
        .order_by(PromotionalGrant.created_at.desc())
        .all()
    )
    return [PromotionalGrantResponse.model_validate(g) for g in grants]


@router.get(
    "/campaigns",
    response_model=CampaignListResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_campaigns(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    raw = service.list_campaigns_with_stats()
    campaigns = [
        CampaignSummary(
            campaign_code=c["campaign_code"],
            campaign_name=c["campaign_name"],
            granted_tier=c["granted_tier"],
            duration_days=c["duration_days"],
            first_grant_at=c["first_grant_at"],
            stats=CampaignStats(
                total=c["stats"]["total"],
                active=c["stats"]["active"],
                converted=c["stats"]["converted"],
                expired=c["stats"]["expired"],
                revoked=c["stats"]["revoked"],
                by_source=CampaignStatsBySource(**c["stats"]["by_source"]),
            ),
        )
        for c in raw
    ]
    return CampaignListResponse(campaigns=campaigns)


@router.get(
    "/campaigns/{campaign_code}",
    response_model=list[PromotionalGrantResponse],
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_grants_for_campaign(
    campaign_code: str,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    rows = (
        db.query(PromotionalGrant, Tenant.name, Tenant.display_name)
        .join(Tenant, Tenant.id == PromotionalGrant.tenant_id)
        .filter(PromotionalGrant.campaign_code == campaign_code)
        .order_by(PromotionalGrant.created_at.desc())
        .all()
    )
    return [
        PromotionalGrantResponse.model_validate(
            {
                **{
                    col.name: getattr(grant, col.name)
                    for col in PromotionalGrant.__table__.columns
                },
                "tenant_name": tenant_name,
                "tenant_display_name": tenant_display_name,
            }
        )
        for grant, tenant_name, tenant_display_name in rows
    ]


@router.get(
    "/config/auto-signup",
    response_model=AutoSignupConfigResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_auto_signup_config(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    config = AutoSignupGrantConfigService(db).get()
    return AutoSignupConfigResponse.model_validate(config)


@router.put(
    "/config/auto-signup",
    response_model=AutoSignupConfigUpdateResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def update_auto_signup_config(
    request: AutoSignupConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = AutoSignupGrantConfigService(db)
    try:
        updated = service.update(
            enabled=request.enabled,
            campaign_code=request.campaign_code,
            campaign_name=request.campaign_name,
            granted_tier=request.granted_tier,
            duration_days=request.duration_days,
            valid_until=request.valid_until,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
        )
    except AutoSignupConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return AutoSignupConfigUpdateResponse.model_validate(updated)
