"""What counts as a conversation for plan limits, and when a returning customer continues one.

One conversation = one `conversation_sessions` row created this calendar month (UTC). A customer
who comes back inside `CONVERSATION_REUSE_WINDOW_HOURS` of the session's last message continues
that same row (reopening it if it was abandoned or resolved), so it is not counted twice. Beyond
the window a new row is created, which is what makes it count.

Every monthly count and every limit check goes through here; do not re-implement the query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.tenant import TenantCapabilities
from app.utils.datetime import utcnow_naive

# Greetings the widget sends when it opens. They never reopen a resolved conversation.
WELCOME_TRIGGERS = frozenset({"start", "hello", "hi", "hey", "welcome", "__init__", "__welcome__"})

# A human still owns these, so they are continued whatever their age (a stale ACTIVE one only
# exists while an agent is assigned: the auto-abandon worker closes the rest after 30 minutes).
_ALWAYS_REUSABLE = (SessionStatus.ACTIVE, SessionStatus.ESCALATED)
_CLOSED = (SessionStatus.ABANDONED, SessionStatus.RESOLVED)


def month_start(now: datetime | None = None) -> datetime:
    """First instant of the current calendar month, naive UTC."""
    now = now or utcnow_naive()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def reuse_cutoff(now: datetime | None = None) -> datetime:
    """Sessions whose last message is older than this are too old to be continued."""
    now = now or utcnow_naive()
    return now - timedelta(hours=settings.CONVERSATION_REUSE_WINDOW_HOURS)


def reusable_session_clause(cutoff: datetime) -> ColumnElement[bool]:
    """SQL form of "this session can be continued": open sessions always, closed ones only
    while their last message is inside the window."""
    return or_(
        ConversationSession.status.in_(_ALWAYS_REUSABLE),
        and_(
            ConversationSession.status.in_(_CLOSED),
            ConversationSession.last_message_at >= cutoff,
        ),
    )


def is_stale_closed(
    session: ConversationSession | object,
    message: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when messaging into this explicitly-addressed session must start a NEW conversation:
    it is abandoned (or resolved, unless the message is just the widget greeting) and its last
    message is outside the window."""
    status = getattr(session, "status", None)
    last = getattr(session, "last_message_at", None)
    if status not in _CLOSED or last is None:
        return False
    if status == SessionStatus.RESOLVED and (message or "").lower().strip() in WELCOME_TRIGGERS:
        return False
    if last.tzinfo is not None:
        last = last.astimezone(UTC).replace(tzinfo=None)  # Postgres hands back aware values
    return last < reuse_cutoff(now)


def reopen_if_closed(session: ConversationSession, message: str | None = None) -> bool:
    """Flip an abandoned session, or a resolved one answered by a real message, back to ACTIVE.

    The same row is kept (a reopened conversation is never a second counted row). Returns True if
    the status changed; the caller commits.
    """
    if session.status == SessionStatus.ABANDONED or (
        session.status == SessionStatus.RESOLVED
        and (message or "").lower().strip() not in WELCOME_TRIGGERS
    ):
        session.status = SessionStatus.ACTIVE
        return True
    return False


def monthly_conversation_count_stmt(tenant_id: UUID, *, now: datetime | None = None):  # type: ignore[no-untyped-def]
    """The one count query, as a statement, for callers that run it through their own executor."""
    return (
        select(func.count())
        .select_from(ConversationSession)
        .where(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.created_at >= month_start(now),
        )
    )


def count_conversations_this_month(
    db: Session, tenant_id: UUID, *, now: datetime | None = None
) -> int:
    return db.execute(monthly_conversation_count_stmt(tenant_id, now=now)).scalar_one()


def conversation_limit(db: Session, tenant_id: UUID) -> int | None:
    """The tenant's monthly cap; None means unlimited (or no capabilities row)."""
    return db.execute(
        select(TenantCapabilities.max_conversations_per_month).where(
            TenantCapabilities.tenant_id == tenant_id
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class ConversationUsage:
    count: int
    limit: int
    resets_at: datetime

    @property
    def reached(self) -> bool:
        return self.count >= self.limit

    def detail(self) -> dict[str, object]:
        """The 402 body the widget and API clients already receive."""
        return {
            "error": "conversation_limit_reached",
            "message": f"Monthly conversation limit of {self.limit} reached.",
            "current_count": self.count,
            "limit": self.limit,
            "resets_at": self.resets_at.isoformat(),
        }


def conversation_usage(
    db: Session, tenant_id: UUID, *, now: datetime | None = None
) -> ConversationUsage | None:
    """Usage against the cap, or None when the tenant has no cap."""
    limit = conversation_limit(db, tenant_id)
    if limit is None:
        return None
    start = month_start(now)
    return ConversationUsage(
        count=count_conversations_this_month(db, tenant_id, now=now),
        limit=limit,
        resets_at=(start + timedelta(days=32)).replace(day=1),
    )


def conversation_limit_reached(db: Session, tenant_id: UUID) -> bool:
    usage = conversation_usage(db, tenant_id)
    return usage is not None and usage.reached


def enforce_conversation_limit(db: Session, tenant_id: UUID) -> None:
    """Raise the 402 `conversation_limit_reached` if opening one more conversation would exceed
    the cap. Call it only where a NEW counted session is about to be created."""
    usage = conversation_usage(db, tenant_id)
    if usage is not None and usage.reached:
        raise HTTPException(status_code=402, detail=usage.detail())
