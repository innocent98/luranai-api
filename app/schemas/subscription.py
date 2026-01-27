"""Subscription Pydantic schemas."""

from pydantic import BaseModel, Field
from typing import Optional


class SubscriptionUpgradeRequest(BaseModel):
    """Schema for subscription upgrade request."""

    tier: str = Field(..., description="Target tier: lite, professional, enterprise")
    billing_cycle: str = Field(default="monthly", description="Billing cycle: monthly, annual")

    class Config:
        json_schema_extra = {
            "example": {
                "tier": "professional",
                "billing_cycle": "annual"
            }
        }


class SubscriptionUpgradeResponse(BaseModel):
    """Schema for subscription upgrade response."""

    message: str
    tier: str
    status: str
    billing_cycle: Optional[str] = None
