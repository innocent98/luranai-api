"""Tenant Pydantic schemas for request/response."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.tenant import TenantStatus
from app.schemas.explore import TenantCategory

# Mirrors app.services.first_responder.FirstResponder (not imported: app.services
# imports app.schemas, which would be circular).
FirstResponder = Literal["ai", "ai_with_handoff", "human"]

CUSTOM_INSTRUCTIONS_MAX_CHARS = 500


# Base schemas
class TenantBase(BaseModel):
    """Base tenant schema with common fields."""

    name: str = Field(..., min_length=1, max_length=100, description="Unique identifier name")
    display_name: str = Field(..., min_length=1, max_length=255, description="Human-readable name")
    description: str | None = Field(
        None,
        max_length=100,
        description="Short description used in compact widget and support surfaces",
    )
    long_description: str | None = Field(
        None,
        max_length=3000,
        description="Extended company and support description",
    )
    support_headline: str | None = Field(
        None,
        max_length=160,
        description="Hosted support page headline",
    )
    support_subheadline: str | None = Field(
        None,
        max_length=255,
        description="Hosted support page subheadline",
    )
    support_hours: str | None = Field(
        None,
        max_length=120,
        description="Support availability note",
    )
    support_response_time: str | None = Field(
        None,
        max_length=120,
        description="Typical support response time note",
    )
    support_footer_note: str | None = Field(
        None,
        max_length=255,
        description="Small hosted support footer note",
    )
    is_publicly_listed: bool = Field(
        default=False,
        description="Whether the business appears in the public directory",
    )
    is_featured: bool = Field(
        default=False,
        description="Whether the business is pinned to the top of the public directory",
    )
    status: TenantStatus = Field(default=TenantStatus.ACTIVE, description="Tenant status")


class TenantAIConfigBase(BaseModel):
    """Base AI config schema."""

    brand_voice: dict = Field(
        default_factory=dict,
        description="Brand voice configuration: {tone, greeting_style, formality_level}",
    )
    allowed_topics: list[str] | None = Field(
        None, description="Array of allowed topics (null = all topics allowed)"
    )
    blocked_topics: list[str] | None = Field(None, description="Array of topics to avoid")
    escalation_keywords: list[str] = Field(
        default_factory=list, description="Keywords that trigger human escalation"
    )
    max_response_tokens: int = Field(
        default=450, ge=50, le=2000, description="Maximum tokens for AI response (default: 450)"
    )
    grounding_requirement: bool = Field(
        default=True, description="Whether AI must ground responses in knowledge base"
    )
    grounding_mode: Literal["strict", "balanced", "flexible"] = Field(
        default="balanced",
        description="Grounding strictness policy",
    )
    low_confidence_action: Literal["answer", "clarify", "escalate", "refuse"] = Field(
        default="clarify",
        description="Action when retrieval confidence is low",
    )
    no_retrieval_action: Literal["answer", "clarify", "escalate", "refuse"] = Field(
        default="clarify",
        description="Action when no knowledge is retrieved",
    )
    first_responder: FirstResponder = Field(default="ai", description="Who answers first")
    require_email_before_escalation: bool = Field(
        default=False,
        description="Require collecting customer email before escalating to human support",
    )
    require_name_before_escalation: bool = Field(
        default=False,
        description="Require collecting customer name before escalating to human support",
    )
    collect_name_before_chat: bool = Field(
        default=False,
        description="Collect customer name before starting a new chat",
    )
    collect_email_before_chat: bool = Field(
        default=False,
        description="Collect customer email before starting a new chat",
    )
    custom_instructions: str | None = Field(
        None,
        max_length=CUSTOM_INSTRUCTIONS_MAX_CHARS,
        description="Additional prompt instructions specific to this tenant",
    )


class TenantCapabilitiesBase(BaseModel):
    """Base capabilities schema."""

    can_create_tickets: bool = Field(default=True, description="Can escalate to support tickets")
    can_access_analytics: bool = Field(default=True, description="Can view analytics dashboard")
    can_upload_knowledge: bool = Field(default=True, description="Can upload knowledge documents")
    max_knowledge_mb: int | None = Field(
        default=100, ge=0, description="Maximum knowledge storage in MB (null = unlimited)"
    )
    max_conversations_per_month: int | None = Field(
        None, ge=0, description="Monthly conversation limit (null = unlimited)"
    )
    max_knowledge_sources: int | None = Field(
        None, ge=0, description="Total knowledge sources (files+URLs)"
    )
    can_use_url_sources: bool = Field(default=False, description="Can use URL sources")
    max_team_members: int | None = Field(None, ge=0, description="Maximum team members")
    max_automation_actions: int | None = Field(
        None, ge=0, description="Maximum automation actions allowed"
    )
    max_automation_executions_per_month: int | None = Field(
        None, ge=0, description="Maximum automation executions allowed per month"
    )
    can_use_ai_chat_automation: bool = Field(
        default=False,
        description="Whether automation actions can be used directly from normal AI chat",
    )
    can_use_lookup_chains: bool = Field(
        default=False,
        description="Whether AI chat can resolve lookup-first automation chains",
    )
    can_use_webhooks: bool = Field(
        default=False,
        description="Whether flows can include webhook/direct API call nodes",
    )
    max_flows: int | None = Field(None, ge=0, description="Maximum flows allowed")
    max_webhooks_per_flow: int | None = Field(
        None, ge=0, description="Maximum webhook nodes allowed per flow"
    )
    can_use_voice: bool = Field(
        default=False,
        description="Whether the tenant can use in-widget voice chat (paid, per-minute).",
    )
    max_voice_minutes_per_month: int | None = Field(
        None, ge=0, description="Monthly voice-minute cap; None = unlimited"
    )
    can_use_screen_recording: bool = Field(
        default=False, description="Customer screen recordings (plan-driven)"
    )
    max_audit_pages_per_crawl: int | None = Field(
        None, ge=0, description="Max pages an audit crawl may scan (null = unlimited)"
    )
    max_audits_per_month: int | None = Field(
        None, ge=0, description="Max dashboard audits per month (null = unlimited)"
    )
    can_use_sales_close: bool = Field(
        default=False, description="Sales meeting booking and quotes (plan-driven)"
    )
    can_use_crm_notifications: bool = Field(
        default=False, description="Slack and CRM webhook notifications (plan-driven)"
    )
    can_export_ratings: bool = Field(
        default=False, description="Admin ratings breakdown and CSV export (plan-driven)"
    )
    can_use_whatsapp: bool = Field(
        default=False,
        description="Whether the tenant may link WhatsApp numbers (plan-driven; a platform-admin edit is an override)",
    )
    max_whatsapp_numbers: int | None = Field(
        default=0, ge=0, description="WhatsApp numbers the tenant may link; null = unlimited"
    )


# Create schemas
class TenantCreate(TenantBase):
    """Schema for creating a new tenant."""

    pass  # API key is generated automatically by the service


class TenantAIConfigCreate(TenantAIConfigBase):
    """Schema for creating AI config."""

    pass


class TenantCapabilitiesCreate(TenantCapabilitiesBase):
    """Schema for creating capabilities."""

    pass


# Update schemas
class TenantUpdate(BaseModel):
    """Schema for updating a tenant (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=100)
    long_description: str | None = Field(None, max_length=3000)
    support_headline: str | None = Field(None, max_length=160)
    support_subheadline: str | None = Field(None, max_length=255)
    support_hours: str | None = Field(None, max_length=120)
    support_response_time: str | None = Field(None, max_length=120)
    support_footer_note: str | None = Field(None, max_length=255)
    is_publicly_listed: bool | None = None
    public_slug: str | None = Field(None, min_length=3, max_length=50)
    status: TenantStatus | None = None
    category: TenantCategory | None = None


class TenantFeaturedUpdate(BaseModel):
    """Schema for toggling a tenant's `is_featured` flag (platform admin only)."""

    model_config = ConfigDict(extra="forbid")

    is_featured: bool = Field(
        ..., description="True to pin to top of public directory, false to unpin"
    )


class TenantAIConfigUpdate(BaseModel):
    """Schema for updating AI config (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    brand_voice: dict | None = None
    allowed_topics: list[str] | None = None
    blocked_topics: list[str] | None = None
    escalation_keywords: list[str] | None = None
    max_response_tokens: int | None = Field(None, ge=50, le=2000)
    grounding_requirement: bool | None = None
    grounding_mode: Literal["strict", "balanced", "flexible"] | None = None
    low_confidence_action: Literal["answer", "clarify", "escalate", "refuse"] | None = None
    no_retrieval_action: Literal["answer", "clarify", "escalate", "refuse"] | None = None
    first_responder: FirstResponder | None = None
    require_email_before_escalation: bool | None = None
    require_name_before_escalation: bool | None = None
    collect_name_before_chat: bool | None = None
    collect_email_before_chat: bool | None = None
    custom_instructions: str | None = Field(None, max_length=CUSTOM_INSTRUCTIONS_MAX_CHARS)


class TenantCapabilitiesUpdate(BaseModel):
    """Schema for updating capabilities (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    can_create_tickets: bool | None = None
    can_access_analytics: bool | None = None
    can_upload_knowledge: bool | None = None
    max_knowledge_mb: int | None = Field(None, ge=0)
    max_conversations_per_month: int | None = Field(None, ge=0)
    max_knowledge_sources: int | None = Field(None, ge=0)
    can_use_url_sources: bool | None = None
    max_team_members: int | None = Field(None, ge=0)
    max_automation_actions: int | None = Field(None, ge=0)
    max_automation_executions_per_month: int | None = Field(None, ge=0)
    can_use_ai_chat_automation: bool | None = None
    can_use_lookup_chains: bool | None = None
    can_use_webhooks: bool | None = None
    max_flows: int | None = Field(None, ge=0)
    max_webhooks_per_flow: int | None = Field(None, ge=0)
    can_use_voice: bool | None = None
    max_voice_minutes_per_month: int | None = Field(None, ge=0)
    can_use_screen_recording: bool | None = None
    max_audit_pages_per_crawl: int | None = Field(None, ge=0)
    max_audits_per_month: int | None = Field(None, ge=0)
    can_use_sales_close: bool | None = None
    can_use_crm_notifications: bool | None = None
    can_export_ratings: bool | None = None
    can_use_whatsapp: bool | None = None
    max_whatsapp_numbers: int | None = Field(None, ge=0)


# Response schemas
class TenantAIConfigResponse(TenantAIConfigBase):
    """Schema for AI config response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    active_knowledge_sources: int = 0
    created_at: datetime
    updated_at: datetime


class TenantCapabilitiesResponse(TenantCapabilitiesBase):
    """Schema for capabilities response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    created_at: datetime
    updated_at: datetime


class TenantResponse(TenantBase):
    """Schema for tenant response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    public_slug: str | None = None
    api_key: str | None = Field(None, description="API key (only provided at creation)")
    subscription_tier: str
    subscription_status: str
    billing_cycle: str | None = None
    trial_ends_at: datetime | None = None
    subscription_starts_at: datetime | None = None
    subscription_ends_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    ai_config: TenantAIConfigResponse | None = None
    capabilities: TenantCapabilitiesResponse | None = None
    category: TenantCategory | None = None


class TenantListResponse(TenantBase):
    """Schema for tenant list response (without relationships)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    public_slug: str | None = None
    subscription_tier: str
    subscription_status: str
    billing_cycle: str | None = None
    trial_ends_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    category: TenantCategory | None = None


class CuratedVoiceResponse(BaseModel):
    id: str
    name: str
    description: str
    preview_url: str


class VoiceSettingsResponse(BaseModel):
    granted: bool = Field(..., description="Platform grant (TenantCapabilities.can_use_voice)")
    enabled: bool = Field(..., description="Tenant's own on/off")
    voice_id: str | None = Field(None, description="Chosen voice; null = platform default")
    minutes_used: float | None = Field(
        None, description="Minutes used this month; null when usage is unavailable"
    )
    minutes_limit: int | None = Field(None, description="Monthly cap; null = unlimited")
    available_voices: list[CuratedVoiceResponse]


class VoiceSettingsUpdate(BaseModel):
    enabled: bool
    voice_id: str | None = None
