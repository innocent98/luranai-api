"""Platform admin subscription management service."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.db.models.email_notification import EmailNotification
from app.db.models.payment import PaymentTransaction
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.schemas.platform_subscription import (
    PaymentTransactionResponse,
    PricingPlanResponse,
    SubscriptionHistoryResponse,
    SubscriptionOverrideResponse,
    TrialReminderHistoryResponse,
    TrialReminderNotificationResponse,
    TenantSubscriptionDetailResponse,
    TrialExtensionResponse,
)
from app.services.plan_service import PlanService
from app.services.subscription_service import SubscriptionService


class PlatformSubscriptionService:
    """Platform admin operations for subscription management."""

    def __init__(self, db: Session):
        """Initialize platform subscription service."""
        self.db = db
        self.subscription_service = SubscriptionService(db)
        self.plan_service = PlanService(db)

    def override_tenant_subscription(
        self,
        tenant_id: UUID,
        tier: str,
        billing_cycle: str | None,
        reason: str,
        admin_id: UUID,
        admin_name: str,
        apply_tier_capabilities: bool = True,
    ) -> SubscriptionOverrideResponse:
        """
        Manually override a tenant's subscription (platform admin only).

        Args:
            tenant_id: Tenant UUID
            tier: New tier to assign
            billing_cycle: Billing cycle (null for trial)
            reason: Reason for override (audit trail)
            admin_id: Platform admin ID making the change
            admin_name: Platform admin name
            apply_tier_capabilities: Reset capabilities to tier defaults

        Returns:
            SubscriptionOverrideResponse

        Raises:
            ValueError: If tenant not found or invalid tier
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        if not self.plan_service.validate_tier(tier):
            raise ValueError(f"Invalid tier: {tier}")

        # Validate billing cycle
        if tier != "trial" and not billing_cycle:
            raise ValueError("Billing cycle required for paid tiers")
        if tier == "trial":
            billing_cycle = None

        # Store previous state
        previous_tier = tenant.subscription_tier
        previous_status = tenant.subscription_status

        # Update tenant subscription
        tenant.subscription_tier = tier
        tenant.subscription_status = "active" if tier != "trial" else "trialing"
        tenant.billing_cycle = billing_cycle

        if tier != "trial":
            tenant.subscription_starts_at = datetime.now(UTC)
            tenant.trial_ends_at = None
        else:
            # If moving back to trial, set trial end date
            if not tenant.trial_ends_at or tenant.trial_ends_at < datetime.now(UTC):
                tenant.trial_ends_at = datetime.now(UTC) + timedelta(days=14)

        # Update capabilities
        if apply_tier_capabilities:
            self._update_capabilities_with_overrides(tenant_id, tier, respect_overrides=False)
        else:
            self._update_capabilities_with_overrides(tenant_id, tier, respect_overrides=True)

        # Record history
        history = SubscriptionHistory(
            tenant_id=tenant_id,
            action="override",
            previous_tier=previous_tier,
            new_tier=tier,
            previous_status=previous_status,
            new_status=tenant.subscription_status,
            changed_by_type="platform_admin",
            changed_by_id=admin_id,
            changed_by_name=admin_name,
            reason=reason,
            event_metadata={
                "billing_cycle": billing_cycle,
                "capabilities_applied": apply_tier_capabilities,
            },
        )
        self.db.add(history)

        self.db.commit()

        return SubscriptionOverrideResponse(
            success=True,
            message=f"Successfully overridden subscription to {tier}",
            tenant_id=tenant_id,
            previous_tier=previous_tier,
            new_tier=tier,
            capabilities_applied=apply_tier_capabilities,
        )

    def extend_trial(
        self,
        tenant_id: UUID,
        days: int,
        reason: str,
        admin_id: UUID,
        admin_name: str,
    ) -> TrialExtensionResponse:
        """
        Extend trial period for a tenant.

        Args:
            tenant_id: Tenant UUID
            days: Number of days to extend
            reason: Reason for extension (audit trail)
            admin_id: Platform admin ID making the change
            admin_name: Platform admin name

        Returns:
            TrialExtensionResponse

        Raises:
            ValueError: If tenant not found or not on trial tier
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        if tenant.subscription_tier != "trial":
            raise ValueError("Can only extend trial tier subscriptions")

        # Calculate new trial end date
        if not tenant.trial_ends_at:
            # If no trial end date, start from now
            previous_end = datetime.now(UTC)
        else:
            previous_end = tenant.trial_ends_at

        # If trial already expired, extend from now
        if previous_end < datetime.now(UTC):
            new_end = datetime.now(UTC) + timedelta(days=days)
            # Reactivate if expired
            if tenant.subscription_status == "trial_expired":
                tenant.subscription_status = "trialing"
        else:
            # Extend from current end date
            new_end = previous_end + timedelta(days=days)

        tenant.trial_ends_at = new_end

        # Record history
        history = SubscriptionHistory(
            tenant_id=tenant_id,
            action="extend_trial",
            previous_tier="trial",
            new_tier="trial",
            previous_status=tenant.subscription_status,
            new_status="trialing",
            changed_by_type="platform_admin",
            changed_by_id=admin_id,
            changed_by_name=admin_name,
            reason=reason,
            event_metadata={
                "days_extended": days,
                "previous_end": previous_end.isoformat(),
                "new_end": new_end.isoformat(),
            },
        )
        self.db.add(history)

        self.db.commit()

        return TrialExtensionResponse(
            success=True,
            message=f"Trial extended by {days} days",
            tenant_id=tenant_id,
            previous_trial_end=previous_end,
            new_trial_end=new_end,
            days_extended=days,
        )

    def get_subscription_details(
        self,
        tenant_id: UUID,
    ) -> TenantSubscriptionDetailResponse:
        """
        Get complete subscription details for a tenant (platform admin view).

        Args:
            tenant_id: Tenant UUID

        Returns:
            TenantSubscriptionDetailResponse with full details

        Raises:
            ValueError: If tenant not found
        """
        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            raise ValueError("Tenant not found")

        # Get capabilities and override flags
        capabilities = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .first()
        )

        override_flags = capabilities.override_flags if capabilities else {}
        has_overrides = bool(override_flags)
        override_fields = list(override_flags.keys()) if override_flags else []

        # Get recent history (last 10 changes)
        history = (
            self.db.query(SubscriptionHistory)
            .filter(SubscriptionHistory.tenant_id == tenant_id)
            .order_by(SubscriptionHistory.created_at.desc())
            .limit(10)
            .all()
        )

        history_responses = [SubscriptionHistoryResponse.model_validate(h) for h in history]

        # Get payment stats
        payment_stats = (
            self.db.query(
                func.count(PaymentTransaction.id).label("total_payments"),
                func.coalesce(func.sum(PaymentTransaction.amount), 0).label("total_revenue_kobo"),
            )
            .filter(
                and_(
                    PaymentTransaction.tenant_id == tenant_id,
                    PaymentTransaction.status == "success",
                )
            )
            .first()
        )

        total_payments = payment_stats.total_payments if payment_stats else 0
        total_revenue_kobo = payment_stats.total_revenue_kobo if payment_stats else 0
        total_revenue_naira = total_revenue_kobo / 100

        return TenantSubscriptionDetailResponse(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            subscription_tier=tenant.subscription_tier,
            subscription_status=tenant.subscription_status,
            billing_cycle=tenant.billing_cycle,
            trial_ends_at=tenant.trial_ends_at,
            subscription_starts_at=tenant.subscription_starts_at,
            has_overrides=has_overrides,
            override_fields=override_fields,
            recent_changes=history_responses,
            total_payments=total_payments,
            total_revenue_naira=total_revenue_naira,
        )

    def get_tenants_by_subscription(
        self,
        subscription_status: str | None = None,
        subscription_tier: str | None = None,
        trial_expiring_days: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Tenant]:
        """
        Filter tenants by subscription criteria.

        Args:
            subscription_status: Filter by status (trial, active, trial_expired, etc.)
            subscription_tier: Filter by tier (trial, lite, professional, enterprise)
            trial_expiring_days: Find trials expiring within N days
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of tenants matching criteria
        """
        query = self.db.query(Tenant)

        if subscription_status:
            query = query.filter(Tenant.subscription_status == subscription_status)

        if subscription_tier:
            query = query.filter(Tenant.subscription_tier == subscription_tier)

        if trial_expiring_days is not None:
            # Find trials expiring within N days
            now = datetime.now(UTC)
            expiry_threshold = now + timedelta(days=trial_expiring_days)
            query = query.filter(
                and_(
                    Tenant.subscription_tier == "trial",
                    Tenant.trial_ends_at.isnot(None),
                    Tenant.trial_ends_at <= expiry_threshold,
                    Tenant.trial_ends_at > now,  # Not already expired
                )
            )

        query = query.limit(limit).offset(offset)
        return query.all()

    def get_payment_transactions(
        self,
        tenant_id: UUID | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PaymentTransactionResponse]:
        """
        Get payment transactions with filters (platform admin view).

        Args:
            tenant_id: Filter by tenant
            status: Filter by payment status
            limit: Maximum results
            offset: Pagination offset

        Returns:
            List of payment transactions with tenant names
        """
        query = self.db.query(PaymentTransaction, Tenant.name.label("tenant_name")).join(
            Tenant, PaymentTransaction.tenant_id == Tenant.id
        )

        if tenant_id:
            query = query.filter(PaymentTransaction.tenant_id == tenant_id)

        if status:
            query = query.filter(PaymentTransaction.status == status)

        query = query.order_by(PaymentTransaction.created_at.desc())
        query = query.limit(limit).offset(offset)

        results = query.all()

        return [
            PaymentTransactionResponse(
                id=payment.id,
                tenant_id=payment.tenant_id,
                tenant_name=tenant_name,
                reference=payment.reference,
                amount=payment.amount,
                currency=payment.currency,
                status=payment.status,
                tier=payment.tier,
                billing_cycle=payment.billing_cycle,
                customer_email=payment.customer_email,
                paid_at=payment.paid_at,
                created_at=payment.created_at,
            )
            for payment, tenant_name in results
        ]

    def get_trial_reminder_history(
        self,
        tenant_id: UUID | None = None,
        days_remaining: int | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> TrialReminderHistoryResponse:
        """
        Get paginated trial reminder email history (platform admin view).

        Args:
            tenant_id: Optional tenant filter
            days_remaining: Optional reminder window filter (e.g., 14, 7, 3, 1, 0)
            status: Optional status filter (sent, failed, skipped)
            limit: Page size
            offset: Pagination offset
        """
        query = self.db.query(EmailNotification, Tenant.name.label("tenant_name")).join(
            Tenant, EmailNotification.tenant_id == Tenant.id
        )

        query = query.filter(EmailNotification.notification_type.like("trial_expiring%"))

        if tenant_id:
            query = query.filter(EmailNotification.tenant_id == tenant_id)

        if status:
            query = query.filter(EmailNotification.status == status)

        if days_remaining is not None:
            target_type = (
                "trial_expiring_today"
                if days_remaining == 0
                else f"trial_expiring_{days_remaining}d"
            )
            query = query.filter(EmailNotification.notification_type == target_type)

        total = query.count()

        rows = (
            query.order_by(EmailNotification.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            TrialReminderNotificationResponse(
                id=notification.id,
                tenant_id=notification.tenant_id,
                tenant_name=tenant_name,
                notification_type=notification.notification_type,
                days_remaining=self._extract_days_remaining(notification.notification_type),
                recipient_email=notification.recipient_email,
                status=notification.status,
                subject=notification.subject,
                sent_at=notification.sent_at,
                created_at=notification.created_at,
            )
            for notification, tenant_name in rows
        ]

        return TrialReminderHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_renewal_reminder_history(
        self,
        tenant_id: UUID | None = None,
        days_remaining: int | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> TrialReminderHistoryResponse:
        """
        Get paginated subscription renewal reminder history (platform admin view).

        Args:
            tenant_id: Optional tenant filter
            days_remaining: Optional reminder window filter (e.g., 7, 3, 1)
            status: Optional status filter (sent, failed, skipped)
            limit: Page size
            offset: Pagination offset
        """
        query = self.db.query(EmailNotification, Tenant.name.label("tenant_name")).join(
            Tenant, EmailNotification.tenant_id == Tenant.id
        )

        query = query.filter(EmailNotification.notification_type.like("subscription_renewal_%"))

        if tenant_id:
            query = query.filter(EmailNotification.tenant_id == tenant_id)

        if status:
            query = query.filter(EmailNotification.status == status)

        if days_remaining is not None:
            target_type = f"subscription_renewal_{days_remaining}d"
            query = query.filter(EmailNotification.notification_type == target_type)

        total = query.count()

        rows = (
            query.order_by(EmailNotification.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        items = [
            TrialReminderNotificationResponse(
                id=notification.id,
                tenant_id=notification.tenant_id,
                tenant_name=tenant_name,
                notification_type=notification.notification_type,
                days_remaining=self._extract_days_remaining(notification.notification_type),
                recipient_email=notification.recipient_email,
                status=notification.status,
                subject=notification.subject,
                sent_at=notification.sent_at,
                created_at=notification.created_at,
            )
            for notification, tenant_name in rows
        ]

        return TrialReminderHistoryResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_pricing_plans(self) -> list[PricingPlanResponse]:
        """
        Get public pricing information for all tiers.

        Note: Enterprise tier is excluded from public plans (contact-only).

        Returns:
            List of pricing plans with capabilities and features
        """
        plans = []

        # Get all active plans from database
        db_plans = self.plan_service.get_all_plans(active_only=True)

        for plan in db_plans:
            # Exclude enterprise from public pricing (contact-only)
            if plan.tier == "enterprise":
                continue

            monthly_kobo = plan.monthly_price_kobo
            annual_kobo = plan.annual_price_kobo
            monthly_naira = monthly_kobo / 100
            annual_naira = annual_kobo / 100

            # Calculate savings
            annual_equivalent = monthly_naira * 12
            annual_savings = annual_equivalent - annual_naira
            annual_discount_pct = (
                (annual_savings / annual_equivalent * 100) if annual_equivalent > 0 else 0
            )

            pricing_plan = PricingPlanResponse(
                tier=str(plan.tier),
                display_name=str(plan.display_name),
                description=str(plan.description),
                monthly_price_kobo=int(monthly_kobo),
                annual_price_kobo=int(annual_kobo),
                monthly_price_naira=float(monthly_naira),
                annual_price_naira=float(annual_naira),
                annual_savings_naira=float(annual_savings),
                annual_discount_percentage=round(annual_discount_pct, 1),
                capabilities=plan.get_capabilities(),
                features=list(plan.features or []),
                is_popular=str(plan.tier) == "professional",  # Mark professional as popular
            )
            plans.append(pricing_plan)

        return plans

    @staticmethod
    def _extract_days_remaining(notification_type: str) -> int | None:
        """Parse day window from notification type."""
        if notification_type == "trial_expiring_today":
            return 0
        suffix = "d"
        if notification_type.endswith(suffix):
            for prefix in ("trial_expiring_", "subscription_renewal_"):
                if notification_type.startswith(prefix):
                    days_part = notification_type[len(prefix) : -len(suffix)]
                    if days_part.isdigit():
                        return int(days_part)
        return None

    def _update_capabilities_with_overrides(
        self,
        tenant_id: UUID,
        tier: str,
        respect_overrides: bool = True,
    ):
        """
        Update tenant capabilities based on tier, respecting manual overrides.

        Args:
            tenant_id: Tenant UUID
            tier: New tier
            respect_overrides: If True, preserve fields in override_flags
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
        override_flags = capabilities.override_flags or {} if respect_overrides else {}

        # Update capability fields
        for key, value in config.items():
            if hasattr(capabilities, key):
                # Skip if manually overridden and respect_overrides is True
                if key not in override_flags:
                    setattr(capabilities, key, value)
                # else: keep current value (override persists)

        self.db.commit()
