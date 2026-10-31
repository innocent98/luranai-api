"""Response schema for GET /api/v1/tenant/entitlements."""

from pydantic import BaseModel, Field


class FeatureFlag(BaseModel):
    enabled: bool
    min_tier: str | None = Field(
        description="Lowest tier that includes the feature; null = platform grant only"
    )


class FeatureLimit(FeatureFlag):
    limit: int | None = Field(description="Maximum allowed; null = unlimited")
    used: int


class WhatsAppFeature(FeatureLimit):
    platform_available: bool = Field(
        description="Global WhatsApp kill switch (WHATSAPP_ENABLED); false = hide the section"
    )


class EntitlementFeatures(BaseModel):
    sales_close: FeatureFlag
    crm_notifications: FeatureFlag
    screen_recording: FeatureFlag
    ratings_export: FeatureFlag
    lookup_chains: FeatureFlag
    webhooks: FeatureFlag
    analytics: FeatureFlag
    ai_actions: FeatureLimit
    whatsapp: WhatsAppFeature
    voice: FeatureFlag
    offerings: FeatureLimit


class EntitlementsResponse(BaseModel):
    tier: str
    subscription_status: str
    features: EntitlementFeatures
