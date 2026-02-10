"""
Pricing API Schemas

Schemas for platform admin pricing management endpoints.
"""

from pydantic import BaseModel, Field


class PricingPlanCapabilitiesUpdate(BaseModel):
    """Capabilities update for subscription plan."""

    can_access_analytics: bool | None = None
    max_knowledge_sources: int | None = None
    can_use_url_sources: bool | None = None
    max_knowledge_mb: int | None = None
    max_conversations_per_month: int | None = None
    max_team_members: int | None = None
    max_response_tokens: int | None = None


class PricingPlanUpdateRequest(BaseModel):
    """Request to update subscription plan pricing and capabilities."""

    monthly_price_kobo: int | None = Field(None, description="Monthly price in kobo", ge=0)
    annual_price_kobo: int | None = Field(None, description="Annual price in kobo", ge=0)
    display_name: str | None = Field(None, description="Display name", min_length=1, max_length=100)
    description: str | None = Field(
        None, description="Plan description", min_length=1, max_length=500
    )
    capabilities: PricingPlanCapabilitiesUpdate | None = Field(
        None, description="Capability updates"
    )
    features: list[str] | None = Field(None, description="List of feature descriptions")
    is_active: bool | None = Field(None, description="Whether plan is active")
    sort_order: int | None = Field(None, description="Display order")
    reason: str = Field(..., description="Reason for pricing change (audit trail)", min_length=1)

    class Config:
        json_schema_extra = {
            "example": {
                "monthly_price_kobo": 1500000,
                "annual_price_kobo": 15000000,
                "capabilities": {
                    "max_knowledge_sources": 15,
                    "max_conversations_per_month": 1500,
                },
                "features": [
                    "15 knowledge sources",
                    "50MB storage",
                    "1,500 conversations/month",
                ],
                "description": "Updated plan for small businesses",
                "reason": "Market adjustment based on competitor analysis",
            }
        }


class PricingPlanCapabilitiesResponse(BaseModel):
    """Capabilities for pricing plan response."""

    can_access_analytics: bool
    max_knowledge_sources: int | None
    can_use_url_sources: bool
    max_knowledge_mb: int | None
    max_conversations_per_month: int | None
    max_team_members: int | None
    max_response_tokens: int


class PricingPlanResponse(BaseModel):
    """Response model for subscription plan."""

    tier: str
    display_name: str
    description: str
    monthly_price_kobo: int
    annual_price_kobo: int
    monthly_price_naira: float
    annual_price_naira: float
    capabilities: PricingPlanCapabilitiesResponse
    features: list[str]
    is_active: bool
    sort_order: int

    class Config:
        from_attributes = True


class PricingPlanListResponse(BaseModel):
    """Response for listing all pricing plans."""

    plans: list[PricingPlanResponse]
    total: int


# Enterprise Pricing Calculator Schemas


class VolumeTierBreakdown(BaseModel):
    """Breakdown for a single volume pricing tier."""

    range: str = Field(..., description="Volume range (e.g., '25,000-50,000')")
    conversations_in_tier: int = Field(..., description="Conversations falling in this tier")
    price_per_conv_kobo: int = Field(..., description="Price per conversation in kobo")
    price_per_conv_naira: float = Field(..., description="Price per conversation in Naira")
    subtotal_kobo: int = Field(..., description="Subtotal cost for this tier in kobo")
    subtotal_naira: float = Field(..., description="Subtotal cost for this tier in Naira")


class CostEstimate(BaseModel):
    """AI cost estimate."""

    cost_per_conv_kobo: int = Field(..., description="AI cost per conversation in kobo")
    total_kobo: int = Field(..., description="Total AI cost in kobo")
    total_naira: float = Field(..., description="Total AI cost in Naira")


class ProfitMargin(BaseModel):
    """Profit margin calculation."""

    profit_kobo: int = Field(..., description="Profit in kobo")
    profit_naira: float = Field(..., description="Profit in Naira")
    margin_percentage: float = Field(..., description="Profit margin percentage")


class EnterprisePricingResponse(BaseModel):
    """Response from enterprise pricing calculator."""

    conversations: int = Field(..., description="Monthly conversation volume")
    tier_breakdown: list[VolumeTierBreakdown] = Field(..., description="Pricing breakdown by tier")
    total_cost_kobo: int = Field(..., description="Total monthly cost in kobo")
    total_cost_naira: float = Field(..., description="Total monthly cost in Naira")
    average_price_per_conv_kobo: int = Field(
        ..., description="Average price per conversation in kobo"
    )
    average_price_per_conv_naira: float = Field(
        ..., description="Average price per conversation in Naira"
    )
    ai_cost_estimate: CostEstimate = Field(..., description="Estimated AI costs")
    profit_margin: ProfitMargin = Field(..., description="Profit margin analysis")


class VolumePricingTier(BaseModel):
    """Volume pricing tier definition."""

    range: str = Field(..., description="Conversation range")
    min_conversations: int = Field(..., description="Minimum conversations in tier")
    max_conversations: int | None = Field(
        None, description="Maximum conversations in tier (null for unlimited)"
    )
    price_per_conv_kobo: int = Field(..., description="Price per conversation in kobo")
    price_per_conv_naira: float = Field(..., description="Price per conversation in Naira")
    example_monthly_cost_naira: float = Field(
        ..., description="Example monthly cost at minimum volume"
    )


class EnterprisePricingTiersResponse(BaseModel):
    """List of all Enterprise volume pricing tiers."""

    tiers: list[VolumePricingTier] = Field(..., description="All pricing tiers")
    note: str = Field(
        default="Pricing is volume-based. Higher volumes get lower per-conversation rates.",
        description="Explanation of pricing model",
    )


class PricingCalculatorRequest(BaseModel):
    """Request to calculate Enterprise pricing."""

    conversations: int = Field(..., ge=1, description="Expected monthly conversation volume")


class TierComparison(BaseModel):
    """Comparison between Growth and Enterprise tiers."""

    recommended_tier: str = Field(..., description="Recommended tier")
    conversations: int = Field(..., description="Monthly conversations")
    growth_tier_with_overage: dict | None = Field(None, description="Growth tier cost with overage")
    enterprise_tier: dict | None = Field(None, description="Enterprise tier cost")
    savings: dict | None = Field(None, description="Savings with Enterprise")
    recommendation: str = Field(..., description="Recommendation explanation")
    growth_tier: dict | None = Field(None, description="Growth tier details")
    reason: str | None = Field(None, description="Reason for recommendation")
    note: str | None = Field(None, description="Additional notes")
