"""Email notification tracking models."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSON

from app.db.base import Base, UUIDMixin, TimestampMixin


class EmailNotification(Base, UUIDMixin, TimestampMixin):
    """
    Track sent email notifications to prevent duplicates and enable audit trail.

    Supports automated notifications like trial expiration reminders,
    subscription confirmations, and payment failure alerts.
    """

    __tablename__ = "email_notifications"
    __table_args__ = (
        Index("ix_email_notifications_tenant_type", "tenant_id", "notification_type"),
        Index("ix_email_notifications_status_created", "status", "created_at"),
        Index("ix_email_notifications_reference", "reference_type", "reference_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this notification relates to",
    )

    # Email details
    notification_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Type: trial_expiring, subscription_confirmed, payment_failed, etc.",
    )
    recipient_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Email address of recipient",
    )
    subject: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Email subject line",
    )

    # Reference context
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        nullable=True,
        comment="Payment ID, subscription ID, etc.",
    )
    reference_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Type of reference: payment, subscription, etc.",
    )

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
        comment="Status: pending, sent, failed, skipped",
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the email was successfully sent",
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if sending failed",
    )

    # Metadata
    template_data: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Template variables used in the email",
    )

    def __repr__(self) -> str:
        return (
            f"<EmailNotification(id={self.id}, type={self.notification_type}, "
            f"status={self.status}, recipient={self.recipient_email})>"
        )
