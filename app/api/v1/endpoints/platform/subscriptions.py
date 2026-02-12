"""Platform admin subscription management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.platform_subscription import (
    PaymentTransactionResponse,
    SubscriptionOverrideResponse,
    TenantSubscriptionDetailResponse,
    TrialReminderHistoryResponse,
    TenantSubscriptionOverrideRequest,
    TrialExtensionRequest,
    TrialExtensionResponse,
)
from app.services.platform_subscription_service import PlatformSubscriptionService
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()


@router.post(
    "/{tenant_id}/override",
    response_model=SubscriptionOverrideResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def override_tenant_subscription(
    tenant_id: UUID,
    request: TenantSubscriptionOverrideRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Manually override a tenant's subscription (platform admin only).

    Use cases:
    - Enterprise deals negotiated offline
    - Promotional upgrades
    - VIP customer exceptions
    - Support resolutions

    The override is recorded in subscription history for audit trail.

    Args:
        tenant_id: Tenant UUID to override
        request: Override details (tier, billing_cycle, reason, apply_tier_capabilities)
        db: Database session
        current_admin: Current platform admin

    Returns:
        SubscriptionOverrideResponse with success message and details
    """
    service = PlatformSubscriptionService(db)

    try:
        result = service.override_tenant_subscription(
            tenant_id=tenant_id,
            tier=request.tier,
            billing_cycle=request.billing_cycle,
            reason=request.reason,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
            apply_tier_capabilities=request.apply_tier_capabilities,
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
            detail=f"Failed to override subscription: {str(e)}",
        ) from e


@router.post(
    "/{tenant_id}/trial/extend",
    response_model=TrialExtensionResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def extend_trial_period(
    tenant_id: UUID,
    request: TrialExtensionRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Extend a tenant's trial period (platform admin only).

    Use cases:
    - Sales demos require more time
    - Support issues delayed customer evaluation
    - Re-engagement campaigns for expired trials
    - Special promotional extensions

    Can extend active trials or reactivate expired trials.
    Only works for tenants on trial tier.

    Args:
        tenant_id: Tenant UUID to extend
        request: Extension details (days, reason)
        db: Database session
        current_admin: Current platform admin

    Returns:
        TrialExtensionResponse with new trial end date
    """
    service = PlatformSubscriptionService(db)

    try:
        result = service.extend_trial(
            tenant_id=tenant_id,
            days=request.days,
            reason=request.reason,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
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
            detail=f"Failed to extend trial: {str(e)}",
        ) from e


@router.get(
    "/{tenant_id}",
    response_model=TenantSubscriptionDetailResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_tenant_subscription_details(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    Get complete subscription details for a tenant (platform admin only).

    Returns:
    - Current subscription status and tier
    - Trial and billing dates
    - Manual capability overrides
    - Recent subscription history
    - Payment statistics

    Args:
        tenant_id: Tenant UUID
        db: Database session
        current_admin: Current platform admin

    Returns:
        TenantSubscriptionDetailResponse with full details
    """
    service = PlatformSubscriptionService(db)

    try:
        details = service.get_subscription_details(tenant_id)
        return details
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get subscription details: {str(e)}",
        ) from e


@router.get(
    "/payments/all",
    response_model=list[PaymentTransactionResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_payment_transactions(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    status_filter: str | None = Query(None, description="Filter by payment status"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List payment transactions (platform admin only).

    Use cases:
    - Financial reconciliation
    - Review failed payments
    - Audit trail
    - Revenue reporting

    Supports filtering by tenant, status, and pagination.

    Args:
        tenant_id: Optional tenant filter
        status_filter: Optional status filter (success, pending, failed)
        limit: Maximum results (1-1000)
        offset: Pagination offset
        db: Database session
        current_admin: Current platform admin

    Returns:
        List of payment transactions with tenant names
    """
    service = PlatformSubscriptionService(db)

    try:
        transactions = service.get_payment_transactions(
            tenant_id=tenant_id,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return transactions
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list transactions: {str(e)}",
        ) from e


@router.get(
    "/reminders/trial",
    response_model=TrialReminderHistoryResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_trial_reminder_history(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    days_remaining: int | None = Query(
        None,
        ge=0,
        le=365,
        description="Filter by reminder window in days (e.g., 14, 7, 3, 1, 0)",
    ),
    status_filter: str | None = Query(
        None,
        description="Filter by delivery status (sent, failed, skipped, pending)",
    ),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List trial reminder email history (platform admin only).

    Useful for auditing reminder delivery and troubleshooting notification gaps.
    """
    service = PlatformSubscriptionService(db)

    try:
        return service.get_trial_reminder_history(
            tenant_id=tenant_id,
            days_remaining=days_remaining,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch trial reminder history: {str(e)}",
        ) from e


@router.get(
    "/reminders/renewal",
    response_model=TrialReminderHistoryResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_renewal_reminder_history(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    days_remaining: int | None = Query(
        None,
        ge=1,
        le=365,
        description="Filter by reminder window in days (e.g., 7, 3, 1)",
    ),
    status_filter: str | None = Query(
        None,
        description="Filter by delivery status (sent, failed, skipped, pending)",
    ),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    """
    List paid-subscription renewal reminder email history (platform admin only).

    Useful for auditing renewal reminder delivery before charge dates.
    """
    service = PlatformSubscriptionService(db)

    try:
        return service.get_renewal_reminder_history(
            tenant_id=tenant_id,
            days_remaining=days_remaining,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch renewal reminder history: {str(e)}",
        ) from e
