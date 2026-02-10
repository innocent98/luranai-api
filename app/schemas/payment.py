"""Payment Pydantic schemas for Paystack integration."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# Request Schemas
class PaymentInitializeRequest(BaseModel):
    """Schema for initializing a payment."""

    tier: str = Field(..., description="Subscription tier: lite, professional, enterprise")
    billing_cycle: str = Field(..., description="Billing cycle: monthly, annual")


class PaymentVerificationRequest(BaseModel):
    """Schema for verifying a payment."""

    reference: str = Field(..., description="Paystack transaction reference")


# Response Schemas
class PaymentInitializeResponse(BaseModel):
    """Schema for payment initialization response."""

    authorization_url: str = Field(..., description="URL to redirect user for payment")
    access_code: str = Field(..., description="Paystack access code")
    reference: str = Field(..., description="Unique transaction reference")
    amount: int = Field(..., description="Amount in kobo (₦1 = 100 kobo)")
    currency: str = Field(default="NGN", description="Currency code")


class PaymentVerificationResponse(BaseModel):
    """Schema for payment verification response."""

    status: str = Field(..., description="Payment status: success, failed, pending")
    reference: str = Field(..., description="Transaction reference")
    amount: int = Field(..., description="Amount in kobo")
    currency: str = Field(..., description="Currency code")
    paid_at: datetime | None = Field(None, description="When payment was completed")
    channel: str | None = Field(None, description="Payment channel: card, bank, etc.")
    customer_email: str | None = Field(None, description="Customer email")
    subscription_code: str | None = Field(None, description="Paystack subscription code")


class SubscriptionPlanResponse(BaseModel):
    """Schema for Paystack subscription plan."""

    plan_code: str = Field(..., description="Paystack plan code")
    name: str = Field(..., description="Plan name")
    amount: int = Field(..., description="Amount in kobo")
    interval: str = Field(..., description="Billing interval: monthly, annually")
    currency: str = Field(default="NGN")


# Webhook Schemas
class PaystackWebhookEvent(BaseModel):
    """Schema for Paystack webhook event."""

    event: str = Field(..., description="Event type: charge.success, subscription.create, etc.")
    data: dict = Field(..., description="Event data")


# Internal Schemas
class TransactionRecord(BaseModel):
    """Internal schema for storing transaction records."""

    id: UUID
    tenant_id: UUID
    reference: str
    amount: int
    currency: str
    status: str
    tier: str
    billing_cycle: str
    paystack_data: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
