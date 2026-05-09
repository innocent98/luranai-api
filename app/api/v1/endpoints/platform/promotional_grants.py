"""Platform admin endpoints for promotional grants."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.db.models.promotional_grant import PromotionalGrant
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.promotional_grant import (
    BulkGrantRequest,
    BulkGrantResponse,
    PreviewRequest,
    PreviewResponse,
    PreviewTenantSummary,
    PromotionalGrantResponse,
    RevokeGrantRequest,
    RevokeGrantResponse,
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
def bulk_create_grants(
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
