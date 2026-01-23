"""Rating and review models."""

import enum
import uuid
from typing import Optional

from sqlalchemy import Integer, JSON, String, Text, Enum as SQLEnum, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from app.db.base import Base, UUIDMixin, TimestampMixin


class FeedbackCategory(str, enum.Enum):
    """Types of feedback tenants can provide about Luran."""

    FEATURE_REQUEST = "feature_request"
    BUG_REPORT = "bug_report"
    PERFORMANCE = "performance"
    PLATFORM_SATISFACTION = "platform_satisfaction"
    DOCUMENTATION = "documentation"
    PRICING = "pricing"
    GENERAL = "general"


class SessionRating(Base, UUIDMixin, TimestampMixin):
    """
    Customer satisfaction rating for chat sessions.

    Allows customers to rate their experience after chatting with AI,
    helping companies identify knowledge gaps and improve quality.
    """

    __tablename__ = "session_ratings"
    __table_args__ = (
        Index("ix_session_ratings_tenant_session", "tenant_id", "session_id"),
        Index("ix_session_ratings_tenant_created", "tenant_id", "created_at"),
        Index("ix_session_ratings_score", "tenant_id", "score"),
        # Prevent duplicate ratings for same session
        UniqueConstraint("session_id", name="uq_session_rating_per_session"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this rating belongs to",
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        unique=True,  # One rating per session
        index=True,
        nullable=False,
        comment="Conversation session being rated",
    )

    score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Rating score 1-5 (1=Poor, 2=Fair, 3=Good, 4=Very Good, 5=Excellent)",
    )

    comment: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional feedback comment from customer",
    )

    user_identifier: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User who submitted rating (for verification)",
    )

    meta: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {ai_helpful, issues_faced, etc.}",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        backref="rating",  # session.rating (one-to-one)
    )


class PlatformFeedback(Base, UUIDMixin, TimestampMixin):
    """
    Tenant feedback about Luran platform.

    Allows companies to rate Luran, report bugs, request features,
    and provide suggestions for platform improvements.
    """

    __tablename__ = "platform_feedback"
    __table_args__ = (
        Index("ix_platform_feedback_tenant_category", "tenant_id", "category"),
        Index("ix_platform_feedback_tenant_created", "tenant_id", "created_at"),
        Index("ix_platform_feedback_status", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant providing feedback",
    )

    category: Mapped[FeedbackCategory] = mapped_column(
        SQLEnum(FeedbackCategory, name="feedback_category", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
        comment="Type of feedback",
    )

    satisfaction_score: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="1-5 rating (only for platform_satisfaction category)",
    )

    title: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Feedback title/summary",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Detailed feedback description",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="open",
        nullable=False,
        index=True,
        comment="Status: open, acknowledged, in_progress, resolved, closed",
    )

    submitted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who submitted feedback",
    )

    meta: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {priority, affected_features, etc.}",
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        backref="platform_feedback",
    )

    submitter: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[submitted_by],
    )
