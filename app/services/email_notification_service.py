"""Email notification service for automated tenant notifications."""

import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.email_notification import EmailNotification
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.models.payment import PaymentTransaction
from app.services.email_service import email_service
from app.core.config import settings


class EmailNotificationService:
    """Service for managing automated email notifications with duplicate prevention."""

    def __init__(self, db: AsyncSession):
        """
        Initialize email notification service.

        Args:
            db: Database session
        """
        self.db = db
        self.email_service = email_service

    async def _check_if_already_sent(
        self,
        tenant_id: uuid.UUID,
        notification_type: str,
        reference_id: Optional[uuid.UUID] = None,
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

        result = await self.db.execute(query)
        existing = result.scalars().first()

        return existing is not None

    async def _record_notification(
        self,
        tenant_id: uuid.UUID,
        notification_type: str,
        recipient_email: str,
        subject: str,
        template_data: Dict[str, Any],
        reference_id: Optional[uuid.UUID] = None,
        reference_type: Optional[str] = None,
        status: str = "pending",
        error_message: Optional[str] = None,
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
        await self.db.commit()
        await self.db.refresh(notification)

        return notification

    async def _get_tenant_admin(self, tenant_id: uuid.UUID) -> Optional[User]:
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
                User.is_active == True,
            )
        )

        result = await self.db.execute(query)
        return result.scalars().first()

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
            # Check for duplicates
            if await self._check_if_already_sent(tenant_id, "trial_expiring", hours=24):
                logger.info(f"Trial expiration email already sent for tenant {tenant_id}")
                await self._record_notification(
                    tenant_id=tenant_id,
                    notification_type="trial_expiring",
                    recipient_email="",
                    subject="",
                    template_data={},
                    status="skipped",
                )
                return True

            # Get tenant and admin
            tenant_query = select(Tenant).where(Tenant.id == tenant_id)
            tenant_result = await self.db.execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant or not tenant.trial_ends_at:
                logger.warning(f"Tenant {tenant_id} not found or no trial end date")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            # Prepare template data
            template_data = {
                "admin_name": admin.full_name,
                "company_name": tenant.display_name,
                "days_remaining": days_remaining,
                "expiry_date": tenant.trial_ends_at.strftime("%B %d, %Y"),
                "usage_summary": {
                    "conversations": 0,  # TODO: Query from analytics
                    "tickets": 0,
                    "knowledge_sources": 0,
                },
                "pricing": {
                    "lite_monthly": "₦12,000",
                    "professional_monthly": "₦35,000",
                },
                "app_url": settings.FRONTEND_URL,
                "doc_url": settings.FRONTEND_DOC_URL,
                "current_year": datetime.now().year,
            }

            subject = f"Your Luran Trial Expires in {days_remaining} Days - {tenant.display_name}"

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
                    notification_type="trial_expiring",
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
                    notification_type="trial_expiring",
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
            tenant_result = await self.db.execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            payment_query = select(PaymentTransaction).where(PaymentTransaction.id == payment_id)
            payment_result = await self.db.execute(payment_query)
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
                "billing_cycle": tenant.billing_cycle.title() if tenant.billing_cycle else "Monthly",
                "next_billing_date": next_billing_date.strftime("%B %d, %Y"),
                "payment_reference": str(payment.payment_reference),
                "features": {
                    "conversations": "2,000" if tenant.subscription_tier == "professional" else "500",
                    "team_members": "10" if tenant.subscription_tier == "professional" else "2",
                    "knowledge_sources": "Unlimited"
                    if tenant.subscription_tier == "professional"
                    else "5",
                    "custom_branding": tenant.subscription_tier == "professional",
                    "priority_support": tenant.subscription_tier
                    in ["professional", "enterprise"],
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
                template = self.email_service.jinja_env.get_template(
                    "subscription_confirmed.html"
                )
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
        failure_reason: Optional[str] = None,
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
            tenant_result = await self.db.execute(tenant_query)
            tenant = tenant_result.scalars().first()

            if not tenant:
                logger.warning(f"Tenant {tenant_id} not found")
                return False

            admin = await self._get_tenant_admin(tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {tenant_id}")
                return False

            payment_query = select(PaymentTransaction).where(PaymentTransaction.id == payment_id)
            payment_result = await self.db.execute(payment_query)
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
