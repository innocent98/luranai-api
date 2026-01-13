"""Conversation and support ticket models."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, Enum as SQLEnum, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from app.db.base import Base, UUIDMixin, TimestampMixin


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


class TicketStatus(str, enum.Enum):
    """Support ticket status."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, enum.Enum):
    """Support ticket priority level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


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
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(SessionStatus, name="session_status"),
        default=SessionStatus.ACTIVE,
        nullable=False,
        index=True,
        comment="Current status of the conversation",
    )
    escalation_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Why this conversation was escalated to humans",
    )
    escalated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the conversation was escalated",
    )
    confidence_scores: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        comment="Array of AI confidence scores over time",
    )
    meta: Mapped[Optional[dict]] = mapped_column(
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
        return f"<ConversationSession(id={self.id}, tenant_id={self.tenant_id}, status={self.status})>"


class Message(Base, UUIDMixin):
    """
    Individual messages in a conversation.

    Tracks user inputs, AI responses, and system messages with metadata
    about knowledge retrieval and confidence scores.
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session this message belongs to",
    )
    role: Mapped[MessageRole] = mapped_column(
        SQLEnum(MessageRole, name="message_role"),
        nullable=False,
        comment="Who sent this message (user/assistant/system)",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Message content",
    )
    retrieved_chunk_ids: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        comment="UUIDs of knowledge chunks used for this response",
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="AI confidence score for this response (0.0-1.0)",
    )
    token_count: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of tokens in this message",
    )
    meta: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional data: {model_version, processing_time, etc.}",
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

    def __repr__(self) -> str:
        return f"<Message(id={self.id}, session_id={self.session_id}, role={self.role})>"


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
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Support agent assigned to this ticket",
    )
    resolution_notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Notes about how the ticket was resolved",
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the ticket was resolved",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        back_populates="tickets",
    )

    def __repr__(self) -> str:
        return f"<Ticket(id={self.id}, ticket_number={self.ticket_number}, status={self.status})>"
