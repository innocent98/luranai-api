"""Rating and review models."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin
from app.utils.datetime import utcnow_aware

if TYPE_CHECKING:
    from app.db.models.conversation import ConversationSession
    from app.db.models.tenant import Tenant
    from app.db.models.user import User


class RatingSource:
    """Where a rating / rating request came from (varchar + CHECK, spec N4-D1)."""

    IDLE_PROMPT = "idle_prompt"
    RESOLUTION_PROMPT = "resolution_prompt"
    MANUAL_REQUEST = "manual_request"
    AUTO_RESOLVE = "auto_resolve"
    ALL = (IDLE_PROMPT, RESOLUTION_PROMPT, MANUAL_REQUEST, AUTO_RESOLVE)
    REQUESTABLE = (MANUAL_REQUEST, AUTO_RESOLVE)


SESSION_RATING_SOURCE_CHECK = (
    "source IN ('idle_prompt', 'resolution_prompt', 'manual_request', 'auto_resolve')"
)
RATING_REQUEST_SOURCE_CHECK = "source IN ('manual_request', 'auto_resolve')"


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
    Customer satisfaction rating for a chat session.

    One rating per (session, rated party): the AI/no-human party (rated_agent_id NULL)
    at most once, and each human agent at most once (spec N4-D1).
    """

    __tablename__ = "session_ratings"
    __table_args__ = (
        Index("ix_session_ratings_tenant_session", "tenant_id", "session_id"),
        Index("ix_session_ratings_tenant_created", "tenant_id", "created_at"),
        Index("ix_session_ratings_score", "tenant_id", "score"),
        Index(
            "ix_session_ratings_tenant_agent_created", "tenant_id", "rated_agent_id", "created_at"
        ),
        Index(
            "uq_session_ratings_session_agent",
            "session_id",
            "rated_agent_id",
            unique=True,
            postgresql_where=text("rated_agent_id IS NOT NULL"),
            sqlite_where=text("rated_agent_id IS NOT NULL"),
        ),
        Index(
            "uq_session_ratings_session_ai",
            "session_id",
            unique=True,
            postgresql_where=text("rated_agent_id IS NULL"),
            sqlite_where=text("rated_agent_id IS NULL"),
        ),
        UniqueConstraint("rating_request_id", name="uq_session_ratings_rating_request_id"),
        CheckConstraint(SESSION_RATING_SOURCE_CHECK, name="ck_session_ratings_source"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant this rating belongs to",
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Conversation session being rated",
    )

    score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Rating score 1-5 (1=Poor, 2=Fair, 3=Good, 4=Very Good, 5=Excellent)",
    )

    comment: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Optional feedback comment from customer — encrypted at rest (N4-D7)",
    )

    user_identifier: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User who submitted rating (for verification)",
    )

    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {ai_helpful, issues_faced, etc.}",
    )

    rated_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="Human agent rated; NULL = AI / no human (kept on purge_user, N4-D10)",
    )

    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RatingSource.IDLE_PROMPT,
        comment="idle_prompt | resolution_prompt | manual_request | auto_resolve",
    )

    rating_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rating_requests.id", ondelete="SET NULL"),
        nullable=True,
        comment="The token-bearing request this rating fulfilled (one rating per request)",
    )

    # Relationships
    session: Mapped["ConversationSession"] = relationship(
        "ConversationSession",
        backref="ratings",  # session.ratings (one per rated party)
    )


class RatingRequest(Base, UUIDMixin):
    """A one-time, token-gated request for the customer to rate a human agent (N4-D2)."""

    __tablename__ = "rating_requests"
    __table_args__ = (
        Index("ix_rating_requests_session_agent_created", "session_id", "agent_id", "created_at"),
        Index("ix_rating_requests_tenant_created", "tenant_id", "created_at"),
        CheckConstraint(RATING_REQUEST_SOURCE_CHECK, name="ck_rating_requests_source"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        comment="Snapshot of Ticket.assigned_to at request time (kept on purge_user, N4-D10)",
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="NULL for auto_resolve; NULLed by purge_user",
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="sha256(token).hexdigest(); the plaintext token is never stored here",
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow_aware, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="created_at + 7 days; set to now() when superseded",
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
        SQLEnum(
            FeedbackCategory,
            name="feedback_category",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
        comment="Type of feedback",
    )

    satisfaction_score: Mapped[int | None] = mapped_column(
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
        comment="Status: open, acknowledged, in_progress, resolved, closed",
    )

    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who submitted feedback",
    )

    meta: Mapped[dict | None] = mapped_column(
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


class PublicFeedback(Base, UUIDMixin, TimestampMixin):
    """
    Public feedback from landing page visitors (no authentication required).

    Allows anyone to provide feedback about Luran platform from the landing page,
    helping gather insights from potential customers and general users.
    """

    __tablename__ = "public_feedback"
    __table_args__ = (
        Index("ix_public_feedback_category_created", "category", "created_at"),
        Index("ix_public_feedback_status", "status"),
        Index("ix_public_feedback_email", "email"),
    )

    category: Mapped[FeedbackCategory] = mapped_column(
        SQLEnum(
            FeedbackCategory,
            name="feedback_category",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
        comment="Type of feedback",
    )

    satisfaction_score: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="1-5 rating (optional)",
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

    # Optional contact information
    name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Submitter name (optional)",
    )

    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Contact email for follow-up (optional)",
    )

    company: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Company/organization name (optional)",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="open",
        nullable=False,
        comment="Status: open, acknowledged, in_progress, resolved, closed",
    )

    meta: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Additional context: {referral_source, page_url, user_agent, etc.}",
    )
