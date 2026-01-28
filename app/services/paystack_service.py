"""Paystack payment integration service."""

import hashlib
import hmac
from datetime import datetime
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.plan_service import PlanService


class PaystackService:
    """Service for handling Paystack payment operations."""

    # Paystack plan codes (actual codes from Paystack dashboard)
    PLAN_CODES = {
        "lite_monthly": "PLN_jyimgdb5eobje4a",
        "lite_annual": "PLN_yeostwggamfwy3b",
        "professional_monthly": "PLN_ev747ystsd08nhq",
        "professional_annual": "PLN_pz9lyqt3q4uajn4",
    }

    def __init__(self, db: Session):
        self.db = db
        self.plan_service = PlanService(db)
        self.base_url = settings.PAYSTACK_BASE_URL
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }

    def get_amount(self, tier: str, billing_cycle: str) -> int:
        """
        Get subscription amount in kobo.

        Args:
            tier: Subscription tier (lite, professional, enterprise)
            billing_cycle: Billing cycle (monthly, annual)

        Returns:
            Amount in kobo (₦1 = 100 kobo)

        Raises:
            ValueError: If invalid tier or billing cycle
        """
        if billing_cycle not in ["monthly", "annual"]:
            raise ValueError(f"Invalid billing cycle: {billing_cycle}")

        # Get price from database
        amount_kobo = self.plan_service.get_pricing(tier, billing_cycle)

        if amount_kobo == 0 and tier == "enterprise":
            raise ValueError(
                f"Enterprise pricing is custom. Please contact sales."
            )

        return amount_kobo

    def get_plan_code(self, tier: str, billing_cycle: str) -> str:
        """
        Get Paystack plan code for subscription.

        Args:
            tier: Subscription tier
            billing_cycle: Billing cycle

        Returns:
            Paystack plan code

        Raises:
            ValueError: If invalid tier or enterprise tier
        """
        if tier == "enterprise":
            raise ValueError("Enterprise subscriptions require custom pricing")

        plan_key = f"{tier}_{billing_cycle}"
        if plan_key not in self.PLAN_CODES:
            raise ValueError(f"No plan code for {tier} {billing_cycle}")

        return self.PLAN_CODES[plan_key]

    async def initialize_subscription(
        self,
        email: str,
        tier: str,
        billing_cycle: str,
        tenant_id: str,
        callback_url: Optional[str] = None,
    ) -> dict:
        """
        Initialize a recurring Paystack subscription.

        This creates a subscription that will automatically charge the customer
        at the specified interval (monthly or annually).

        Args:
            email: Customer email
            tier: Subscription tier
            billing_cycle: Billing cycle
            tenant_id: Tenant UUID
            callback_url: URL to redirect after payment

        Returns:
            dict with authorization_url, access_code, reference

        Raises:
            HTTPException: If initialization fails
        """
        try:
            # Get plan code for recurring subscription
            plan_code = self.get_plan_code(tier, billing_cycle)

            # Get amount for display purposes
            amount = self.get_amount(tier, billing_cycle)

            # Generate unique reference
            reference = f"luran_sub_{tenant_id[:8]}_{uuid4().hex[:8]}"

            # Prepare request data for subscription initialization
            # Note: When using a plan, amount is still required by Paystack
            data = {
                "email": email,
                "amount": amount,  # Required even with plan
                "plan": plan_code,
                "reference": reference,
                "callback_url": callback_url or f"{settings.SERVER_HOST}/subscription/verify",
                "metadata": {
                    "tenant_id": tenant_id,
                    "tier": tier,
                    "billing_cycle": billing_cycle,
                    "subscription_type": "recurring",
                    "custom_fields": [
                        {
                            "display_name": "Subscription Tier",
                            "variable_name": "tier",
                            "value": tier,
                        },
                        {
                            "display_name": "Billing Cycle",
                            "variable_name": "billing_cycle",
                            "value": billing_cycle,
                        },
                    ],
                },
            }

            logger.info(f"Initializing Paystack subscription: {reference} for {email} (plan: {plan_code})")

            # Make API request to transaction/initialize with plan parameter
            # Paystack creates subscription automatically when plan is provided
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/transaction/initialize",
                    json=data,
                    headers=self.headers,
                    timeout=30.0,
                )

                response.raise_for_status()
                result = response.json()

                if not result.get("status"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=result.get("message", "Failed to initialize subscription"),
                    )

                payment_data = result["data"]
                logger.info(f"Subscription initialized: {reference}")

                return {
                    "authorization_url": payment_data["authorization_url"],
                    "access_code": payment_data["access_code"],
                    "reference": payment_data["reference"],
                    "amount": amount,
                    "currency": "NGN",
                    "plan_code": plan_code,
                }

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack API error: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Payment gateway error: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"Failed to initialize subscription: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to initialize subscription: {str(e)}",
            )

    async def verify_transaction(self, reference: str) -> dict:
        """
        Verify a Paystack transaction.

        Args:
            reference: Transaction reference

        Returns:
            dict with transaction details

        Raises:
            HTTPException: If verification fails
        """
        try:
            logger.info(f"Verifying transaction: {reference}")

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/transaction/verify/{reference}",
                    headers=self.headers,
                    timeout=30.0,
                )

                response.raise_for_status()
                result = response.json()

                if not result.get("status"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=result.get("message", "Transaction verification failed"),
                    )

                data = result["data"]
                logger.info(f"Transaction verified: {reference} - Status: {data['status']}")

                return {
                    "status": data["status"],
                    "reference": data["reference"],
                    "amount": data["amount"],
                    "currency": data["currency"],
                    "paid_at": data.get("paid_at"),
                    "channel": data.get("channel"),
                    "customer_email": data.get("customer", {}).get("email"),
                    "metadata": data.get("metadata", {}),
                }

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack API error: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Payment verification error: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"Failed to verify transaction: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to verify transaction: {str(e)}",
            )

    async def create_subscription(
        self,
        customer_code: str,
        plan_code: str,
        authorization_code: str,
    ) -> dict:
        """
        Create a subscription on Paystack.

        Args:
            customer_code: Paystack customer code
            plan_code: Paystack plan code
            authorization_code: Paystack authorization code from successful transaction

        Returns:
            dict with subscription details

        Raises:
            HTTPException: If subscription creation fails
        """
        try:
            data = {
                "customer": customer_code,
                "plan": plan_code,
                "authorization": authorization_code,
            }

            logger.info(f"Creating Paystack subscription: {plan_code} for {customer_code}")

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/subscription",
                    json=data,
                    headers=self.headers,
                    timeout=30.0,
                )

                response.raise_for_status()
                result = response.json()

                if not result.get("status"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=result.get("message", "Failed to create subscription"),
                    )

                subscription_data = result["data"]
                logger.info(f"Subscription created: {subscription_data['subscription_code']}")

                return subscription_data

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack API error: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Subscription creation error: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"Failed to create subscription: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create subscription: {str(e)}",
            )

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verify Paystack webhook signature.

        Paystack computes webhook signatures using HMAC SHA-512 with your
        secret key (the same key used for API calls).

        See: https://paystack.com/docs/payments/webhooks/#verify-webhook-signature

        Args:
            payload: Raw request body as bytes
            signature: X-Paystack-Signature header value

        Returns:
            True if signature is valid, False otherwise
        """
        # Paystack uses your secret key (not a separate webhook secret)
        secret_key = self.secret_key  # Already loaded from settings.PAYSTACK_SECRET_KEY

        # Compute HMAC SHA-512 hash using your Paystack secret key
        computed_signature = hmac.new(
            secret_key.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()

        is_valid = hmac.compare_digest(computed_signature, signature)

        if not is_valid:
            logger.warning(
                f"Webhook signature mismatch. "
                f"Expected: {computed_signature[:20]}... "
                f"Received: {signature[:20]}..."
            )

        return is_valid

    async def get_subscription(self, subscription_code: str) -> dict:
        """
        Get subscription details from Paystack.

        Args:
            subscription_code: Paystack subscription code

        Returns:
            dict with subscription details

        Raises:
            HTTPException: If request fails
        """
        try:
            logger.info(f"Fetching subscription: {subscription_code}")

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/subscription/{subscription_code}",
                    headers=self.headers,
                    timeout=30.0,
                )

                response.raise_for_status()
                result = response.json()

                if not result.get("status"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=result.get("message", "Failed to fetch subscription"),
                    )

                return result["data"]

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack API error: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch subscription: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"Failed to fetch subscription: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch subscription: {str(e)}",
            )

    async def cancel_subscription(
        self, subscription_code: str, email_token: str
    ) -> dict:
        """
        Cancel a subscription on Paystack.

        Args:
            subscription_code: Paystack subscription code
            email_token: Token from subscription email

        Returns:
            dict with cancellation details

        Raises:
            HTTPException: If cancellation fails
        """
        try:
            data = {"code": subscription_code, "token": email_token}

            logger.info(f"Cancelling subscription: {subscription_code}")

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/subscription/disable",
                    json=data,
                    headers=self.headers,
                    timeout=30.0,
                )

                response.raise_for_status()
                result = response.json()

                if not result.get("status"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=result.get("message", "Failed to cancel subscription"),
                    )

                logger.info(f"Subscription cancelled: {subscription_code}")
                return result["data"]

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack API error: {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Cancellation error: {e.response.text}",
            )
        except Exception as e:
            logger.error(f"Failed to cancel subscription: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to cancel subscription: {str(e)}",
            )
