"""Platform admin email campaign models."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class PlatformEmailCampaign(Base, UUIDMixin, TimestampMixin):
    """Campaign metadata for platform-admin broadcast emails."""

    __tablename__ = "platform_email_campaigns"
    __table_args__ = (
        Index("ix_platform_email_campaigns_status_created", "status", "created_at"),
        Index("ix_platform_email_campaigns_admin_created", "created_by_admin_id", "created_at"),
    )

    created_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_admins.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    audience_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    template_key: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="queued",
        server_default="queued",
        index=True,
    )
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    preheader: Mapped[str | None] = mapped_column(String(255), nullable=True)
    heading: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cta_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cta_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    audience_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlatformEmailCampaignRecipient(Base, UUIDMixin, TimestampMixin):
    """Resolved recipients for a platform email campaign."""

    __tablename__ = "platform_email_campaign_recipients"
    __table_args__ = (
        Index(
            "ix_platform_email_campaign_recipients_campaign_status",
            "campaign_id",
            "status",
        ),
        Index(
            "ix_platform_email_campaign_recipients_email_campaign",
            "recipient_email",
            "campaign_id",
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_email_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
