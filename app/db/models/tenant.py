"""Tenant models for multi-tenant architecture."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.db.models.payment import PaymentTransaction
    from app.db.models.processing_job import ProcessingJob


class TenantStatus(str, enum.Enum):
    """Tenant status enum."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class Tenant(Base, UUIDMixin, TimestampMixin):
    """
    Core tenant/product entity representing a product using the AI support platform.

    Examples: weysure, aegiscribe, earn_hub
    """

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
        comment="Unique identifier name (e.g., 'weysure', 'aegiscribe')",
    )
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable name (e.g., 'Weysure Insurance')",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Brief description of the product/tenant",
    )
    long_description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Extended company and support description for hosted support pages",
    )
    support_headline: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
        comment="Short hosted-support headline shown on the public support page",
    )
    support_subheadline: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Supporting hosted-support subheadline shown on the public support page",
    )
    support_hours: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Business support hours shown on the public support page",
    )
    support_response_time: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Typical support response-time note shown on the public support page",
    )
    support_footer_note: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Small hosted-support footer note shown on the public support page",
    )
    is_publicly_listed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether this tenant should appear in the public business directory",
    )
    is_featured: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="Featured businesses appear first in the public directory",
    )
    category: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        default=None,
        comment="Industry category for public directory filtering — validated against TenantCategory enum at API boundary",
    )
    public_slug: Mapped[str | None] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=True,
        comment="Public support slug used for hosted support pages",
    )
    status: Mapped[TenantStatus] = mapped_column(
        SQLEnum(TenantStatus, name="tenant_status"),
        default=TenantStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    api_key_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Hashed API key for tenant authentication",
    )

    # Widget Configuration
    widget_primary_color: Mapped[str | None] = mapped_column(
        String(7),
        nullable=True,
        default="#2563eb",
        comment="Primary color for widget (hex format)",
    )
    widget_accent_color: Mapped[str | None] = mapped_column(
        String(7),
        nullable=True,
        default="#10b981",
        comment="Accent color for widget (hex format)",
    )
    widget_logo_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="URL to company logo for widget",
    )
    widget_greeting_message: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        default="Hi! How can we help you today?",
        comment="Greeting message shown in widget",
    )
    widget_position: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        default="bottom-right",
        comment="Widget position: bottom-right, bottom-left, top-right, top-left",
    )
    widget_show_unread_badge: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Show unread message badge on widget button",
    )
    widget_allow_file_upload: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Allow file uploads in widget chat",
    )

    # Subscription & Trial Tracking
    subscription_tier: Mapped[str] = mapped_column(
        String(50),
        default="trial",
        nullable=False,
        index=True,
        comment="Subscription tier: trial, lite, professional, enterprise",
    )
    subscription_status: Mapped[str] = mapped_column(
        String(50),
        default="trialing",
        nullable=False,
        index=True,
        comment="Status: trialing, active, past_due, canceled, trial_expired",
    )
    billing_cycle: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Billing cycle: monthly, annual (null for trial)",
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="When 30-day trial expires (set on signup)",
    )
    subscription_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When paid subscription started",
    )
    subscription_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When subscription ends (canceled or expired)",
    )

    # Relationships
    ai_config: Mapped[Optional["TenantAIConfig"]] = relationship(
        "TenantAIConfig",
        back_populates="tenant",
        uselist=False,
        cascade="all, delete-orphan",
    )
    capabilities: Mapped[Optional["TenantCapabilities"]] = relationship(
        "TenantCapabilities",
        back_populates="tenant",
        uselist=False,
        cascade="all, delete-orphan",
    )
    processing_jobs: Mapped[list["ProcessingJob"]] = relationship(
        "ProcessingJob",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    payment_transactions: Mapped[list["PaymentTransaction"]] = relationship(
        "PaymentTransaction",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Tenant(id={self.id}, name={self.name}, status={self.status})>"


class TenantAIConfig(Base, UUIDMixin, TimestampMixin):
    """
    AI behavior configuration for each tenant.

    Controls brand voice, allowed/blocked topics, escalation triggers,
    and response generation parameters.
    """

    __tablename__ = "tenant_ai_configs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
        comment="One config per tenant",
    )

    # Brand Voice Configuration
    brand_voice: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="JSON: {tone, greeting_style, emoji_usage, formality_level}",
    )

    # Topic Control
    allowed_topics: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Array of allowed topics (null = all topics allowed)",
    )
    blocked_topics: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Array of topics to avoid",
    )

    # Escalation Configuration
    escalation_keywords: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Array of keywords that trigger human escalation",
    )
    # Response Generation Parameters
    max_response_tokens: Mapped[int] = mapped_column(
        Integer,
        default=450,
        nullable=False,
        comment="Maximum tokens for AI response",
    )
    grounding_requirement: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether AI must ground responses in knowledge base",
    )
    grounding_mode: Mapped[str] = mapped_column(
        String(20),
        default="balanced",
        nullable=False,
        comment="Grounding strictness: strict, balanced, flexible",
    )
    low_confidence_action: Mapped[str] = mapped_column(
        String(20),
        default="clarify",
        nullable=False,
        comment="Action when retrieval confidence is low: answer, clarify, escalate, refuse",
    )
    no_retrieval_action: Mapped[str] = mapped_column(
        String(20),
        default="clarify",
        nullable=False,
        comment="Action when no knowledge is retrieved: answer, clarify, escalate, refuse",
    )
    require_email_before_escalation: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Require capturing customer email before escalating to human support",
    )
    require_name_before_escalation: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Require capturing customer name before escalating to human support",
    )
    collect_name_before_chat: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Collect customer name before starting chat",
    )
    collect_email_before_chat: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Collect customer email before starting chat",
    )

    # Custom Instructions
    custom_instructions: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Additional prompt instructions specific to this tenant",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="ai_config",
    )

    def __repr__(self) -> str:
        return f"<TenantAIConfig(tenant_id={self.tenant_id}, max_response_tokens={self.max_response_tokens})>"


class TenantCapabilities(Base, UUIDMixin, TimestampMixin):
    """
    Feature flags and usage limits for each tenant.

    Controls what actions the tenant's AI agent can perform and resource limits.
    """

    __tablename__ = "tenant_capabilities"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
        comment="One set of capabilities per tenant",
    )

    # Feature Permissions
    can_create_tickets: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Can escalate to human support tickets",
    )
    can_access_analytics: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Can view analytics dashboard",
    )
    can_upload_knowledge: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Can upload knowledge base documents",
    )

    # Resource Limits
    max_knowledge_mb: Mapped[int] = mapped_column(
        Integer,
        default=100,
        nullable=False,
        comment="Maximum knowledge base storage in MB",
    )
    max_conversations_per_month: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Monthly conversation limit (null = unlimited)",
    )

    # Combined Knowledge Sources (Files + URLs)
    max_knowledge_sources: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Total knowledge sources: files + URLs combined (null = unlimited)",
    )
    can_use_url_sources: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Can scrape webpage URLs as knowledge sources",
    )

    # Team Member Limits
    max_team_members: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum team members allowed (null = unlimited)",
    )
    max_automation_actions: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum automation actions allowed (null = unlimited)",
    )
    max_automation_executions_per_month: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum automation executions allowed per month (null = unlimited)",
    )
    can_use_ai_chat_automation: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Can use automation actions directly from normal AI chat",
    )
    can_use_lookup_chains: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Can use lookup-first automation chains in AI chat",
    )
    can_use_webhooks: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Can use webhook/direct API call nodes in flows",
    )
    max_flows: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum flows allowed (null = unlimited)",
    )
    max_webhooks_per_flow: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum webhook nodes allowed per flow (null = unlimited)",
    )

    # Override Tracking
    override_flags: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
        comment="Track manual capability overrides: {field_name: true} to persist through tier upgrades",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="capabilities",
    )

    def __repr__(self) -> str:
        return f"<TenantCapabilities(tenant_id={self.tenant_id}, max_knowledge_mb={self.max_knowledge_mb})>"
