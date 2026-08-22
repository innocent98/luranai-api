"""Subscription management endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_current_user, get_db
from app.core.config import settings
from app.schemas.payment import (
    PaymentInitializeRequest,
    PaymentInitializeResponse,
    PaymentVerificationRequest,
    PaymentVerificationResponse,
)
from app.schemas.platform_subscription import PricingPlanResponse, SubscriptionHistoryResponse
from app.schemas.subscription import (
    MySubscriptionResponse,
    SubscriptionCancelRequest,
    SubscriptionCancelResponse,
    SubscriptionDowngradeRequest,
    SubscriptionDowngradeResponse,
)
from app.schemas.user import UserResponse
from app.services.paystack_service import PaystackService
from app.services.platform_subscription_service import PlatformSubscriptionService
from app.services.subscription_service import SubscriptionService
from app.services.webhook_handler_service import WebhookHandlerService
from app.utils.http_rate_limiter import (
    payment_rate_limit,
    subscription_rate_limit,
    subscription_read_rate_limit,
)

router = APIRouter()


def _tier_rank(tier: str) -> int:
    return {
        "trial": 0,
        "lite": 1,
        "professional": 2,
        "growth": 3,
        "enterprise": 4,
    }.get(tier, -1)


@router.post(
    "/upgrade/initialize",
    response_model=PaymentInitializeResponse,
    dependencies=[Depends(payment_rate_limit)],
)
async def initialize_subscription_payment(
    payment_request: PaymentInitializeRequest,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Initialize payment for subscription upgrade.

    Creates a Paystack transaction and returns payment URL.
    User will be redirected to Paystack to complete payment.

    After payment, webhook will handle subscription upgrade automatically.

    Args:
        payment_request: Payment initialization request with tier and billing cycle
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Payment initialization response with authorization URL

    Raises:
        HTTPException 400: Invalid tier or billing cycle
        HTTPException 402: Trial expired or subscription inactive
    """
    # Validate tier and billing cycle
    if payment_request.tier not in ["lite", "professional", "growth"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tier. Choose 'lite', 'professional', or 'growth'. For Enterprise, contact sales.",
        )

    if payment_request.billing_cycle not in ["monthly", "annual"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid billing cycle. Choose 'monthly' or 'annual'.",
        )

    from app.db.models.tenant import Tenant

    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    current_tier = str(tenant.subscription_tier)
    requested_tier = str(payment_request.tier)

    if _tier_rank(requested_tier) <= _tier_rank(current_tier):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lower-tier changes must use the downgrade flow. Please use the downgrade page to move to a lower plan.",
        )

    # Initialize Paystack payment
    paystack_service = PaystackService(db)
    webhook_handler = WebhookHandlerService(db)

    try:
        # Initialize recurring subscription
        payment_data = await paystack_service.initialize_subscription(
            email=current_user.email,
            tier=payment_request.tier,
            billing_cycle=payment_request.billing_cycle,
            tenant_id=str(current_user.tenant_id),
            callback_url=f"{settings.FRONTEND_URL}/dashboard/settings/subscription/callback",
        )

        # Create transaction record
        webhook_handler.create_transaction(
            tenant_id=current_user.tenant_id,
            reference=payment_data["reference"],
            amount=payment_data["amount"],
            tier=payment_request.tier,
            billing_cycle=payment_request.billing_cycle,
            customer_email=current_user.email,
        )

        return PaymentInitializeResponse(
            authorization_url=payment_data["authorization_url"],
            access_code=payment_data["access_code"],
            reference=payment_data["reference"],
            amount=payment_data["amount"],
            currency=payment_data["currency"],
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize payment.",
        ) from e


@router.post(
    "/upgrade/verify",
    response_model=PaymentVerificationResponse,
    dependencies=[Depends(payment_rate_limit)],
)
async def verify_subscription_payment(
    verification_request: PaymentVerificationRequest,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Verify payment and check subscription upgrade status.

    This endpoint is called after user returns from Paystack payment page.
    It verifies the payment status and returns the result.

    Note: Subscription upgrade is handled by webhook automatically.
    This endpoint just verifies payment status for UI feedback.

    Args:
        verification_request: Verification request with payment reference
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Payment verification response with status

    Raises:
        HTTPException 400: Invalid reference
        HTTPException 404: Transaction not found
    """
    paystack_service = PaystackService(db)
    webhook_handler = WebhookHandlerService(db)

    try:
        # Verify transaction with Paystack
        verification_data = await paystack_service.verify_transaction(
            verification_request.reference
        )

        # Get transaction record
        transaction = webhook_handler.get_transaction_by_reference(verification_request.reference)

        if not transaction:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found",
            )

        # Verify tenant matches
        if transaction.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unauthorized: Transaction belongs to different tenant",
            )

        return PaymentVerificationResponse(
            status=verification_data["status"],
            reference=verification_data["reference"],
            amount=verification_data["amount"],
            currency=verification_data["currency"],
            paid_at=verification_data.get("paid_at"),
            channel=verification_data.get("channel"),
            customer_email=verification_data.get("customer_email"),
            subscription_code=transaction.subscription_code,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify payment: {str(e)}",
        ) from e


@router.get(
    "/plans",
    response_model=list[PricingPlanResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(subscription_read_rate_limit)],
)
async def get_pricing_plans(
    db: Session = Depends(get_db),
):
    """
    Get public pricing information for all subscription tiers.

    This endpoint is PUBLIC (no authentication required) and designed for:
    - Marketing website pricing pages
    - Signup flow tier selection
    - Mobile app pricing display
    - Customer self-service portals

    Returns pricing in both kobo (Nigerian smallest currency unit) and Naira,
    along with capabilities, features list, and savings calculations.

    Args:
        db: Database session

    Returns:
        List of all subscription tiers with pricing and features
    """
    service = PlatformSubscriptionService(db)

    try:
        plans = service.get_pricing_plans()
        return plans
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get pricing plans: {str(e)}",
        ) from e


@router.get(
    "/my-subscription",
    response_model=MySubscriptionResponse,
    dependencies=[Depends(subscription_read_rate_limit)],
)
async def get_my_subscription(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get current user's subscription details.

    Returns complete subscription information including:
    - Current tier and status
    - Billing cycle and dates (trial end, subscription start/end, next billing)
    - Capabilities (can upgrade, can cancel)
    - Days until expiry

    This endpoint is available to all authenticated users (not just tenant admins).

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        MySubscriptionResponse with subscription details

    Raises:
        HTTPException 404: Tenant not found
        HTTPException 500: Server error
    """
    subscription_service = SubscriptionService(db)

    try:
        subscription_data = subscription_service.get_my_subscription(current_user.tenant_id)
        return MySubscriptionResponse(**subscription_data)
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


@router.post(
    "/cancel",
    response_model=SubscriptionCancelResponse,
    dependencies=[Depends(subscription_rate_limit)],
)
async def cancel_my_subscription(
    cancel_request: SubscriptionCancelRequest,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Cancel current user's subscription.

    Only TENANT_ADMIN users can cancel subscriptions.

    Cancellation options:
    - **Immediate**: Cancel right away (may be eligible for prorated refund)
    - **End of billing period**: Cancel at the end of current billing cycle (no refund)

    After cancellation:
    - You'll have a 30-day grace period to reactivate before data deletion
    - Your data will be archived and accessible during grace period
    - After grace period, your data will be permanently deleted

    Cannot cancel:
    - Trial subscriptions (they expire automatically)
    - Already canceled subscriptions

    Args:
        cancel_request: Cancellation request with reason and immediate flag
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        SubscriptionCancelResponse with cancellation details

    Raises:
        HTTPException 400: Cannot cancel trial or already canceled
        HTTPException 403: User is not tenant admin
        HTTPException 404: Tenant not found
        HTTPException 500: Server error
    """
    subscription_service = SubscriptionService(db)

    try:
        cancellation_data = subscription_service.cancel_subscription(
            tenant_id=current_user.tenant_id,
            reason=cancel_request.reason,
            immediate=cancel_request.immediate,
            user_id=current_user.id,
            user_email=current_user.email,
        )
        return SubscriptionCancelResponse(**cancellation_data)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel subscription: {str(e)}",
        ) from e


@router.get(
    "/history",
    response_model=list[SubscriptionHistoryResponse],
    dependencies=[Depends(subscription_read_rate_limit)],
)
async def get_subscription_history(
    limit: int = 20,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get subscription change history for current tenant.

    Returns a chronological list of all subscription changes including:
    - Upgrades/downgrades
    - Tier changes
    - Cancellations
    - Trial extensions (by platform admin)
    - Manual overrides (by platform admin)

    This endpoint is available to all authenticated users (not just tenant admins).

    Args:
        limit: Maximum number of history records (default 20, max 100)
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of SubscriptionHistoryResponse records, most recent first

    Raises:
        HTTPException 400: Invalid limit parameter
        HTTPException 404: Tenant not found
        HTTPException 500: Server error
    """
    # Validate limit
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Limit must be between 1 and 100",
        )

    subscription_service = SubscriptionService(db)

    try:
        history = subscription_service.get_subscription_history(
            tenant_id=current_user.tenant_id,
            limit=limit,
        )
        return [SubscriptionHistoryResponse.model_validate(h) for h in history]
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get subscription history: {str(e)}",
        ) from e


@router.post(
    "/downgrade",
    response_model=SubscriptionDowngradeResponse,
    status_code=status.HTTP_200_OK,
    summary="Downgrade subscription to lower tier",
    description="Self-service downgrade with data validation. Validates tenant data fits within new tier limits before allowing downgrade.",
    dependencies=[Depends(subscription_rate_limit)],
)
async def downgrade_subscription(
    downgrade_request: SubscriptionDowngradeRequest,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Downgrade subscription to a lower tier.

    Validates that current tenant data (knowledge sources, storage, team members)
    fits within the new tier's limits before allowing the downgrade.

    **Immediate downgrade**: Takes effect immediately, new pricing on next billing cycle.

    **No refund**: Current billing cycle is not refunded (you paid for the full period).

    **Paystack update**: Automatically updates your Paystack subscription to ensure
    correct pricing on next auto-renewal.

    Only TENANT_ADMIN users can downgrade subscriptions.

    Args:
        downgrade_request: Downgrade request with tier and optional reason
        db: Database session
        current_user: Current authenticated tenant admin

    Returns:
        SubscriptionDowngradeResponse with new tier, billing info, and warnings

    Raises:
        HTTPException 400: Invalid tier, not a downgrade, or data exceeds limits
        HTTPException 402: Current subscription not eligible for downgrade
        HTTPException 403: User is not tenant admin
        HTTPException 500: Internal error during downgrade
    """
    from loguru import logger

    subscription_service = SubscriptionService(db)

    try:
        # Check if tenant has active paid subscription
        from app.db.models.tenant import Tenant

        tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        if tenant.subscription_tier == "trial":
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Cannot downgrade from trial. Please upgrade first.",
            )

        if tenant.subscription_status not in ["active"]:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Subscription {tenant.subscription_status}. Cannot downgrade inactive subscriptions.",
            )

        # Perform downgrade
        result = await subscription_service.downgrade_subscription(
            tenant_id=current_user.tenant_id,
            new_tier=downgrade_request.tier,
            reason=downgrade_request.reason,
            user_id=current_user.id,
            user_email=current_user.email,
        )

        return SubscriptionDowngradeResponse(**result)

    except ValueError as e:
        # Handle validation errors
        error_detail: str | dict[str, Any] = str(e)
        if isinstance(e.args[0], dict):
            error_detail = e.args[0]

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail) from e

    except Exception as e:
        logger.error(f"Error downgrading subscription: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to downgrade subscription: {str(e)}",
        ) from e
