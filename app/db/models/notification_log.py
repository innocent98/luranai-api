"""NotificationLog — per-notification audit + idempotency record (Stage 3 Slice 5a)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SQLEnum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class NotifEvent(str, enum.Enum):
    SALES_READY_LEAD = "sales_ready_lead"
    MEETING_BOOKED = "meeting_booked"
    QUOTE_GENERATED = "quote_generated"


class NotifChannel(str, enum.Enum):
    EMAIL = "email"
    IN_APP = "in_app"
    SLACK = "slack"
    CRM = "crm"


class NotifStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class NotificationLog(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index("uq_notification_dedupe", "tenant_id", "event", "channel", "entity_id", "recipient", unique=True),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    event: Mapped[NotifEvent] = mapped_column(
        SQLEnum(NotifEvent, name="notif_event", values_callable=lambda x: [e.value for e in x]), nullable=False)
    channel: Mapped[NotifChannel] = mapped_column(
        SQLEnum(NotifChannel, name="notif_channel", values_callable=lambda x: [e.value for e in x]), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[NotifStatus] = mapped_column(
        SQLEnum(NotifStatus, name="notif_status", values_callable=lambda x: [e.value for e in x]),
        default=NotifStatus.PENDING, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    body: Mapped[str | None] = mapped_column(String(400), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<NotificationLog(event={self.event}, channel={self.channel}, status={self.status})>"
