"""Meeting — a native booking made via the chat-widget meeting picker (Stage 3 Slice 3)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SQLEnum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from app.db.base import Base, TimestampMixin, UUIDMixin


class MeetingStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class Meeting(Base, UUIDMixin, TimestampMixin):
    """A booked meeting slot, owned by a lead. No external calendar in this slice."""

    __tablename__ = "meetings"
    __table_args__ = (
        # Defence-in-depth against double-booking a live slot for a tenant.
        Index(
            "uq_meeting_tenant_slot_scheduled",
            "tenant_id",
            "scheduled_at",
            unique=True,
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    visitor_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    use_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        SQLEnum(
            MeetingStatus, name="meeting_status", values_callable=lambda x: [e.value for e in x]
        ),
        default=MeetingStatus.SCHEDULED,
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Meeting(id={self.id}, tenant_id={self.tenant_id}, scheduled_at={self.scheduled_at})>"
        )
