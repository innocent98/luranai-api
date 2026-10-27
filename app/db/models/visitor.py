"""Visitor identity and visitor session models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString, hash_for_lookup
from app.db.base import Base, TimestampMixin, UUIDMixin


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist enum values instead of enum member names."""
    return [member.value for member in enum_cls]


class VisitorSource(str, enum.Enum):
    """Customer-facing entry points that can create a visitor."""

    WIDGET = "widget"
    SUPPORT_PAGE = "support_page"
    WHATSAPP = "whatsapp"


class DeviceType(str, enum.Enum):
    """Normalized device categories for visitor sessions."""

    DESKTOP = "desktop"
    MOBILE = "mobile"
    TABLET = "tablet"


class Visitor(Base, UUIDMixin, TimestampMixin):
    """Persistent lightweight customer identity scoped to a tenant."""

    __tablename__ = "visitors"
    __table_args__ = (
        Index("ix_visitors_tenant_token", "tenant_id", "public_token", unique=True),
        Index("ix_visitors_tenant_last_seen", "tenant_id", "last_seen_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this visitor belongs to",
    )
    public_token: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="Opaque browser-stored token used to resolve the visitor",
    )
    name: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Optional visitor name — encrypted at rest",
    )
    email: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Optional visitor email — encrypted at rest",
    )
    email_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="SHA-256 hash of email for lookups",
    )
    phone: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Optional visitor phone — encrypted at rest",
    )
    phone_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="SHA-256 hash of phone for lookups",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When this visitor was first seen",
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Most recent activity timestamp for this visitor",
    )
    created_from: Mapped[VisitorSource] = mapped_column(
        SQLEnum(
            VisitorSource,
            name="visitor_source",
            values_callable=_enum_values,
        ),
        nullable=False,
        comment="Entry point used when visitor was first created",
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Optional lightweight visitor metadata such as locale and referrer",
    )
    is_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this visitor is blocked from public support interactions",
    )

    visitor_sessions: Mapped[list["VisitorSession"]] = relationship(
        "VisitorSession",
        back_populates="visitor",
        cascade="all, delete-orphan",
        order_by="VisitorSession.started_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Visitor(id={self.id}, tenant_id={self.tenant_id})>"


class VisitorSession(Base, UUIDMixin):
    """Time-bounded browser/device activity window for a visitor."""

    __tablename__ = "visitor_sessions"
    __table_args__ = (
        Index("ix_visitor_sessions_visitor_started", "visitor_id", "started_at"),
        Index("ix_visitor_sessions_tenant_last_activity", "tenant_id", "last_activity_at"),
        Index("ix_visitor_sessions_token", "session_token", unique=True),
    )

    visitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visitors.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Visitor this session belongs to",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Denormalized tenant reference for filtering",
    )
    session_token: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        comment="Opaque browser-stored token for the visitor session",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the visitor session started",
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Most recent activity timestamp for the visitor session",
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the visitor session ended, if explicitly closed",
    )
    entry_point: Mapped[VisitorSource] = mapped_column(
        SQLEnum(
            VisitorSource,
            name="visitor_session_entry_point",
            values_callable=_enum_values,
        ),
        nullable=False,
        comment="Entry point used for this visitor session",
    )
    user_agent: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Raw user-agent string — encrypted at rest",
    )
    ip_address: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="IP address — encrypted at rest",
    )
    ip_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="SHA-256 hash of IP address for lookups",
    )
    device_type: Mapped[DeviceType | None] = mapped_column(
        SQLEnum(
            DeviceType,
            name="visitor_device_type",
            values_callable=_enum_values,
        ),
        nullable=True,
        comment="Normalized device type if parsed from user-agent",
    )
    browser_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Parsed browser name if available",
    )
    os_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Parsed operating system name if available",
    )
    referrer_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Referrer where the support interaction was launched from",
    )
    landing_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Page URL where the visitor started the support interaction",
    )
    locale: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Browser locale if available",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When this visitor session record was created",
    )

    visitor: Mapped["Visitor"] = relationship("Visitor", back_populates="visitor_sessions")

    def __repr__(self) -> str:
        return f"<VisitorSession(id={self.id}, visitor_id={self.visitor_id})>"


@event.listens_for(Visitor, "before_insert")
@event.listens_for(Visitor, "before_update")
def _populate_visitor_hashes(mapper, connection, target):
    """Auto-populate email_hash and phone_hash from email/phone."""
    if target.email:
        target.email_hash = hash_for_lookup(target.email)
    elif target.email is None:
        target.email_hash = None
    if target.phone:
        target.phone_hash = hash_for_lookup(target.phone)
    elif target.phone is None:
        target.phone_hash = None


@event.listens_for(VisitorSession, "before_insert")
@event.listens_for(VisitorSession, "before_update")
def _populate_visitor_session_hashes(mapper, connection, target):
    """Auto-populate ip_hash from ip_address."""
    if target.ip_address:
        target.ip_hash = hash_for_lookup(target.ip_address)
    elif target.ip_address is None:
        target.ip_hash = None
