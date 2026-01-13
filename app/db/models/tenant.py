"""Tenant models for multi-tenant architecture."""

import enum
import uuid
from typing import Optional

from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from app.db.base import Base, UUIDMixin, TimestampMixin


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
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Brief description of the product/tenant",
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
    allowed_topics: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        comment="Array of allowed topics (null = all topics allowed)",
    )
    blocked_topics: Mapped[Optional[list]] = mapped_column(
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
    confidence_threshold: Mapped[float] = mapped_column(
        Float,
        default=0.55,
        nullable=False,
        comment="Minimum confidence score to return AI response (0.0-1.0)",
    )

    # Response Generation Parameters
    max_response_tokens: Mapped[int] = mapped_column(
        Integer,
        default=400,
        nullable=False,
        comment="Maximum tokens for AI response",
    )
    grounding_requirement: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether AI must ground responses in knowledge base",
    )

    # Custom Instructions
    custom_instructions: Mapped[Optional[str]] = mapped_column(
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
        return f"<TenantAIConfig(tenant_id={self.tenant_id}, confidence_threshold={self.confidence_threshold})>"


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
    max_conversations_per_month: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Monthly conversation limit (null = unlimited)",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="capabilities",
    )

    def __repr__(self) -> str:
        return f"<TenantCapabilities(tenant_id={self.tenant_id}, max_knowledge_mb={self.max_knowledge_mb})>"
