"""Email notification service for automated tenant notifications."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.conversation import ConversationSession, Ticket
from app.db.models.email_notification import EmailNotification
from app.db.models.knowledge import KnowledgeSource
from app.db.models.payment import PaymentTransaction
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.services.email_service import email_service


class EmailNotificationService:
    """Service for managing automated email notifications with duplicate prevention."""

    def __init__(self, db: AsyncSession | Session):
        """
        Initialize email notification service.

        Args:
            db: Database session
        """
        self.db = db
        self.email_service = email_service
        self._lagos_tz = ZoneInfo("Africa/Lagos")

    async def _execute(self, query):
        if isinstance(self.db, AsyncSession):
            return await self.db.execute(query)
        return self.db.execute(query)

    async def _commit(self) -> None:
        if isinstance(self.db, AsyncSession):
            await self.db.commit()
        else:
            self.db.commit()

    async def _refresh(self, instance: Any) -> None:
        if isinstance(self.db, AsyncSession):
            await self.db.refresh(instance)
        else:
            self.db.refresh(instance)

    async def _check_if_already_sent(
        self,
        tenant_id: uuid.UUID,
        notification_type: str,
        reference_id: uuid.UUID | None = None,
        hours: int = 24,
    ) -> bool:
        """
        Check if a notification was already sent recently.

        Args:
            tenant_id: Tenant ID
            notification_type: Type of notification
            reference_id: Optional reference ID (payment, subscription, etc.)
            hours: Check within last N hours (default 24)

        Returns:
            True if already sent, False otherwise
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)

        query = select(EmailNotification).where(
            and_(
                EmailNotification.tenant_id == tenant_id,
                EmailNotification.notification_type == notification_type,
                EmailNotification.status == "sent",
                EmailNotification.created_at >= cutoff_time,
            )
        )

        if reference_id:
            query = query.where(EmailNotification.reference_id == reference_id)

        result = await self._execute(query)
        existing = result.scalars().first()

        return existing is not None

    async def _record_notification(
        self,
        tenant_id: uuid.UUID,
        notification_type: str,
        recipient_email: str,
        subject: str,
        template_data: dict[str, Any],
        reference_id: uuid.UUID | None = None,
        reference_type: str | None = None,
        status: str = "pending",
        error_message: str | None = None,
    ) -> EmailNotification:
        """
        Record an email notification in the database.

        Args:
            tenant_id: Tenant ID
            notification_type: Type of notification
            recipient_email: Recipient email address
            subject: Email subject
            template_data: Template variables used
            reference_id: Optional reference ID
            reference_type: Optional reference type
            status: Status (pending, sent, failed, skipped)
            error_message: Error message if failed

        Returns:
            Created EmailNotification record
        """
        notification = EmailNotification(
            tenant_id=tenant_id,
            notification_type=notification_type,
            recipient_email=recipient_email,
            subject=subject,
            reference_id=reference_id,
            reference_type=reference_type,
            status=status,
            template_data=template_data,
            sent_at=datetime.utcnow() if status == "sent" else None,
            error_message=error_message,
        )

        self.db.add(notification)
        await self._commit()
        await self._refresh(notification)

        return notification

    async def _get_tenant_admin(self, tenant_id: uuid.UUID) -> User | None:
        """
        Get the tenant admin user.

        Args:
            tenant_id: Tenant ID

        Returns:
            Tenant admin User or None
        """
        query = select(User).where(
            and_(
                User.tenant_id == tenant_id,
                User.role == UserRole.TENANT_ADMIN,
                User.is_active.is_(True),
            )
        )

        result = await self._execute(query)
        return cast(User | None, result.scalars().first())

    async def _get_plan_price_display(self, tier: str) -> str:
        """Get monthly price display for a subscription tier."""
        query = select(SubscriptionPlan).where(
            and_(
                SubscriptionPlan.tier == tier,
                SubscriptionPlan.is_active.is_(True),
            )
        )
        result = await self._execute(query)
        plan = result.scalars().first()
        if not plan:
            return "N/A"
        return f"₦{int(plan.monthly_price_kobo / 100):,}"

    async def _get_plan_renewal_amount_display(self, tier: str, billing_cycle: str | None) -> str:
        """Get renewal amount display from current subscription plan pricing."""
        query = select(SubscriptionPlan).where(
            and_(
                SubscriptionPlan.tier == tier,
                SubscriptionPlan.is_active.is_(True),
            )
        )
        result = await self._execute(query)
        plan = result.scalars().first()
        if not plan:
            return "N/A"

        cycle = (billing_cycle or "monthly").lower()
        amount_kobo = plan.annual_price_kobo if cycle == "annual" else plan.monthly_price_kobo
        if not amount_kobo:
            return "Contact Sales"
        return f"₦{int(amount_kobo / 100):,}"

    async def _get_monthly_usage_summary(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """Get current-month usage summary for reminder emails."""
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        conversations_query = select(func.count(ConversationSession.id)).where(
            and_(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.created_at >= month_start,
            )
        )
        tickets_query = select(func.count(Ticket.id)).where(
            and_(
                Ticket.tenant_id == tenant_id,
                Ticket.created_at >= month_start,
            )
        )
        knowledge_sources_query = select(func.count(KnowledgeSource.id)).where(
            and_(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.created_at >= month_start,
            )
        )

        conversations_result = await self._execute(conversations_query)
        tickets_result = await self._execute(tickets_query)
        knowledge_sources_result = await self._execute(knowledge_sources_query)

        return {
            "conversations": int(conversations_result.scalar() or 0),
            "tickets": int(tickets_result.scalar() or 0),
            "knowledge_sources": int(knowledge_sources_result.scalar() or 0),
        }

    async def _get_latest_successful_payment(
        self, tenant_id: uuid.UUID
    ) -> PaymentTransaction | None:
        """Get latest successful payment transaction for tenant."""
        query = (
            select(PaymentTransaction)
            .where(
                and_(
                    PaymentTransaction.tenant_id == tenant_id,
                    PaymentTransaction.status == "success",
                )
            )
            .order_by(PaymentTransaction.paid_at.desc(), PaymentTransaction.created_at.desc())
            .limit(1)
        )
        result = await self._execute(query)
        return cast(PaymentTransaction | None, result.scalars().first())

    async def estimate_next_renewal_at(self, tenant_id: uuid.UUID) -> datetime | None:
        """Estimate next auto-renewal timestamp from latest successful payment."""
        tenant_query = select(Tenant).where(Tenant.id == tenant_id)
        tenant_result = await self._execute(tenant_query)
        tenant = tenant_result.scalars().first()
        if not tenant or tenant.subscription_status != "active" or tenant.subscription_tier == "trial":
            return None

        latest_payment = await self._get_latest_successful_payment(tenant_id)
        if not latest_payment:
            return None

        base_time = latest_payment.paid_at or latest_payment.created_at
        if not base_time:
            return None

        billing_cycle = tenant.billing_cycle or latest_payment.billing_cycle
        if billing_cycle == "annual":
            return base_time + timedelta(days=365)
        return base_time + timedelta(days=30)

    async def send_trial_expiration_reminder(
        self, tenant_id: uuid.UUID, days_remaining: int = 3
    ) -> bool:
        """
        Send trial expiration reminder email.

        Args:
            tenant_id: Tenant ID
            days_remaining: Days remaining in trial

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            notification_type = (
                "trial_expiring_today" if days_remaining <= 0 else f"trial_expiring_{days_remaining}d"
            )

            # Check for duplicates for this specific reminder window.
            if await self._check_if_already_sent(tenant_id, notification_type, hours=24):
                logger.info(f"Trial expiration email already sent for tenant {tenant_id}")
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type=notification_type,
                    recipient_email="",
                    subject="",
                    template_data={},
                    status="skipped",
                )
                return True

            # Get tenant and admin
            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = await self._execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant or not tenant.trial_ends_at:
                logger.warning(f"Tenant {tenant_id} not found or no trial end date")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            # Prepare template data
            lite_monthly = await self._get_plan_price_display("lite")
            professional_monthly = await self._get_plan_price_display("professional")
            usage_summary = await self._get_monthly_usage_summary(tenant_id)
            days_label = "today" if days_remaining <= 0 else str(days_remaining)

            template_data = {
                "admin_name": admin.full_name,
                "company_name": tenant.display_name,
                "days_remaining": days_remaining,
                "days_label": days_label,
                "expiry_date": tenant.trial_ends_at.strftime("%B %d, %Y"),
                "usage_summary": usage_summary,
                "pricing": {
                    "lite_monthly": lite_monthly,
                    "professional_monthly": professional_monthly,
                },
                "app_url": settings.FRONTEND_URL,
                "doc_url": settings.FRONTEND_DOC_URL,
                "current_year": datetime.now().year,
            }

            if days_remaining <= 0:
                subject = f"Your Luran free trial expires today - {tenant.display_name}"
            elif days_remaining == 1:
                subject = f"Your Luran free trial expires in 1 day - {tenant.display_name}"
            else:
                subject = (
                    f"Your Luran free trial expires in {days_remaining} days - {tenant.display_name}"
                )

            # Try to send email
            try:
                # Render template
                template = self.email_service.jinja_env.get_template("trial_expiring.html")
                html_content = template.render(**template_data)

                # Send email
                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )

                # Record notification
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )

                return success

            except Exception as e:
                logger.error(f"Failed to send trial expiration email: {e}")
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    status="failed",
                    error_message=str(e),
                )
                return False

        except Exception as e:
            logger.error(f"Error in send_trial_expiration_reminder: {e}")
            return False

    async def send_subscription_confirmation(
        self, tenant_id: uuid.UUID, payment_id: uuid.UUID
    ) -> bool:
        """
        Send subscription confirmation email after successful payment.

        Args:
            tenant_id: Tenant ID
            payment_id: Payment transaction ID

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Check for duplicates
            if await self._check_if_already_sent(
                tenant_id, "subscription_confirmed", reference_id=payment_id, hours=48
            ):
                logger.info(
                    f"Subscription confirmation already sent for tenant {tenant_id}, payment {payment_id}"
                )
                return True

            # Get tenant, admin, and payment
            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = await self._execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            payment_query = select(PaymentTransaction).where(PaymentTransaction.id == payment_id)
            payment_result = await self._execute(payment_query)
            payment = payment_result.scalars().first()

            if not payment or payment.status != "success":
                logger.warning(f"Payment {payment_id} not found or not successful")
                return False

            # Calculate next billing date
            next_billing_date = datetime.utcnow()
            if tenant.billing_cycle == "monthly":
                next_billing_date += timedelta(days=30)
            elif tenant.billing_cycle == "annual":
                next_billing_date += timedelta(days=365)

            # Prepare template data
            template_data = {
                "admin_name": admin.full_name,
                "company_name": tenant.display_name,
                "plan_name": tenant.subscription_tier.replace("_", " ").title(),
                "currency": payment.currency,
                "amount": f"{payment.amount:,.2f}",
                "billing_cycle": (
                    tenant.billing_cycle.title() if tenant.billing_cycle else "Monthly"
                ),
                "next_billing_date": next_billing_date.strftime("%B %d, %Y"),
                "payment_reference": str(payment.reference),
                "features": {
                    "conversations": (
                        "2,000" if tenant.subscription_tier == "professional" else "500"
                    ),
                    "team_members": "10" if tenant.subscription_tier == "professional" else "2",
                    "knowledge_sources": (
                        "Unlimited" if tenant.subscription_tier == "professional" else "5"
                    ),
                    "custom_branding": tenant.subscription_tier == "professional",
                    "priority_support": tenant.subscription_tier in ["professional", "enterprise"],
                    "advanced_analytics": tenant.subscription_tier
                    in ["professional", "enterprise"],
                    "api_access": tenant.subscription_tier == "enterprise",
                },
                "app_url": settings.FRONTEND_URL,
                "doc_url": settings.FRONTEND_DOC_URL,
                "current_year": datetime.now().year,
            }

            subject = f"Subscription Confirmed - Welcome to Luran {template_data['plan_name']}!"

            # Try to send email
            try:
                template = self.email_service.jinja_env.get_template("subscription_confirmed.html")
                html_content = template.render(**template_data)

                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )

                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type="subscription_confirmed",
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    reference_id=payment_id,
                    reference_type="payment",
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )

                return success

            except Exception as e:
                logger.error(f"Failed to send subscription confirmation email: {e}")
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type="subscription_confirmed",
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    reference_id=payment_id,
                    reference_type="payment",
                    status="failed",
                    error_message=str(e),
                )
                return False

        except Exception as e:
            logger.error(f"Error in send_subscription_confirmation: {e}")
            return False

    async def send_payment_failure_notification(
        self,
        tenant_id: uuid.UUID,
        payment_id: uuid.UUID,
        grace_days: int = 7,
        failure_reason: str | None = None,
    ) -> bool:
        """
        Send payment failure notification email.

        Args:
            tenant_id: Tenant ID
            payment_id: Failed payment transaction ID
            grace_days: Days before service suspension
            failure_reason: Optional reason for payment failure

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Check for duplicates
            if await self._check_if_already_sent(
                tenant_id, "payment_failed", reference_id=payment_id, hours=24
            ):
                logger.info(
                    f"Payment failure email already sent for tenant {tenant_id}, payment {payment_id}"
                )
                return True

            # Get tenant, admin, and payment
            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = await self._execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            payment_query = select(PaymentTransaction).where(PaymentTransaction.id == payment_id)
            payment_result = await self._execute(payment_query)
            payment = payment_result.scalars().first()

            if not payment:
                logger.warning(f"Payment {payment_id} not found")
                return False

            # Calculate suspension date
            suspension_date = datetime.utcnow() + timedelta(days=grace_days)

            # Prepare template data
            template_data = {
                "admin_name": admin.full_name,
                "company_name": tenant.display_name,
                "plan_name": tenant.subscription_tier.replace("_", " ").title(),
                "currency": payment.currency,
                "amount": f"{payment.amount:,.2f}",
                "payment_date": payment.created_at.strftime("%B %d, %Y at %I:%M %p"),
                "failure_reason": failure_reason or "Payment declined by your bank",
                "grace_days": grace_days,
                "suspension_date": suspension_date.strftime("%B %d, %Y"),
                "data_retention_days": 30,
                "app_url": settings.FRONTEND_URL,
                "doc_url": settings.FRONTEND_DOC_URL,
                "current_year": datetime.now().year,
            }

            subject = f"Payment Failed - Action Required for {tenant.display_name}"

            # Try to send email
            try:
                template = self.email_service.jinja_env.get_template("payment_failed.html")
                html_content = template.render(**template_data)

                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )

                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type="payment_failed",
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    reference_id=payment_id,
                    reference_type="payment",
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )

                return success

            except Exception as e:
                logger.error(f"Failed to send payment failure email: {e}")
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type="payment_failed",
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    reference_id=payment_id,
                    reference_type="payment",
                    status="failed",
                    error_message=str(e),
                )
                return False

        except Exception as e:
            logger.error(f"Error in send_payment_failure_notification: {e}")
            return False

    async def send_subscription_renewal_reminder(
        self,
        tenant_id: uuid.UUID,
        days_remaining: int,
        next_renewal_at: datetime | None = None,
    ) -> bool:
        """
        Send pre-renewal reminder for active paid subscriptions.

        Args:
            tenant_id: Tenant ID
            days_remaining: Days until renewal (e.g., 7, 3, 1)
            next_renewal_at: Optional precomputed renewal timestamp
        """
        try:
            notification_type = f"subscription_renewal_{days_remaining}d"
            if await self._check_if_already_sent(tenant_id, notification_type, hours=24):
                logger.info(f"Renewal reminder already sent for tenant {tenant_id}")
                return True

            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = await self._execute(tenant_query)
            tenant = tenant_result.scalars().first()
            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False
            if tenant.subscription_status != "active" or tenant.subscription_tier == "trial":
                logger.info(f"Skipping renewal reminder: tenant {tenant_id} is not on active paid plan")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            renewal_at = next_renewal_at or await self.estimate_next_renewal_at(tenant_id)
            if not renewal_at:
                logger.warning(f"No renewal timestamp available for tenant {tenant_id}")
                return False

            latest_payment = await self._get_latest_successful_payment(tenant_id)
            amount_naira = await self._get_plan_renewal_amount_display(
                tier=tenant.subscription_tier,
                billing_cycle=tenant.billing_cycle,
            )

            renewal_local = renewal_at.astimezone(self._lagos_tz)
            renewal_date_local = renewal_local.strftime("%B %d, %Y")
            renewal_time_local = renewal_local.strftime("%I:%M %p")

            template_data = {
                "admin_name": admin.full_name,
                "company_name": tenant.display_name,
                "plan_name": tenant.subscription_tier.replace("_", " ").title(),
                "billing_cycle": (tenant.billing_cycle or "monthly").title(),
                "days_remaining": days_remaining,
                "renewal_date": renewal_date_local,
                "renewal_time": renewal_time_local,
                "renewal_timezone": "GMT+1 (Africa/Lagos)",
                "renewal_amount": amount_naira,
                "app_url": settings.FRONTEND_URL,
                "doc_url": settings.FRONTEND_DOC_URL,
                "current_year": datetime.now(UTC).year,
            }

            if days_remaining == 1:
                subject = f"Your Luran subscription renews in 1 day - {tenant.display_name}"
            else:
                subject = (
                    f"Your Luran subscription renews in {days_remaining} days - {tenant.display_name}"
                )

            template = self.email_service.jinja_env.get_template("subscription_renewal_upcoming.html")
            html_content = template.render(**template_data)
            success = self.email_service._send_email(
                to_email=admin.email,
                subject=subject,
                html_content=html_content,
            )

            await self._record_notification(
                tenant_id=tenant_id,
                notification_type=notification_type,
                recipient_email=admin.email,
                subject=subject,
                template_data=template_data,
                reference_id=latest_payment.id if latest_payment else None,
                reference_type="payment",
                status="sent" if success else "failed",
                error_message=None if success else "SMTP send failed",
            )
            return success

        except Exception as e:
            logger.error(f"Error in send_subscription_renewal_reminder: {e}")
            return False
