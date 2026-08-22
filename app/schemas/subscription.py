"""Subscription Pydantic schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MySubscriptionResponse(BaseModel):
    """Schema for user's current subscription details."""

    tenant_id: UUID
    tenant_name: str
    subscription_tier: str = Field(
        ..., description="Current tier: trial, lite, professional, growth, enterprise"
    )
    subscription_status: str = Field(
        ..., description="Status: trial, active, canceled, past_due, trial_expired"
    )
    billing_cycle: str | None = Field(None, description="Billing cycle: monthly, annual")
    trial_ends_at: datetime | None = Field(
        None, description="Trial end date (null if not on trial)"
    )
    subscription_starts_at: datetime | None = Field(
        None, description="Subscription start date (null if on trial)"
    )
    subscription_ends_at: datetime | None = Field(
        None, description="Subscription end date (null if active)"
    )
    next_billing_date: datetime | None = Field(
        None, description="Next billing date (null if canceled or trial)"
    )
    can_upgrade: bool = Field(..., description="Whether user can upgrade (trial or lower tier)")
    can_cancel: bool = Field(..., description="Whether user can cancel (active paid subscription)")
    is_trial: bool = Field(..., description="Whether currently on trial")
    days_until_expiry: int | None = Field(None, description="Days until trial/subscription expires")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "123e4567-e89b-12d3-a456-426614174000",
                "tenant_name": "Acme Corp",
                "subscription_tier": "professional",
                "subscription_status": "active",
                "billing_cycle": "annual",
                "trial_ends_at": None,
                "subscription_starts_at": "2024-01-15T10:00:00Z",
                "subscription_ends_at": None,
                "next_billing_date": "2025-01-15T10:00:00Z",
                "can_upgrade": True,
                "can_cancel": True,
                "is_trial": False,
                "days_until_expiry": None,
            }
        }
    )


class SubscriptionCancelRequest(BaseModel):
    """Schema for subscription cancellation request."""

    reason: str | None = Field(None, description="Reason for cancellation (optional, for feedback)")
    immediate: bool = Field(
        default=False,
        description="If True, cancel immediately. If False, cancel at end of billing period.",
    )

    model_config = ConfigDict(
        json_schema_extra={"example": {"reason": "Found a better alternative", "immediate": False}}
    )


class SubscriptionCancelResponse(BaseModel):
    """Schema for subscription cancellation response."""

    message: str
    status: str = Field(..., description="New subscription status")
    ends_at: datetime | None = Field(None, description="When subscription will end")
    refund_eligible: bool = Field(..., description="Whether refund may be available")
    can_reactivate_until: datetime | None = Field(
        None, description="Deadline to reactivate before data deletion"
    )


class SubscriptionDowngradeRequest(BaseModel):
    """Request to downgrade subscription."""

    tier: str = Field(..., description="Target tier (lite, professional, growth)")
    reason: str | None = Field(None, description="Reason for downgrade", max_length=500)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tier": "lite",
                "reason": "Reducing costs, don't need professional features",
            }
        }
    )


class ValidationViolation(BaseModel):
    """A limit that would be exceeded by tier change."""

    field: str = Field(..., description="Field that exceeds limit")
    current_value: int = Field(..., description="Current usage")
    allowed_value: int = Field(..., description="New tier's limit")
    difference: int = Field(..., description="Amount over limit")
    action_required: str = Field(..., description="What user must do")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "field": "knowledge_sources",
                "current_value": 15,
                "allowed_value": 10,
                "difference": 5,
                "action_required": "Delete 5 knowledge sources before downgrading",
            }
        }
    )


class SubscriptionDowngradeResponse(BaseModel):
    """Response from successful downgrade."""

    success: bool
    message: str
    previous_tier: str
    new_tier: str
    next_billing_date: datetime | None
    next_billing_amount_kobo: int
    warnings: list[str] = []

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "message": "Successfully downgraded to lite",
                "previous_tier": "professional",
                "new_tier": "lite",
                "next_billing_date": "2026-02-15T00:00:00Z",
                "next_billing_amount_kobo": 1200000,
                "warnings": ["Your monthly conversation limit will decrease to 1,000"],
            }
        }
    )
