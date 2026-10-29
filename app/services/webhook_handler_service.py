"""Paystack webhook event handler service."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy.orm import Session

from app.db.models.payment import PaymentTransaction
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.referral import rewards as referral_rewards
from app.services.subscription_service import SubscriptionService


class WebhookHandlerService:
    """Service for handling Paystack webhook events."""

    def __init__(self, db: Session):
        self.db = db
        self.subscription_service = SubscriptionService(db)

    def handle_event(self, event_type: str, data: dict) -> dict:
        """
        Route webhook events to appropriate handlers.

        Args:
            event_type: Paystack event type (e.g., charge.success)
            data: Event data payload

        Returns:
            dict with processing result

        Raises:
            HTTPException: If event handling fails
        """
        logger.info(f"Processing webhook event: {event_type}")

        handlers = {
            "charge.success": self._handle_charge_success,
            "subscription.create": self._handle_subscription_create,
            "subscription.disable": self._handle_subscription_disable,
            "subscription.not_renew": self._handle_subscription_not_renew,
            "invoice.create": self._handle_invoice_create,
            "invoice.payment_failed": self._handle_invoice_payment_failed,
            "charge.dispute.create": self._handle_dispute_create,
        }

        handler = handlers.get(event_type)
        if not handler:
            logger.warning(f"No handler for event type: {event_type}")
            return {"status": "ignored", "message": f"Event type {event_type} not handled"}

        try:
            result = handler(data)
            logger.info(f"Successfully processed {event_type}")
            return result
        except Exception as e:
            logger.error(f"Error handling {event_type}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to process webhook: {str(e)}",
            ) from e

    def _handle_charge_success(self, data: dict) -> dict:
        """
        Handle successful payment charge.

        Updates transaction record and upgrades tenant subscription.

        Args:
            data: Charge event data

        Returns:
            dict with processing result
        """
        reference = data.get("reference")
        if not reference:
            raise ValueError("Missing reference in charge.success event")

        logger.info(f"Handling successful charge: {reference}")

        # Find transaction record. The row lock serialises parallel deliveries of the
        # same event, so the status check below cannot race with another worker.
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.reference == reference)
            .with_for_update()
            .first()
        )

        if not transaction:
            logger.warning(f"Transaction not found: {reference}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Paystack retries webhooks. A replay must not re-run the tier upgrade, add a
        # second history row, or overwrite paid_at, so a settled transaction is a no-op.
        if transaction.status == "success":
            logger.info(f"charge.success replay ignored, already processed: {reference}")
            self.db.commit()  # nothing was mutated; this only releases the row lock
            return {"status": "already_processed"}

        # Update transaction record
        transaction.status = "success"
        transaction.paid_at = datetime.now(UTC)
        transaction.customer_email = data.get("customer", {}).get("email")
        transaction.customer_code = data.get("customer", {}).get("customer_code")
        transaction.channel = data.get("channel")
        transaction.authorization_code = data.get("authorization", {}).get("authorization_code")
        transaction.paystack_data = data

        # Get tenant's previous tier for history
        tenant = self.db.query(Tenant).filter(Tenant.id == transaction.tenant_id).first()
        previous_tier = tenant.subscription_tier if tenant else None
        previous_status = tenant.subscription_status if tenant else None

        # Upgrade tenant subscription
        try:
            self.subscription_service.upgrade_subscription(
                tenant_id=transaction.tenant_id,
                tier=transaction.tier,
                billing_cycle=transaction.billing_cycle,
            )

            logger.info(
                f"Upgraded tenant {transaction.tenant_id} to {transaction.tier} {transaction.billing_cycle}"
            )

            # Record subscription history
            history = SubscriptionHistory(
                tenant_id=transaction.tenant_id,
                action="upgrade",
                previous_tier=previous_tier,
                new_tier=transaction.tier,
                previous_status=previous_status,
                new_status="active",
                changed_by_type="webhook",
                changed_by_name="Paystack Webhook",
                reason=f"Payment successful for {transaction.tier} {transaction.billing_cycle}",
                event_metadata={
                    "transaction_reference": reference,
                    "amount": transaction.amount,
                    "billing_cycle": transaction.billing_cycle,
                },
                payment_reference=reference,
            )
            self.db.add(history)

            # Mark any active promotional grant as converted
            try:
                from app.services.promotional_grant_service import PromotionalGrantService

                PromotionalGrantService(self.db).mark_converted_for_tenant(
                    tenant_id=transaction.tenant_id,
                    paid_tier=transaction.tier,
                    paid_billing_cycle=transaction.billing_cycle,
                )
            except Exception as promo_exc:
                logger.error(
                    f"Promo conversion mark failed for tenant {transaction.tenant_id}: {promo_exc}"
                )

        except Exception as e:
            logger.error(f"Failed to upgrade tenant subscription: {e}")
            transaction.error_message = f"Subscription upgrade failed: {str(e)}"
            # Don't raise - we got the payment, will fix subscription manually

        # Referral reward for the tenant's first payment. After the upgrade (which commits
        # internally) and before the final commit, so the reward row commits with the history row.
        self._create_referral_reward(transaction)

        self.db.commit()

        return {
            "status": "success",
            "message": "Payment processed and subscription upgraded",
            "transaction_id": str(transaction.id),
            "tenant_id": str(transaction.tenant_id),
        }

    def _create_referral_reward(self, transaction: PaymentTransaction) -> None:
        """Create the referral reward for a just-settled payment, without ever affecting it.

        Runs inside a SAVEPOINT and swallows every failure (including a failed SQL statement,
        which would otherwise abort the Postgres transaction): the payment, the upgrade and the
        history row are already in the session and must still commit. Logs ids only. A reward lost
        here is recreated by the daily ``reconcile_missing_rewards`` job, because a replay of this
        event returns early and would never retry it.
        """
        tenant_id, reference, paid_at = (
            transaction.tenant_id,
            transaction.reference,
            transaction.paid_at,
        )
        try:
            with self.db.begin_nested():
                referral_rewards.create_reward_for_first_payment(
                    self.db, tenant_id, reference, paid_at
                )
        except Exception as exc:  # noqa: BLE001 - rewarding must never break the payment path
            logger.error(
                f"Referral reward creation failed for tenant {tenant_id}, payment {reference}: "
                f"{type(exc).__name__}"
            )

    def _handle_dispute_create(self, data: dict) -> dict:
        """Hold the referral reward of the tenant whose payment was disputed.

        Event: ``charge.dispute.create`` (named in Paystack's docs). Paystack's docs do not publish
        the payload fields, so the transaction reference is read defensively from
        ``data.transaction.reference``, then ``data.transaction_reference``, then
        ``data.reference``. Anything unresolvable is skipped (a 500 would only make Paystack retry).
        """
        transaction_data = data.get("transaction")
        reference = (
            (transaction_data.get("reference") if isinstance(transaction_data, dict) else None)
            or data.get("transaction_reference")
            or data.get("reference")
        )
        if not reference:
            logger.warning("charge.dispute.create without a transaction reference; ignored")
            return {"status": "skipped", "message": "Missing transaction reference"}

        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.reference == reference)
            .first()
        )
        if not transaction:
            logger.warning(f"Dispute for unknown transaction: {reference}")
            return {"status": "skipped", "message": "Transaction not found"}

        reward = referral_rewards.hold_for_dispute(self.db, transaction.tenant_id)
        self.db.commit()
        return {
            "status": "success",
            "message": "Dispute recorded",
            "tenant_id": str(transaction.tenant_id),
            "reward_status": reward.status.value if reward else None,
        }

    def _handle_subscription_create(self, data: dict) -> dict:
        """
        Handle subscription creation event.

        This event is triggered when a recurring subscription is successfully created
        on Paystack after the first payment. It stores the subscription_code which
        is needed for managing the recurring subscription (cancel, check status, etc.).

        Args:
            data: Subscription event data

        Returns:
            dict with processing result
        """
        subscription_code = data.get("subscription_code")
        customer_code = data.get("customer", {}).get("customer_code")
        plan_code = data.get("plan", {}).get("plan_code")

        if not subscription_code:
            raise ValueError("Missing subscription_code in subscription.create event")

        logger.info(f"Handling subscription creation: {subscription_code} (plan: {plan_code})")

        # Find most recent successful transaction for this customer
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(
                PaymentTransaction.customer_code == customer_code,
                PaymentTransaction.status == "success",
            )
            .order_by(PaymentTransaction.created_at.desc())
            .first()
        )

        if not transaction:
            logger.warning(f"No transaction found for customer: {customer_code}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Update with subscription code
        transaction.subscription_code = subscription_code

        # Also ensure tenant subscription is upgraded (in case charge.success didn't trigger it)
        tenant = self.db.query(Tenant).filter(Tenant.id == transaction.tenant_id).first()
        if tenant and tenant.subscription_tier == "trial":
            try:
                logger.info(f"Upgrading tenant {tenant.id} via subscription.create event")
                self.subscription_service.upgrade_subscription(
                    tenant_id=transaction.tenant_id,
                    tier=transaction.tier,
                    billing_cycle=transaction.billing_cycle,
                )
            except Exception as e:
                logger.error(f"Failed to upgrade tenant in subscription.create: {e}")

        self.db.commit()

        logger.info(
            f"Updated transaction {transaction.id} with subscription code {subscription_code}"
        )

        return {
            "status": "success",
            "message": "Subscription created and saved",
            "transaction_id": str(transaction.id),
            "subscription_code": subscription_code,
            "plan_code": plan_code,
        }

    def _handle_subscription_disable(self, data: dict) -> dict:
        """
        Handle subscription cancellation event.

        Updates tenant subscription status to canceled.

        Args:
            data: Subscription event data

        Returns:
            dict with processing result
        """
        subscription_code = data.get("subscription_code")

        if not subscription_code:
            raise ValueError("Missing subscription_code in subscription.disable event")

        logger.info(f"Handling subscription cancellation: {subscription_code}")

        # Find transaction with this subscription code
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.subscription_code == subscription_code)
            .first()
        )

        if not transaction:
            logger.warning(f"No transaction found for subscription: {subscription_code}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Update tenant subscription status
        tenant = self.db.query(Tenant).filter(Tenant.id == transaction.tenant_id).first()
        if tenant:
            tenant.subscription_status = "canceled"
            tenant.subscription_ends_at = datetime.now(UTC)
            self.db.commit()

            logger.info(f"Cancelled subscription for tenant {tenant.id}")

            return {
                "status": "success",
                "message": "Subscription cancelled",
                "tenant_id": str(tenant.id),
            }

        return {"status": "skipped", "message": "Tenant not found"}

    def _handle_subscription_not_renew(self, data: dict) -> dict:
        """
        Handle subscription non-renewal event.

        Updates tenant subscription status.

        Args:
            data: Subscription event data

        Returns:
            dict with processing result
        """
        subscription_code = data.get("subscription_code")

        if not subscription_code:
            raise ValueError("Missing subscription_code in subscription.not_renew event")

        logger.info(f"Handling subscription non-renewal: {subscription_code}")

        # Find transaction with this subscription code
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.subscription_code == subscription_code)
            .first()
        )

        if not transaction:
            logger.warning(f"No transaction found for subscription: {subscription_code}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Update tenant subscription status
        tenant = self.db.query(Tenant).filter(Tenant.id == transaction.tenant_id).first()
        if tenant:
            tenant.subscription_status = "expiring"
            # Don't set end date yet - will end at current billing cycle
            self.db.commit()

            logger.info(f"Marked subscription as expiring for tenant {tenant.id}")

            return {
                "status": "success",
                "message": "Subscription marked as expiring",
                "tenant_id": str(tenant.id),
            }

        return {"status": "skipped", "message": "Tenant not found"}

    def _handle_invoice_create(self, data: dict) -> dict:
        """
        Handle invoice creation event for recurring billing.

        This event is triggered when Paystack creates an invoice for a recurring
        subscription renewal. The invoice will be charged automatically.

        Args:
            data: Invoice event data

        Returns:
            dict with processing result
        """
        subscription_code = data.get("subscription", {}).get("subscription_code")
        invoice_code = data.get("invoice_code")
        amount = data.get("amount")

        if not subscription_code:
            logger.warning("No subscription_code in invoice.create event")
            return {"status": "skipped", "message": "No subscription code"}

        logger.info(
            f"Handling invoice creation: {invoice_code} for subscription: {subscription_code}"
        )

        # Find transaction with this subscription code
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.subscription_code == subscription_code)
            .first()
        )

        if not transaction:
            logger.warning(f"No transaction found for subscription: {subscription_code}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Log the renewal invoice - actual payment will be handled by charge.success or invoice.payment_failed
        amount_value = amount or 0
        logger.info(
            f"Renewal invoice created for tenant {transaction.tenant_id}: "
            f"₦{amount_value/100:.2f} ({transaction.tier} {transaction.billing_cycle})"
        )

        return {
            "status": "success",
            "message": "Invoice logged - awaiting payment",
            "tenant_id": str(transaction.tenant_id),
            "invoice_code": invoice_code,
        }

    def _handle_invoice_payment_failed(self, data: dict) -> dict:
        """
        Handle failed invoice payment event.

        Updates tenant subscription status to past_due.

        Args:
            data: Invoice event data

        Returns:
            dict with processing result
        """
        subscription_code = data.get("subscription", {}).get("subscription_code")

        if not subscription_code:
            logger.warning("No subscription_code in invoice.payment_failed event")
            return {"status": "skipped", "message": "No subscription code"}

        logger.info(f"Handling failed payment for subscription: {subscription_code}")

        # Find transaction with this subscription code
        transaction = (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.subscription_code == subscription_code)
            .first()
        )

        if not transaction:
            logger.warning(f"No transaction found for subscription: {subscription_code}")
            return {"status": "skipped", "message": "Transaction not found"}

        # Update tenant subscription status
        tenant = self.db.query(Tenant).filter(Tenant.id == transaction.tenant_id).first()
        if tenant:
            tenant.subscription_status = "past_due"
            self.db.commit()

            logger.warning(f"Marked subscription as past_due for tenant {tenant.id}")

            # TODO: Send notification email to tenant admin

            return {
                "status": "success",
                "message": "Subscription marked as past_due",
                "tenant_id": str(tenant.id),
            }

        return {"status": "skipped", "message": "Tenant not found"}

    def get_transaction_by_reference(self, reference: str) -> PaymentTransaction | None:
        """
        Get payment transaction by reference.

        Args:
            reference: Transaction reference

        Returns:
            PaymentTransaction or None
        """
        return (
            self.db.query(PaymentTransaction)
            .filter(PaymentTransaction.reference == reference)
            .first()
        )

    def create_transaction(
        self,
        tenant_id: UUID,
        reference: str,
        amount: int,
        tier: str,
        billing_cycle: str,
        customer_email: str | None = None,
    ) -> PaymentTransaction:
        """
        Create a new payment transaction record.

        Args:
            tenant_id: Tenant UUID
            reference: Unique transaction reference
            amount: Amount in kobo
            tier: Subscription tier
            billing_cycle: Billing cycle
            customer_email: Customer email (optional)

        Returns:
            Created PaymentTransaction
        """
        transaction = PaymentTransaction(
            tenant_id=tenant_id,
            reference=reference,
            amount=amount,
            currency="NGN",
            status="pending",
            tier=tier,
            billing_cycle=billing_cycle,
            customer_email=customer_email,
        )

        self.db.add(transaction)
        self.db.commit()
        self.db.refresh(transaction)

        logger.info(f"Created transaction record: {reference}")

        return transaction
