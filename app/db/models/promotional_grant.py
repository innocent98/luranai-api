"""Promotional grant database models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class PromotionalGrant(Base, UUIDMixin, TimestampMixin):
    """Time-bounded subscription tier elevation for marketing campaigns and onboarding."""

    __tablename__ = "promotional_grants"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    campaign_code: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    granted_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grant_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    previous_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_billing_cycle: Mapped[str | None] = mapped_column(String(20), nullable=True)
    previous_trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_subscription_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    granted_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True
    )
    granted_by_admin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PromotionalGrant(id={self.id}, tenant_id={self.tenant_id}, "
            f"campaign={self.campaign_code}, tier={self.granted_tier}, status={self.status})>"
        )


class AutoSignupGrantConfig(Base, UUIDMixin):
    """Singleton config controlling auto-grant on signup. Exactly one row exists."""

    __tablename__ = "auto_signup_grant_config"

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    campaign_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    granted_tier: Mapped[str | None] = mapped_column(String(50), nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_admin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<AutoSignupGrantConfig(enabled={self.enabled}, campaign={self.campaign_code})>"
