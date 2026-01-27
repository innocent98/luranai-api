"""Tenant Pydantic schemas for request/response."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict

from app.db.models.tenant import TenantStatus


# Base schemas
class TenantBase(BaseModel):
    """Base tenant schema with common fields."""

    name: str = Field(..., min_length=1, max_length=100, description="Unique identifier name")
    display_name: str = Field(..., min_length=1, max_length=255, description="Human-readable name")
    description: Optional[str] = Field(None, description="Brief description of the product/tenant")
    status: TenantStatus = Field(default=TenantStatus.ACTIVE, description="Tenant status")


class TenantAIConfigBase(BaseModel):
    """Base AI config schema."""

    brand_voice: dict = Field(
        default_factory=dict,
        description="Brand voice configuration: {tone, greeting_style, emoji_usage, formality_level}",
    )
    allowed_topics: Optional[list[str]] = Field(
        None, description="Array of allowed topics (null = all topics allowed)"
    )
    blocked_topics: Optional[list[str]] = Field(None, description="Array of topics to avoid")
    escalation_keywords: list[str] = Field(
        default_factory=list, description="Keywords that trigger human escalation"
    )
    confidence_threshold: float = Field(
        default=0.55, ge=0.0, le=1.0, description="Minimum confidence score for responses"
    )
    max_response_tokens: int = Field(
        default=800, ge=50, le=2000, description="Maximum tokens for AI response (default: 800)"
    )
    grounding_requirement: bool = Field(
        default=True, description="Whether AI must ground responses in knowledge base"
    )
    custom_instructions: Optional[str] = Field(
        None, description="Additional prompt instructions specific to this tenant"
    )


class TenantCapabilitiesBase(BaseModel):
    """Base capabilities schema."""

    can_create_tickets: bool = Field(default=True, description="Can escalate to support tickets")
    can_access_analytics: bool = Field(default=True, description="Can view analytics dashboard")
    can_upload_knowledge: bool = Field(default=True, description="Can upload knowledge documents")
    max_knowledge_mb: Optional[int] = Field(default=100, ge=0, description="Maximum knowledge storage in MB (null = unlimited)")
    max_conversations_per_month: Optional[int] = Field(
        None, ge=0, description="Monthly conversation limit (null = unlimited)"
    )
    max_knowledge_sources: Optional[int] = Field(
        None, ge=0, description="Total knowledge sources (files+URLs)"
    )
    can_use_url_sources: bool = Field(
        default=False, description="Can use URL sources"
    )
    max_team_members: Optional[int] = Field(
        None, ge=0, description="Maximum team members"
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

    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[TenantStatus] = None


class TenantAIConfigUpdate(BaseModel):
    """Schema for updating AI config (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    brand_voice: Optional[dict] = None
    allowed_topics: Optional[list[str]] = None
    blocked_topics: Optional[list[str]] = None
    escalation_keywords: Optional[list[str]] = None
    confidence_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_response_tokens: Optional[int] = Field(None, ge=50, le=2000)
    grounding_requirement: Optional[bool] = None
    custom_instructions: Optional[str] = None


class TenantCapabilitiesUpdate(BaseModel):
    """Schema for updating capabilities (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    can_create_tickets: Optional[bool] = None
    can_access_analytics: Optional[bool] = None
    can_upload_knowledge: Optional[bool] = None
    max_knowledge_mb: Optional[int] = Field(None, ge=0)
    max_conversations_per_month: Optional[int] = Field(None, ge=0)
    max_knowledge_sources: Optional[int] = Field(None, ge=0)
    can_use_url_sources: Optional[bool] = None
    max_team_members: Optional[int] = Field(None, ge=0)


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
    api_key: Optional[str] = Field(None, description="API key (only provided at creation)")
    subscription_tier: str
    subscription_status: str
    billing_cycle: Optional[str] = None
    trial_ends_at: Optional[datetime] = None
    subscription_starts_at: Optional[datetime] = None
    subscription_ends_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    ai_config: Optional[TenantAIConfigResponse] = None
    capabilities: Optional[TenantCapabilitiesResponse] = None


class TenantListResponse(TenantBase):
    """Schema for tenant list response (without relationships)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscription_tier: str
    subscription_status: str
    billing_cycle: Optional[str] = None
    trial_ends_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
