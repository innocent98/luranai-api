"""Schemas for platform admin subscription management."""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from uuid import UUID


# Request Schemas
class TenantSubscriptionOverrideRequest(BaseModel):
    """Request to manually override a tenant's subscription by platform admin."""

    tier: Literal["trial", "lite", "professional", "enterprise"] = Field(
        ...,
        description="New subscription tier to assign"
    )
    billing_cycle: Optional[Literal["monthly", "annual"]] = Field(
        None,
        description="Billing cycle (null for trial tier)"
    )
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="Reason for manual override (required for audit trail)"
    )
    apply_tier_capabilities: bool = Field(
        default=True,
        description="Reset capabilities to tier defaults (false preserves manual overrides)"
    )


class TrialExtensionRequest(BaseModel):
    """Request to extend a trial period."""

    days: int = Field(
        ...,
        ge=1,
        le=90,
        description="Number of days to extend trial period (1-90)"
    )
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="Reason for trial extension (required for audit trail)"
    )


# Response Schemas
class SubscriptionHistoryResponse(BaseModel):
    """Subscription history entry for audit trail."""

    id: UUID
    tenant_id: UUID
    action: str
    previous_tier: Optional[str] = None
    new_tier: Optional[str] = None
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    changed_by_type: str
    changed_by_name: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantSubscriptionDetailResponse(BaseModel):
    """Complete subscription details for a tenant (platform admin view)."""

    # Tenant info
    tenant_id: UUID
    tenant_name: str

    # Current subscription
    subscription_tier: str
    subscription_status: str
    billing_cycle: Optional[str] = None
    trial_ends_at: Optional[datetime] = None
    subscription_starts_at: Optional[datetime] = None

    # Override tracking
    has_overrides: bool
    override_fields: list[str] = Field(default_factory=list)

    # History and stats
    recent_changes: list[SubscriptionHistoryResponse] = Field(default_factory=list)
    total_payments: int = 0
    total_revenue_naira: float = 0.0


class PricingPlanResponse(BaseModel):
    """Public pricing information for a subscription tier."""

    tier: str
    display_name: str
    description: str
    monthly_price_kobo: int
    annual_price_kobo: int
    monthly_price_naira: float
    annual_price_naira: float
    annual_savings_naira: float
    annual_discount_percentage: float
    capabilities: dict
    features: list[str]
    is_popular: bool = False


class PaymentTransactionResponse(BaseModel):
    """Payment transaction details (platform admin view)."""

    id: UUID
    tenant_id: UUID
    tenant_name: str
    reference: str
    amount: int
    currency: str
    status: str
    tier: str
    billing_cycle: str
    customer_email: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SubscriptionOverrideResponse(BaseModel):
    """Response after successful subscription override."""

    success: bool
    message: str
    tenant_id: UUID
    previous_tier: str
    new_tier: str
    capabilities_applied: bool


class TrialExtensionResponse(BaseModel):
    """Response after successful trial extension."""

    success: bool
    message: str
    tenant_id: UUID
    previous_trial_end: datetime
    new_trial_end: datetime
    days_extended: int
