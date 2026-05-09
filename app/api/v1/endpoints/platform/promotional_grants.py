"""Platform admin endpoints for promotional grants."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.promotional_grant import (
    PreviewRequest,
    PreviewResponse,
    PreviewTenantSummary,
)
from app.services.promotional_grant_service import (
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
