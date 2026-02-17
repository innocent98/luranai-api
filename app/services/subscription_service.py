"""Manage tenant subscriptions and tier changes."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.plan_service import PlanService
from app.utils.datetime import ensure_utc_aware


class SubscriptionService:
    """Manage tenant subscriptions and tier changes."""

    def __init__(self, db: Session):
        """Initialize subscription service with database session."""
        self.db = db
        self.plan_service = PlanService(db)

    def upgrade_subscription(
        self, tenant_id: UUID, tier: str, billing_cycle: str = "monthly"
    ) -> Tenant:
        """
        Upgrade tenant to paid tier.

        Args:
            tenant_id: Tenant UUID
            tier: New tier (lite, professional, enterprise)
            billing_cycle: monthly or annual

        Returns:
            Updated tenant

        Raises:
            ValueError: If tenant not found or invalid tier
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        if not self.plan_service.validate_tier(tier):
            raise ValueError(f"Invalid tier: {tier}")

        # Update tenant subscription
        tenant.subscription_tier = tier
        tenant.subscription_status = "active"
        tenant.billing_cycle = billing_cycle
        tenant.subscription_starts_at = datetime.now(UTC)
        tenant.trial_ends_at = None  # Clear trial

        # Update capabilities based on tier
        self._update_capabilities_for_tier(tenant_id, tier)

        self.db.commit()
        self.db.refresh(tenant)

        return tenant

    def _update_capabilities_for_tier(self, tenant_id: UUID, tier: str):
        """
        Update tenant capabilities based on tier (respecting manual overrides).

        Manual capability overrides tracked in override_flags will be preserved
        during tier upgrades. This allows platform admins to grant custom limits
        that persist through subscription changes.
        """
        config = self.plan_service.get_plan_config(tier)

        capabilities = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .first()
        )

        if not capabilities:
            raise ValueError("Capabilities not found")

        # Get override flags (fields that should NOT be reset)
        override_flags = capabilities.override_flags or {}

        # Update capability fields, skipping manually overridden ones
        for key, value in config.items():
            if hasattr(capabilities, key):
                # Skip if manually overridden by platform admin
                if key not in override_flags:
                    setattr(capabilities, key, value)
                # else: keep current value (override persists)

        self.db.commit()

    def get_my_subscription(self, tenant_id: UUID) -> dict[str, Any]:
        """
        Get user's current subscription details.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Dict with subscription details including status, dates, and capabilities

        Raises:
            ValueError: If tenant not found
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        now = datetime.now(UTC)
        is_trial = tenant.subscription_tier == "trial"
        trial_ends_at = ensure_utc_aware(tenant.trial_ends_at)
        subscription_ends_at = ensure_utc_aware(tenant.subscription_ends_at)

        # Calculate days until expiry
        days_until_expiry = None
        if is_trial and trial_ends_at:
            days_until_expiry = (trial_ends_at - now).days
        elif subscription_ends_at:
            days_until_expiry = (subscription_ends_at - now).days

        # Calculate next billing date (for active subscriptions)
        next_billing_date = None
        if tenant.subscription_status == "active" and not is_trial:
            if tenant.subscription_starts_at and tenant.billing_cycle:
                if tenant.billing_cycle == "monthly":
                    next_billing_date = tenant.subscription_starts_at + timedelta(days=30)
                elif tenant.billing_cycle == "annual":
                    next_billing_date = tenant.subscription_starts_at + timedelta(days=365)

        # Determine if user can upgrade or cancel
        can_upgrade = is_trial or tenant.subscription_tier in ["lite", "professional"]
        can_cancel = (
            tenant.subscription_status == "active"
            and not is_trial
            and tenant.subscription_tier != "trial"
        )

        return {
            "tenant_id": tenant.id,
            "tenant_name": tenant.name,
            "subscription_tier": tenant.subscription_tier,
            "subscription_status": tenant.subscription_status,
            "billing_cycle": tenant.billing_cycle,
            "trial_ends_at": tenant.trial_ends_at,
            "subscription_starts_at": tenant.subscription_starts_at,
            "subscription_ends_at": tenant.subscription_ends_at,
            "next_billing_date": next_billing_date,
            "can_upgrade": can_upgrade,
            "can_cancel": can_cancel,
            "is_trial": is_trial,
            "days_until_expiry": days_until_expiry,
        }

    def cancel_subscription(
        self,
        tenant_id: UUID,
        reason: str | None = None,
        immediate: bool = False,
        user_id: UUID | None = None,
        user_email: str | None = None,
    ) -> dict[str, Any]:
        """
        Cancel user's subscription.

        Args:
            tenant_id: Tenant UUID
            reason: Reason for cancellation (optional)
            immediate: If True, cancel immediately. If False, cancel at end of billing period.
            user_id: User ID who initiated cancellation
            user_email: User email who initiated cancellation

        Returns:
            Dict with cancellation details

        Raises:
            ValueError: If tenant not found, already canceled, or on trial
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        if tenant.subscription_tier == "trial":
            raise ValueError("Cannot cancel trial subscription. Trial will expire automatically.")

        if tenant.subscription_status in ["canceled", "trial_expired"]:
            raise ValueError("Subscription is already canceled")

        # Store previous state
        previous_status = tenant.subscription_status
        previous_tier = tenant.subscription_tier

        now = datetime.now(UTC)

        if immediate:
            # Cancel immediately
            tenant.subscription_status = "canceled"
            tenant.subscription_ends_at = now
            message = "Subscription canceled immediately"
            refund_eligible = True  # May be eligible for prorated refund
            can_reactivate_until = now + timedelta(days=30)  # 30-day grace period
        else:
            # Cancel at end of billing period
            tenant.subscription_status = "active"  # Keep active until end

            # Calculate end date based on billing cycle
            if tenant.subscription_starts_at and tenant.billing_cycle:
                if tenant.billing_cycle == "monthly":
                    ends_at = tenant.subscription_starts_at + timedelta(days=30)
                elif tenant.billing_cycle == "annual":
                    ends_at = tenant.subscription_starts_at + timedelta(days=365)
                else:
                    ends_at = now + timedelta(days=30)  # Default 30 days

                tenant.subscription_ends_at = ends_at
                message = f"Subscription will be canceled on {ends_at.strftime('%Y-%m-%d')}"
                refund_eligible = False  # No refund if canceled at end of period
                can_reactivate_until = ends_at
            else:
                # No billing cycle info, cancel immediately
                tenant.subscription_ends_at = now
                tenant.subscription_status = "canceled"
                message = "Subscription canceled immediately"
                refund_eligible = False
                can_reactivate_until = now + timedelta(days=30)

        # Record cancellation in subscription history
        history = SubscriptionHistory(
            tenant_id=tenant_id,
            action="cancellation",
            previous_tier=previous_tier,
            new_tier=tenant.subscription_tier,
            previous_status=previous_status,
            new_status=tenant.subscription_status,
            changed_by_type="user",
            changed_by_id=user_id,
            changed_by_name=user_email or "Unknown User",
            reason=reason or "User requested cancellation",
            event_metadata={
                "immediate": immediate,
                "ends_at": (
                    tenant.subscription_ends_at.isoformat() if tenant.subscription_ends_at else None
                ),
            },
        )
        self.db.add(history)

        self.db.commit()
        self.db.refresh(tenant)

        return {
            "message": message,
            "status": tenant.subscription_status,
            "ends_at": tenant.subscription_ends_at,
            "refund_eligible": refund_eligible,
            "can_reactivate_until": can_reactivate_until,
        }

    def get_subscription_history(
        self,
        tenant_id: UUID,
        limit: int = 20,
    ) -> list[SubscriptionHistory]:
        """
        Get subscription change history for a tenant.

        Args:
            tenant_id: Tenant UUID
            limit: Maximum number of history records to return (default 20)

        Returns:
            List of SubscriptionHistory records, most recent first

        Raises:
            ValueError: If tenant not found
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        history = (
            self.db.query(SubscriptionHistory)
            .filter(SubscriptionHistory.tenant_id == tenant_id)
            .order_by(SubscriptionHistory.created_at.desc())
            .limit(limit)
            .all()
        )

        return history

    async def downgrade_subscription(
        self,
        tenant_id: UUID,
        new_tier: str,
        reason: str | None = None,
        user_id: UUID | None = None,
        user_email: str | None = None,
    ) -> dict[str, Any]:
        """
        Downgrade tenant to lower tier (immediate, no refund).

        This method:
        1. Validates the new tier exists and is lower than current
        2. Validates tenant data fits within new tier limits
        3. Updates tenant subscription immediately
        4. Updates Paystack subscription to new plan (critical for correct renewal pricing)
        5. Updates capabilities based on new tier
        6. Records downgrade in subscription history

        Args:
            tenant_id: Tenant UUID
            new_tier: Target tier (must be lower than current)
            reason: User-provided reason (optional)
            user_id: User initiating downgrade
            user_email: User email

        Returns:
            Dict with downgrade details including new tier, next billing info, warnings

        Raises:
            ValueError: If validation fails, invalid tier, or not a downgrade
        """
        from uuid import uuid4

        from loguru import logger

        from app.db.models.payment import PaymentTransaction
        from app.services.paystack_service import PaystackService
        from app.services.subscription_validation_service import SubscriptionValidationService

        # 1. Get current tenant state
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        current_tier = tenant.subscription_tier

        # 2. Validate new tier exists
        if not self.plan_service.validate_tier(new_tier):
            raise ValueError(f"Invalid tier: {new_tier}")

        # 3. Validate downgrade direction (not upgrade)
        tier_order = {"trial": 0, "lite": 1, "professional": 2, "growth": 3, "enterprise": 4}
        if tier_order.get(new_tier, -1) >= tier_order.get(current_tier, -1):
            raise ValueError("Use upgrade endpoint for tier increases")

        # 4. Validate data fits new tier limits
        validation_service = SubscriptionValidationService(self.db)
        validation_result = validation_service.validate_tier_change(tenant_id, new_tier)

        if not validation_result.valid:
            raise ValueError(
                {
                    "error": "data_exceeds_limits",
                    "violations": [v.dict() for v in validation_result.violations],
                    "message": "Your current usage exceeds the new tier's limits",
                }
            )

        # 5. Store previous state for audit
        previous_tier = tenant.subscription_tier
        previous_status = tenant.subscription_status

        # 6. Update tenant subscription (immediate downgrade)
        tenant.subscription_tier = new_tier
        # Keep status as "active" (still paid subscription)
        # Keep billing_cycle unchanged
        # Keep subscription_starts_at unchanged (for next billing calculation)

        # 7. Update Paystack subscription to new plan (CRITICAL for correct auto-renewal)
        try:
            # Get the most recent transaction with subscription_code
            transaction = (
                self.db.query(PaymentTransaction)
                .filter(
                    PaymentTransaction.tenant_id == tenant_id,
                    PaymentTransaction.subscription_code.isnot(None),
                    PaymentTransaction.status == "success",
                )
                .order_by(PaymentTransaction.created_at.desc())
                .first()
            )

            if transaction and transaction.subscription_code and transaction.authorization_code:
                # Create new subscription with new plan (replaces old one)
                paystack_service = PaystackService(self.db)
                if not tenant.billing_cycle:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Missing billing cycle for tenant",
                    )
                billing_cycle = str(tenant.billing_cycle)
                new_plan_code = paystack_service.get_plan_code(new_tier, billing_cycle)

                # Create new subscription with new plan
                if not transaction.customer_code:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Missing customer code for subscription",
                    )
                new_subscription = await paystack_service.create_subscription(
                    customer_code=transaction.customer_code,
                    plan_code=new_plan_code,
                    authorization_code=transaction.authorization_code,
                )

                # Create new transaction record for the downgrade
                new_amount = self.plan_service.get_pricing(new_tier, billing_cycle)
                new_transaction = PaymentTransaction(
                    tenant_id=tenant_id,
                    reference=f"luran_downgrade_{tenant_id}_{uuid4().hex[:8]}",
                    amount=new_amount,
                    currency="NGN",
                    status="success",  # Downgrade is immediate, no payment needed
                    tier=new_tier,
                    billing_cycle=tenant.billing_cycle,
                    customer_email=transaction.customer_email,
                    customer_code=transaction.customer_code,
                    authorization_code=transaction.authorization_code,
                    subscription_code=new_subscription["subscription_code"],
                    paystack_data=new_subscription,
                )
                self.db.add(new_transaction)

                logger.info(
                    f"Updated Paystack subscription: {new_subscription['subscription_code']} "
                    f"(plan: {new_plan_code})"
                )

        except Exception as e:
            logger.error(f"Failed to update Paystack subscription: {e}")
            # Don't fail the whole downgrade - tier is updated in our DB
            # Platform admin can manually fix Paystack subscription if needed
            # But log it clearly for monitoring
            validation_result.warnings.append(
                "Downgrade completed, but billing system update may require manual adjustment"
            )

        # 8. Update capabilities based on new tier
        self._update_capabilities_for_tier(tenant_id, new_tier)

        # 9. Record in subscription history
        history = SubscriptionHistory(
            tenant_id=tenant_id,
            action="downgrade",
            previous_tier=previous_tier,
            new_tier=new_tier,
            previous_status=previous_status,
            new_status=tenant.subscription_status,
            changed_by_type="user",
            changed_by_id=user_id,
            changed_by_name=user_email or "Unknown User",
            reason=reason or "User requested downgrade",
            event_metadata={
                "billing_cycle": tenant.billing_cycle,
                "validation_warnings": validation_result.warnings,
                "immediate": True,
                "refund_issued": False,
            },
        )
        self.db.add(history)

        self.db.commit()
        self.db.refresh(tenant)

        # 10. Calculate next billing info
        next_billing_date = None
        if tenant.subscription_starts_at and tenant.billing_cycle:
            if tenant.billing_cycle == "monthly":
                next_billing_date = tenant.subscription_starts_at + timedelta(days=30)
            elif tenant.billing_cycle == "annual":
                next_billing_date = tenant.subscription_starts_at + timedelta(days=365)

        # 11. Get new tier pricing
        new_pricing = self.plan_service.get_plan(new_tier)

        return {
            "success": True,
            "message": f"Successfully downgraded to {new_tier}",
            "previous_tier": previous_tier,
            "new_tier": new_tier,
            "next_billing_date": next_billing_date,
            "next_billing_amount_kobo": (
                new_pricing.monthly_price_kobo
                if tenant.billing_cycle == "monthly"
                else new_pricing.annual_price_kobo
            ),
            "warnings": validation_result.warnings,
        }
