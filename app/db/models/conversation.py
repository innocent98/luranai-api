"""Conversation and support ticket models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedJSON, EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class SessionStatus(str, enum.Enum):
    """Conversation session status."""

    ACTIVE = "active"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class MessageRole(str, enum.Enum):
    """Message role in conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    HUMAN_AGENT = "human_agent"


class TicketStatus(str, enum.Enum):
    """Support ticket status."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    MISSED = "missed"


class TicketPriority(str, enum.Enum):
    """Support ticket priority level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class EscalationAction(str, enum.Enum):
    """Escalation action to take."""

    AUTO_ESCALATE = "auto_escalate"  # Immediate escalation
    OFFER_ESCALATION = "offer_escalation"  # Ask user first
    NO_ESCALATION = "no_escalation"  # AI will handle


class ConversationSession(Base, UUIDMixin, TimestampMixin):
    """
    Tracks chat sessions with users.

    Each session represents a continuous conversation with a user,
    tracking escalation status and AI confidence over time.
    """

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index("ix_conversation_sessions_tenant_status", "tenant_id", "status"),
        Index("ix_conversation_sessions_user", "tenant_id", "user_identifier"),
        Index("ix_conversation_sessions_visitor", "visitor_id", "last_message_at"),
        Index("ix_conversation_sessions_customer", "customer_user_id", "last_message_at"),
        Index("ix_conversation_sessions_source_created", "source", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this conversation belongs to",
    )
    user_identifier: Mapped[str] = mapped_column(
        String(255),
        index=True,
        nullable=False,
        comment="Anonymous or authenticated user ID",
    )
    visitor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visitors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Persistent visitor identity linked to this chat session",
    )
    visitor_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visitor_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Most recent visitor browser session linked to this chat session",
    )
    customer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Authenticated B2C customer linked to this conversation",
    )
    source: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Attribution source — validated against ConversationSource enum at API boundary",
    )
    # JSONB on Postgres (GIN-indexable, containment operators) — JSON on SQLite for tests
    source_meta: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
        comment="Optional attribution context (category_filter, position, page, etc.)",
    )
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(SessionStatus, name="session_status"),
        default=SessionStatus.ACTIVE,
        nullable=False,
        index=True,
        comment="Current status of the conversation",
    )
    escalation_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Why this conversation was escalated to humans",
    )
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the conversation was escalated",
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Support agent currently handling this conversation",
    )
    agent_joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the agent joined the conversation",
    )
    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {user_agent, ip_hash, location, etc.}",
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Timestamp of last message in conversation",
    )
    customer_last_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="When the customer last viewed this conversation",
    )

    # Relationships
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    tickets: Mapped[list["Ticket"]] = relationship(
        "Ticket",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationSession(id={self.id}, tenant_id={self.tenant_id}, status={self.status})>"
        )


class AttachmentDirection(str, enum.Enum):
    """Who supplied the attachment."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class AttachmentStatus(str, enum.Enum):
    """Attachment lifecycle: uploaded (orphan) -> linked to a message."""

    UPLOADED = "uploaded"
    LINKED = "linked"


class AttachmentModeration(str, enum.Enum):
    """Moderation outcome (only passing images are stored)."""

    PASSED = "passed"


class Message(Base, UUIDMixin):
    """
    Individual messages in a conversation.

    Tracks user inputs, AI responses, and system messages with metadata
    about knowledge retrieval and confidence scores.
    """

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session this message belongs to",
    )
    role: Mapped[MessageRole] = mapped_column(
        SQLEnum(MessageRole, name="message_role"),
        nullable=False,
        comment="Who sent this message (user/assistant/system/human_agent)",
    )
    content: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Message content — encrypted at rest",
    )
    retrieved_chunk_ids: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment="UUIDs of knowledge chunks used for this response",
    )
    token_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of tokens in this message",
    )
    meta: Mapped[dict | None] = mapped_column(
        EncryptedJSON,
        nullable=True,
        comment="Additional data — encrypted at rest",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="When this message was created",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        back_populates="messages",
    )
    attachments: Mapped[list["MessageAttachment"]] = relationship(
        "MessageAttachment",
        # Default lazy ("select"), NOT "selectin": a relationship-level eager load
        # makes EVERY `select(Message)` touch message_attachments, which breaks the
        # staged deploy's encryption backfills that query Message before the
        # message_attachments migration runs. Read paths that render attachments
        # eager-load explicitly via selectinload(Message.attachments).
        lazy="select",
        order_by="(MessageAttachment.position, MessageAttachment.created_at)",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Message(id={self.id}, session_id={self.session_id}, role={self.role})>"


class MessageAttachment(Base, UUIDMixin):
    """Image attachment on a chat message (stored on Cloudinary, URL encrypted)."""

    __tablename__ = "message_attachments"

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
        comment="Message this attachment is linked to (null until linked)",
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session this attachment belongs to",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant owning this attachment",
    )
    direction: Mapped[AttachmentDirection] = mapped_column(
        SQLEnum(AttachmentDirection, name="attachment_direction"),
        nullable=False,
        comment="inbound (visitor) or outbound (agent)",
    )
    cloudinary_public_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Cloudinary public id",
    )
    url: Mapped[str] = mapped_column(
        EncryptedString,
        nullable=False,
        comment="Delivery URL — encrypted at rest",
    )
    content_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="MIME type",
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="File size in bytes",
    )
    moderation_status: Mapped[AttachmentModeration] = mapped_column(
        SQLEnum(AttachmentModeration, name="attachment_moderation"),
        nullable=False,
    )
    status: Mapped[AttachmentStatus] = mapped_column(
        SQLEnum(AttachmentStatus, name="attachment_status"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Send order within the message (index in the request's attachment_ids)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="When this attachment was created",
    )

    def __repr__(self) -> str:
        return f"<MessageAttachment(id={self.id}, session_id={self.session_id})>"


class Ticket(Base, UUIDMixin, TimestampMixin):
    """
    Escalated support tickets for human intervention.

    Created when AI cannot handle a request or user explicitly requests
    human support.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
        Index("ix_tickets_assigned_to", "assigned_to"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this ticket belongs to",
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session that created this ticket",
    )
    ticket_number: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
        comment="Human-readable ticket identifier (e.g., TKT-2024-0001)",
    )
    status: Mapped[TicketStatus] = mapped_column(
        SQLEnum(TicketStatus, name="ticket_status"),
        default=TicketStatus.OPEN,
        nullable=False,
        index=True,
        comment="Current ticket status",
    )
    priority: Mapped[TicketPriority] = mapped_column(
        SQLEnum(TicketPriority, name="ticket_priority"),
        default=TicketPriority.MEDIUM,
        nullable=False,
        comment="Ticket priority level",
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="AI-generated summary of the issue",
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Support agent assigned to this ticket",
    )
    resolution_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Notes about how the ticket was resolved",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the ticket was resolved",
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When an agent claimed this escalation from the mobile app",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        back_populates="tickets",
    )

    def __repr__(self) -> str:
        return f"<Ticket(id={self.id}, ticket_number={self.ticket_number}, status={self.status})>"
