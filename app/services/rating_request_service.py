"""N4 rating requests: mint, gate, supersede/reuse, frames (spec §4.2, §4.4, §4.5).

Security notes:
- The plaintext token leaves this module only in RatingRequestResult.token (for the
  customer-only WS frame) and in the customer's encrypted Message.meta. It is never
  logged; logs here carry ids and exception type names only.
- create_request locks the ticket row (SELECT … FOR UPDATE) so manual-vs-manual and
  manual-vs-auto races serialise: "open" (fulfilled_at IS NULL AND expires_at > now())
  cannot be a partial index because now() is not immutable.
"""

import hashlib
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    Ticket,
    TicketStatus,
)
from app.db.models.review import RatingRequest, RatingSource, SessionRating
from app.db.models.user import User, UserRole
from app.utils.datetime import ensure_utc_aware, utcnow_aware, utcnow_naive

logger = logging.getLogger(__name__)

REQUEST_TTL = timedelta(days=7)
MANUAL_COOLDOWN = timedelta(minutes=10)
RESOLVED_GRACE = timedelta(hours=24)
SYSTEM_NOTICE = "rating_request"
FALLBACK_PROMPT = "How was your chat with our team?"


class RatingRequestError(Exception):
    """A refused request; `detail()` is the spec's object error body."""

    STATUS = {
        "rating_request_forbidden": 403,
        "no_human_agent": 409,
        "ticket_not_ratable": 409,
        "already_rated": 409,
        "rating_request_cooldown": 409,
    }
    MESSAGES = {
        "rating_request_forbidden": "Only the assigned agent or an admin can request a rating",
        "no_human_agent": "No human agent is assigned to this ticket",
        "ticket_not_ratable": (
            "A rating can only be requested while the ticket is in progress "
            "or within 24 hours of resolving it"
        ),
        "already_rated": "The customer has already rated this agent for this conversation",
        "rating_request_cooldown": "A rating was requested recently. Try again later",
    }

    def __init__(self, code: str, retry_after_seconds: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]
        self.message = self.MESSAGES[code]
        self.retry_after_seconds = retry_after_seconds

    def detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_seconds is not None:
            body["retry_after_seconds"] = self.retry_after_seconds
        return body


@dataclass
class Eligibility:
    can_request: bool
    reason_code: str | None
    retry_after_seconds: int | None
    open_request: RatingRequest | None


@dataclass
class RatingRequestResult:
    request: RatingRequest
    token: str | None  # None when an open request was reused (auto on resolve)
    message: Message | None
    agent_first_name: str | None
    agent_name: str | None
    created: bool


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def first_name(full_name: str | None) -> str | None:
    parts = (full_name or "").split()
    return parts[0] if parts else None


def prompt_copy(first: str | None) -> str:
    return f"How was your chat with {first}?" if first else FALLBACK_PROMPT


class RatingRequestService:
    """Creates and evaluates rating requests for a ticket's human assignee."""

    def __init__(self, db: Session):
        self.db = db

    # --- reads -------------------------------------------------------------

    def open_request(self, session_id: UUID, agent_id: UUID) -> RatingRequest | None:
        return self.db.execute(
            select(RatingRequest)
            .where(
                RatingRequest.session_id == session_id,
                RatingRequest.agent_id == agent_id,
                RatingRequest.fulfilled_at.is_(None),
                RatingRequest.expires_at > utcnow_aware(),
            )
            .order_by(RatingRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_request(self, session_id: UUID, agent_id: UUID) -> RatingRequest | None:
        return self.db.execute(
            select(RatingRequest)
            .where(RatingRequest.session_id == session_id, RatingRequest.agent_id == agent_id)
            .order_by(RatingRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def agent_rating(self, session_id: UUID, agent_id: UUID) -> SessionRating | None:
        return self.db.execute(
            select(SessionRating).where(
                SessionRating.session_id == session_id,
                SessionRating.rated_agent_id == agent_id,
            )
        ).scalar_one_or_none()

    # --- gating ------------------------------------------------------------

    @staticmethod
    def _ratable(ticket: Any, now: datetime) -> bool:
        if ticket.status == TicketStatus.IN_PROGRESS:
            return True
        if ticket.status == TicketStatus.RESOLVED and ticket.resolved_at is not None:
            return now - ensure_utc_aware(ticket.resolved_at) <= RESOLVED_GRACE
        return False

    def _cooldown_left(self, session_id: UUID, agent_id: UUID, now: datetime) -> int | None:
        latest = self.latest_request(session_id, agent_id)
        if latest is None:
            return None
        elapsed = now - ensure_utc_aware(latest.created_at)
        if elapsed >= MANUAL_COOLDOWN:
            return None
        return max(1, math.ceil((MANUAL_COOLDOWN - elapsed).total_seconds()))

    def check_manual(self, ticket: Any, actor: Any) -> None:
        """Raise RatingRequestError for the first failing rule (order is the contract)."""
        now = utcnow_aware()
        if ticket.assigned_to is None:
            raise RatingRequestError("no_human_agent")
        if actor.role != UserRole.TENANT_ADMIN and actor.id != ticket.assigned_to:
            raise RatingRequestError("rating_request_forbidden")
        if not self._ratable(ticket, now):
            raise RatingRequestError("ticket_not_ratable")
        if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
            raise RatingRequestError("already_rated")
        left = self._cooldown_left(ticket.session_id, ticket.assigned_to, now)
        if left is not None:
            raise RatingRequestError("rating_request_cooldown", retry_after_seconds=left)

    def evaluate(self, ticket: Any, actor: Any) -> Eligibility:
        open_req = (
            self.open_request(ticket.session_id, ticket.assigned_to) if ticket.assigned_to else None
        )
        try:
            self.check_manual(ticket, actor)
        except RatingRequestError as exc:
            return Eligibility(False, exc.code, exc.retry_after_seconds, open_req)
        return Eligibility(True, None, None, open_req)

    def rating_state(self, ticket: Any) -> tuple[str, UUID | None]:
        """`ticket_resolved` / `ticket_closed` fields: pending | already_rated | none."""
        if ticket.assigned_to is None:
            return "none", None
        if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
            return "already_rated", None
        open_req = self.open_request(ticket.session_id, ticket.assigned_to)
        if open_req is not None:
            return "pending", open_req.id
        return "none", None

    # --- write -------------------------------------------------------------

    def create_request(
        self, ticket_id: UUID, *, source: str, requested_by: Any
    ) -> RatingRequestResult:
        """Mint (or, for auto, reuse) a request under a row lock on the ticket.

        A refusal (RatingRequestError) ends the transaction (nothing was written) and any
        other failure rolls back, so the ticket row lock is released immediately.
        """
        if source not in RatingSource.REQUESTABLE:
            raise ValueError(f"unsupported rating request source: {source}")
        ticket = self.db.execute(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if ticket is None:
            raise ValueError("Ticket not found")
        try:
            if source == RatingSource.MANUAL_REQUEST:
                self.check_manual(ticket, requested_by)
            else:
                if ticket.assigned_to is None:
                    raise RatingRequestError("no_human_agent")
                if self.agent_rating(ticket.session_id, ticket.assigned_to) is not None:
                    raise RatingRequestError("already_rated")
            return self._mint(ticket, source=source, requested_by=requested_by)
        except RatingRequestError:
            # Refusals are raised before any write, so ending the transaction is a no-op
            # that releases the ticket row lock now (not at session close). Commit, not
            # rollback: rollback would also discard unrelated pending caller state.
            self.db.commit()
            raise
        except Exception:
            self.db.rollback()
            raise

    def _mint(self, ticket: Ticket, *, source: str, requested_by: Any) -> RatingRequestResult:
        agent_id = ticket.assigned_to
        agent = self.db.get(User, agent_id)
        full = agent.full_name if agent else None
        first = first_name(full)
        now = utcnow_aware()

        existing = self.open_request(ticket.session_id, agent_id)
        if existing is not None:
            if source == RatingSource.AUTO_RESOLVE:
                self.db.commit()  # nothing changed; releases the row lock
                return RatingRequestResult(existing, None, None, first, full, created=False)
            existing.expires_at = now  # manual after cooldown supersedes the open one

        token = new_token()
        request = RatingRequest(
            tenant_id=ticket.tenant_id,
            session_id=ticket.session_id,
            ticket_id=ticket.id,
            agent_id=agent_id,
            requested_by=requested_by.id if source == RatingSource.MANUAL_REQUEST else None,
            source=source,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=now + REQUEST_TTL,
        )
        self.db.add(request)
        self.db.flush()
        # Persisted in the same transaction as the request (not via add_message, which
        # commits mid-lock): the request and its bound message exist together or not at all.
        message = Message(
            session_id=ticket.session_id,
            role=MessageRole.ASSISTANT,
            content=prompt_copy(first),
            meta={
                "system_notice": SYSTEM_NOTICE,
                "request_id": str(request.id),
                "agent_first_name": first,
                "rating_token": token,
            },
        )
        self.db.add(message)
        self.db.flush()
        request.message_id = message.id
        session = self.db.get(ConversationSession, ticket.session_id)
        if session is not None:
            session.last_message_at = utcnow_naive()
        self.db.commit()
        self.db.refresh(request)
        self.db.refresh(message)
        return RatingRequestResult(request, token, message, first, full, created=True)


# --- WS frames (spec §4.5) --------------------------------------------------


def customer_frame(result: RatingRequestResult) -> dict[str, Any]:
    req, msg = result.request, result.message
    return {
        "type": "rating_requested",
        "request_id": str(req.id),
        "token": result.token,
        "agent_name": result.agent_first_name,
        "message_id": str(msg.id),
        "content": msg.content,
        "source": req.source,
        "timestamp": ensure_utc_aware(msg.created_at).isoformat(),
        "expires_at": ensure_utc_aware(req.expires_at).isoformat(),
    }


def agent_frame(req: RatingRequest) -> dict[str, Any]:
    return {
        "type": "rating_request_created",
        "request_id": str(req.id),
        "ticket_id": str(req.ticket_id) if req.ticket_id else None,
        "agent_id": str(req.agent_id),
        "source": req.source,
        "created_at": ensure_utc_aware(req.created_at).isoformat(),
    }


async def fanout_rating_request(result: RatingRequestResult) -> None:
    """Customer-only bound card + token-free agent notice. Best effort: already committed."""
    if not result.created or result.token is None or result.message is None:
        return
    req = result.request
    for message, to_customer in ((customer_frame(result), True), (agent_frame(req), False)):
        try:
            await connection_manager.fanout_event(
                session_id=req.session_id,
                message=message,
                to_customer=to_customer,
                to_agents=not to_customer,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the caller; never log the payload
            logger.error(
                "rating request fanout failed request_id=%s frame=%s (%s)",
                req.id,
                message["type"],
                type(exc).__name__,
            )
