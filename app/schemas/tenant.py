"""Tenant Pydantic schemas for request/response."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.tenant import TenantStatus

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
    max_flows: int | None = Field(None, ge=0, description="Maximum flows allowed")


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
    max_flows: int | None = Field(None, ge=0)


# Response schemas
class TenantAIConfigResponse(TenantAIConfigBase):
    """Schema for AI config response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
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
