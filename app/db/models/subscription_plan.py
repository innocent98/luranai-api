"""
Subscription Plan Model

This module defines the SubscriptionPlan model for database-driven pricing configuration.
"""

from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


# Columns that describe the plan itself, not a tenant capability.
PLAN_META_FIELDS = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        "tier",
        "display_name",
        "description",
        "monthly_price_kobo",
        "annual_price_kobo",
        "features",
        "is_active",
        "sort_order",
    }
)

# Capability columns that live on the plan only (no tenant_capabilities column).
PLAN_ONLY_CAPABILITY_FIELDS = frozenset({"max_response_tokens", "max_nodes_per_flow"})


class SubscriptionPlan(Base, UUIDMixin, TimestampMixin):
    """
    Database model for subscription plans with pricing and capabilities.

    Replaces hardcoded PRICING and TIER_CONFIGS constants with database-driven configuration.
    """

    __tablename__ = "subscription_plans"

    # Identification
    tier: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)

    # Pricing (all prices in kobo)
    monthly_price_kobo: Mapped[int] = mapped_column(Integer, nullable=False)
    annual_price_kobo: Mapped[int] = mapped_column(Integer, nullable=False)

    # Capabilities (matching TIER_CONFIGS structure)
    can_access_analytics: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_knowledge_sources: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    can_use_url_sources: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_knowledge_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    max_conversations_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    max_team_members: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    max_response_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_automation_actions: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    max_automation_executions_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    can_use_ai_chat_automation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_use_lookup_chains: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Sales and engagement gates (pricing and entitlements, 2026-10-28)
    can_use_sales_close: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # meeting booking + quotes
    can_use_crm_notifications: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # Slack + CRM webhook
    can_use_screen_recording: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    can_export_ratings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # admin ratings breakdown + CSV
    # WhatsApp is plan-driven (every tier includes it; a platform admin can still switch it off or
    # on for one tenant through an override). `WHATSAPP_ENABLED` stays the global kill switch.
    can_use_whatsapp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # None = unlimited (no column default on purpose: the ORM would turn an explicit None into the
    # default); 0 or less switches WhatsApp off for the plan.
    max_whatsapp_numbers: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Offerings catalog (2026-10-30): every plan has it, the cap is the lever. `max_offerings`
    # None = unlimited (no column default on purpose); photos are per item.
    max_offerings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_offering_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )

    # Flow capabilities
    max_flows: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    max_nodes_per_flow: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    can_use_webhooks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_webhooks_per_flow: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    max_audit_pages_per_crawl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_audits_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Marketing
    features: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )  # List of feature descriptions

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_subscription_plans_tier", "tier", unique=True),
        Index("ix_subscription_plans_active_sort", "is_active", "sort_order"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to dictionary representation."""
        return {
            "id": str(self.id),
            "tier": self.tier,
            "display_name": self.display_name,
            "description": self.description,
            "monthly_price_kobo": self.monthly_price_kobo,
            "annual_price_kobo": self.annual_price_kobo,
            "monthly_price_naira": self.monthly_price_kobo / 100,
            "annual_price_naira": self.annual_price_kobo / 100,
            "capabilities": self.get_capabilities(),
            "features": self.features,
            "is_active": self.is_active,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def capability_columns(cls) -> tuple[str, ...]:
        """Every capability column of the plan, derived from the table so a new one is never missed."""
        return tuple(c.key for c in cls.__table__.columns if c.key not in PLAN_META_FIELDS)

    def get_capabilities(self) -> dict[str, Any]:
        """Capabilities dict (TIER_CONFIGS structure), derived from the plan's columns."""
        return {name: getattr(self, name) for name in self.capability_columns()}
