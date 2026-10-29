"""WhatsApp channel models (linked-device sidecar, demo slice).

Tenant-scoped. The phone number and the contact reference are encrypted at rest; the
lookup keys (`phone_hmac`, `contact_hmac`) are peppered HMACs (`app.channels.identity`),
never the unsalted `hash_for_lookup`. The pairing code is deliberately NOT a column: it
is returned once to the dashboard and never persisted.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class WhatsAppProvider(str, enum.Enum):
    LINKED_DEVICE = "linked_device"  # option A: Baileys sidecar (this slice)
    CLOUD_API = "cloud_api"  # option B: Meta Cloud API (stub only)


class WhatsAppStatus(str, enum.Enum):
    """Mirrors the sidecar contract; the backend only ever flips it from webhooks."""

    AWAITING_PAIR = "awaiting_pair"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    LOGGED_OUT = "logged_out"
    FAILED = "failed"


# Statuses that hold a live socket in the sidecar (count against the capacity guard).
LIVE_STATUSES = (
    WhatsAppStatus.AWAITING_PAIR.value,
    WhatsAppStatus.CONNECTING.value,
    WhatsAppStatus.CONNECTED.value,
    WhatsAppStatus.RECONNECTING.value,
)


class WhatsAppConnection(Base, UUIDMixin, TimestampMixin):
    """One linked WhatsApp number belonging to one tenant."""

    __tablename__ = "whatsapp_connections"
    __table_args__ = (Index("ix_whatsapp_connections_tenant_status", "tenant_id", "status"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WhatsAppProvider.LINKED_DEVICE.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=WhatsAppStatus.AWAITING_PAIR.value
    )
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone_e164: Mapped[str] = mapped_column(
        EncryptedString, nullable=False, comment="Digits incl. country code, no '+'; encrypted"
    )
    phone_hmac: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="Peppered HMAC of the phone digits: one number belongs to one tenant globally",
    )
    session_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="Opaque sidecar session id"
    )
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    consent_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_disconnect_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_disconnect_reason: Mapped[str | None] = mapped_column(String(64))
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppChat(Base, UUIDMixin, TimestampMixin):
    """A 1:1 chat between a connected number and one customer."""

    __tablename__ = "whatsapp_chats"
    __table_args__ = (
        UniqueConstraint("connection_id", "contact_hmac", name="uq_whatsapp_chat_contact"),
        Index("ix_whatsapp_chats_connection_phone", "connection_id", "phone_hmac"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("whatsapp_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    contact_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_ref: Mapped[str] = mapped_column(
        EncryptedString, nullable=False, comment="Opaque sidecar chat ref (may be a LID); encrypted"
    )
    phone_hmac: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Peppered HMAC of the contact's phone digits, once the sidecar resolved them",
    )
    visitor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visitors.id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    human_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Owner typed from the phone: AI paused until this time"
    )
    rating_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rating_requests.id", ondelete="SET NULL"),
        nullable=True,
        comment="Open rating request whose prompt was delivered to this chat (a 1-5 reply answers it)",
    )
    rating_request_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Copy of the request expiry (7 days after it was minted)"
    )


class WhatsAppInboundMessage(Base, UUIDMixin):
    """Idempotency ledger: UNIQUE(connection_id, provider_message_id) is the dedupe."""

    __tablename__ = "whatsapp_inbound_messages"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "provider_message_id", name="uq_whatsapp_inbound_provider_message"
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("whatsapp_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WhatsAppContactExclusion(Base, UUIDMixin, TimestampMixin):
    """A contact Luran must ignore entirely on ONE connection (the tenant's personal contacts).

    Minimal by design: peppered HMAC keys only, plus an encrypted display mask and an optional
    encrypted label. The full number is never stored. `lid_hmac` is learned: the sidecar's chat
    ref is often a LID (not a phone), so the first inbound that carries both lets us recognise
    the same contact by ref afterwards (owner-typed events carry only the ref).
    """

    __tablename__ = "whatsapp_contact_exclusions"
    __table_args__ = (
        UniqueConstraint("connection_id", "phone_hmac", name="uq_whatsapp_exclusion_phone"),
        Index("ix_whatsapp_exclusions_connection_lid", "connection_id", "lid_hmac"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("whatsapp_connections.id", ondelete="CASCADE"), index=True, nullable=False
    )
    phone_hmac: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Peppered HMAC of the phone digits the tenant typed"
    )
    lid_hmac: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Peppered HMAC of the LID chat ref, learned on inbound"
    )
    phone_masked: Mapped[str] = mapped_column(
        EncryptedString, nullable=False, comment="Display mask only (+234•••••6789); encrypted"
    )
    label: Mapped[str | None] = mapped_column(
        EncryptedString, nullable=True, comment="Tenant-chosen name, optional; encrypted"
    )
