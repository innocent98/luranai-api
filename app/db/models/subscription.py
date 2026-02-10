"""Subscription-related database models."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class SubscriptionHistory(Base, UUIDMixin, TimestampMixin):
    """Audit trail for subscription changes.

    Tracks all subscription modifications for compliance, debugging,
    analytics, and customer support. Records what changed, who made
    the change, and why.
    """

    __tablename__ = "subscription_history"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant whose subscription changed",
    )

    # What changed
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Action type: upgrade, downgrade, extend_trial, override, cancel, reactivate",
    )
    previous_tier: Mapped[str | None] = mapped_column(
        String(50), comment="Subscription tier before change"
    )
    new_tier: Mapped[str | None] = mapped_column(
        String(50), comment="Subscription tier after change"
    )
    previous_status: Mapped[str | None] = mapped_column(
        String(50), comment="Subscription status before change"
    )
    new_status: Mapped[str | None] = mapped_column(
        String(50), comment="Subscription status after change"
    )

    # Who made the change
    changed_by_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Actor type: platform_admin, webhook, system, tenant_admin",
    )
    changed_by_id: Mapped[UUID | None] = mapped_column(
        comment="Admin/user ID who initiated the change"
    )
    changed_by_name: Mapped[str | None] = mapped_column(
        String(255), comment="Name of person/system who made the change"
    )

    # Why and context
    reason: Mapped[str | None] = mapped_column(Text, comment="Human-readable reason for the change")
    event_metadata: Mapped[dict | None] = mapped_column(
        JSON, comment="Additional context data (JSON)"
    )
    payment_reference: Mapped[str | None] = mapped_column(
        String(100), index=True, comment="Payment transaction reference if applicable"
    )

    def __repr__(self) -> str:
        return (
            f"<SubscriptionHistory(id={self.id}, tenant_id={self.tenant_id}, "
            f"action={self.action}, {self.previous_tier} -> {self.new_tier})>"
        )
