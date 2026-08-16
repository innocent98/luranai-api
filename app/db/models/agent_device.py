"""AgentDevice — one row per (agent, mobile device) for escalation push delivery."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class DevicePlatform(str, enum.Enum):
    """Mobile platform a registered device runs."""

    ANDROID = "android"
    IOS = "ios"


class AgentDevice(Base, UUIDMixin, TimestampMixin):
    """An agent's registered push target.

    An agent may have several devices; every one of them rings. Rows are removed
    on logout and pruned lazily when FCM reports the token as UNREGISTERED.
    """

    __tablename__ = "agent_devices"
    __table_args__ = (
        UniqueConstraint("user_id", "device_token", name="uq_agent_device_user_token"),
        Index("ix_agent_devices_tenant_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Agent this device belongs to",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant of the owning agent (denormalised for tenant-scoped fanout)",
    )
    device_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="FCM registration token — never returned by any API or written to logs",
    )
    platform: Mapped[DevicePlatform] = mapped_column(
        SQLEnum(
            DevicePlatform,
            name="device_platform",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time the app re-registered this token",
    )

    def __repr__(self) -> str:
        return f"<AgentDevice(user_id={self.user_id}, platform={self.platform})>"
