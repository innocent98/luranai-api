"""Ticket service for managing support tickets."""

from uuid import UUID

from sqlalchemy import case, desc, func, literal, or_, select, update
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Ticket, TicketStatus
from app.db.models.tenant import TenantCapabilities
from app.db.models.user import User
from app.db.models.visitor import Visitor
from app.schemas.ticket import (
    AssignedAgent,
    TicketAssign,
    TicketCreate,
    TicketResolve,
    TicketResponse,
    TicketUpdate,
)
from app.schemas.visitor import VisitorIdentitySummary
from app.utils.datetime import utcnow_naive


MIN_SEARCH_LEN = 2
_LIKE_ESCAPE = "\\"


def normalize_ticket_search(q: str | None) -> str | None:
    """Strip a free-text ticket search; None when absent or shorter than 2 chars."""
    if q is None:
        return None
    needle = q.strip()
    return needle if len(needle) >= MIN_SEARCH_LEN else None


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters so user input matches literally (ESCAPE '\\')."""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


class TicketAlreadyFinalError(ValueError):
    """Raised when resolving a ticket that is already RESOLVED or CLOSED."""


class TicketService:
    """Service for ticket management and escalation."""

    def __init__(self, db: Session):
        """Initialize ticket service with database session."""
        self.db = db

    def _to_ticket_response(self, ticket: Ticket) -> TicketResponse:
        """
        Convert Ticket model to TicketResponse with agent details.

        Args:
            ticket: Ticket model instance

        Returns:
            TicketResponse with assigned_agent populated if ticket is assigned
        """
        # Convert ticket to dict using model_validate
        ticket_response = TicketResponse.model_validate(ticket)

        # If ticket is assigned, fetch agent details
        if ticket.assigned_to:
            stmt = select(User).where(User.id == ticket.assigned_to)
            agent = self.db.execute(stmt).scalar_one_or_none()

            if agent:
                ticket_response.assigned_agent = AssignedAgent(
                    id=agent.id,
                    email=agent.email,
                    full_name=agent.full_name,
                    role=agent.role,
                )

        session = self.db.execute(
            select(ConversationSession).where(ConversationSession.id == ticket.session_id)
        ).scalar_one_or_none()
        if session and session.visitor_id:
            visitor = self.db.execute(
                select(Visitor).where(Visitor.id == session.visitor_id)
            ).scalar_one_or_none()
            if visitor:
                ticket_response.visitor = VisitorIdentitySummary(
                    id=visitor.id,
                    name=visitor.name,
                    email=visitor.email,
                    phone=visitor.phone,
                    first_seen_at=visitor.first_seen_at,
                    last_seen_at=visitor.last_seen_at,
                    is_blocked=visitor.is_blocked,
                )

        return ticket_response

    def _generate_ticket_number(self) -> str:
        """
        Generate a unique ticket number.

        Returns:
            Ticket number in format TKT-YYYY-NNNN
        """
        # Get count of tickets created this year
        current_year = utcnow_naive().year
        year_prefix = f"TKT-{current_year}-"

        stmt = select(func.count(Ticket.id)).where(Ticket.ticket_number.startswith(year_prefix))
        count = self.db.execute(stmt).scalar()

        # Generate next number
        next_number = (count or 0) + 1
        return f"{year_prefix}{next_number:04d}"

    def create_ticket(self, ticket_data: TicketCreate) -> TicketResponse:
        """
        Create a new support ticket from an escalated conversation.

        Args:
            ticket_data: Ticket creation data

        Returns:
            Created ticket response
        """
        # Enforce tenant ticket capability
        capabilities = self.db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == ticket_data.tenant_id)
        ).scalar_one_or_none()
        if capabilities and not capabilities.can_create_tickets:
            raise ValueError(
                "Ticket creation is disabled for your tenant capabilities. "
                "Enable ticket escalation or contact support."
            )

        # Generate ticket number
        ticket_number = self._generate_ticket_number()

        db_ticket = Ticket(
            tenant_id=ticket_data.tenant_id,
            session_id=ticket_data.session_id,
            ticket_number=ticket_number,
            summary=ticket_data.summary,
            priority=ticket_data.priority,
            status=TicketStatus.OPEN,
        )

        self.db.add(db_ticket)
        self.db.commit()
        self.db.refresh(db_ticket)

        return self._to_ticket_response(db_ticket)

    def get_ticket(self, ticket_id: UUID) -> TicketResponse | None:
        """
        Get ticket by ID.

        Args:
            ticket_id: Ticket UUID

        Returns:
            Ticket response if found, None otherwise
        """
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            return None

        return self._to_ticket_response(ticket)

    def get_ticket_by_number(self, ticket_number: str) -> TicketResponse | None:
        """
        Get ticket by ticket number.

        Args:
            ticket_number: Ticket number (e.g., TKT-2024-0001)

        Returns:
            Ticket response if found, None otherwise
        """
        stmt = select(Ticket).where(Ticket.ticket_number == ticket_number)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            return None

        return self._to_ticket_response(ticket)

    def get_ticket_by_session(self, session_id: UUID) -> TicketResponse | None:
        """
        Get ticket for a conversation session.

        Args:
            session_id: Session UUID

        Returns:
            Most recent ticket for session if found, None otherwise
        """
        stmt = (
            select(Ticket).where(Ticket.session_id == session_id).order_by(desc(Ticket.created_at))
        )

        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            return None

        return self._to_ticket_response(ticket)

    def lock_session_for_escalation(self, session_id: UUID) -> None:
        """Serialise escalations of one session on its conversation_sessions row.

        `SELECT ... FOR UPDATE` held until the caller's transaction ends (every
        TicketService write commits, which releases it). A concurrent escalation
        of the same session blocks here, then re-runs `get_open_ticket_for_session`
        and sees the first caller's committed ticket instead of inserting a
        duplicate. Read Committed re-reads on the next statement, so no refresh
        is needed. A missing session is a no-op: nothing to serialise on.
        """
        self.db.execute(
            select(ConversationSession.id)
            .where(ConversationSession.id == session_id)
            .with_for_update()
        )

    def get_open_ticket_for_session(self, session_id: UUID) -> TicketResponse | None:
        """
        Get the session's live (still-actionable) escalation ticket, if any.

        Re-escalating an already-escalated session (the visitor sends another
        message, a flow re-matches) must reuse this ticket instead of minting a
        second one — a second ticket means a second ARQ job id and a second full
        ring of every agent's phone. RESOLVED and CLOSED tickets don't count as
        live; OPEN, IN_PROGRESS and MISSED do — a missed escalation is still
        waiting to be handled, not a fresh one to be created.

        Args:
            session_id: Session UUID

        Returns:
            Most recent non-resolved, non-closed ticket for session, or None
        """
        stmt = (
            select(Ticket)
            .where(
                Ticket.session_id == session_id,
                Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
            )
            .order_by(desc(Ticket.created_at))
        )

        ticket = self.db.execute(stmt).scalars().first()

        if not ticket:
            return None

        return self._to_ticket_response(ticket)

    def reopen_missed_ticket(self, ticket_id: UUID) -> TicketResponse | None:
        """
        Atomically flip a MISSED, still-unassigned ticket back to OPEN.

        `auto_resolve_stale_tickets` never sweeps MISSED tickets (it only ages
        out OPEN/IN_PROGRESS), so a MISSED ticket that gets reused for a
        re-escalation (see `get_open_ticket_for_session`) needs to become
        actionable again — OPEN so the fallback's own `status == OPEN` guard
        still works for a fresh 90s window, not left permanently MISSED.

        A conditional UPDATE, not read-then-write: if a claim (or a
        concurrent reopen) landed first, `rowcount` is 0 and this returns
        `None` rather than resurrecting a ticket someone already owns.

        Args:
            ticket_id: Ticket UUID

        Returns:
            The reopened ticket, or None if it was no longer MISSED-and-
            unassigned by the time this ran.
        """
        result = self.db.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.status == TicketStatus.MISSED,
                Ticket.assigned_to.is_(None),
            )
            .values(status=TicketStatus.OPEN)
        )
        self.db.commit()

        if result.rowcount == 0:
            return None

        return self.get_ticket(ticket_id)

    def update_ticket(self, ticket_id: UUID, ticket_data: TicketUpdate) -> TicketResponse:
        """
        Update ticket information.

        Args:
            ticket_id: Ticket UUID
            ticket_data: Updated ticket data

        Returns:
            Updated ticket response

        Raises:
            ValueError: If ticket not found
        """
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            raise ValueError("Ticket not found")

        # Update fields
        update_data = ticket_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(ticket, field, value)

        self.db.commit()
        self.db.refresh(ticket)

        return self._to_ticket_response(ticket)

    def assign_ticket(self, ticket_id: UUID, assign_data: TicketAssign) -> TicketResponse:
        """
        Assign ticket to a support agent.

        Args:
            ticket_id: Ticket UUID
            assign_data: Assignment data with user ID

        Returns:
            Updated ticket response

        Raises:
            ValueError: If ticket not found
        """
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            raise ValueError("Ticket not found")

        ticket.assigned_to = assign_data.assigned_to

        # Taking over an unhandled ticket (OPEN, or MISSED after the fallback
        # window lapsed) makes it in-progress; RESOLVED/CLOSED stay terminal.
        if ticket.status in (TicketStatus.OPEN, TicketStatus.MISSED):
            ticket.status = TicketStatus.IN_PROGRESS

        self.db.commit()
        self.db.refresh(ticket)

        return self._to_ticket_response(ticket)

    def claim_ticket(self, ticket_id: UUID, agent_id: UUID) -> Ticket | None:
        """Atomically claim a ticket for ``agent_id`` (dashboard takeover).

        Conditional UPDATE, same primitive as the mobile acknowledge: it wins only
        if the ticket is not RESOLVED/CLOSED and is unassigned or already ours
        (idempotent re-claim). OPEN/MISSED become IN_PROGRESS; IN_PROGRESS stays.

        Returns:
            The ticket row after a win, or ``None`` if the claim was lost (the
            caller reloads the ticket to see why).
        """
        result = self.db.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
                or_(Ticket.assigned_to.is_(None), Ticket.assigned_to == agent_id),
            )
            .values(
                assigned_to=agent_id,
                status=case(
                    (
                        Ticket.status.in_([TicketStatus.OPEN, TicketStatus.MISSED]),
                        literal(TicketStatus.IN_PROGRESS, type_=Ticket.status.type),
                    ),
                    else_=Ticket.status,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        if result.rowcount == 0:
            return None
        ticket = self.db.get(Ticket, ticket_id)
        self.db.refresh(ticket)
        return ticket

    def resolve_ticket(self, ticket_id: UUID, resolve_data: TicketResolve) -> TicketResponse:
        """
        Resolve a ticket and update the associated conversation session status.

        Args:
            ticket_id: Ticket UUID
            resolve_data: Resolution data with notes

        Returns:
            Updated ticket response

        Raises:
            ValueError: If ticket not found
        """
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            raise ValueError("Ticket not found")

        if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            raise TicketAlreadyFinalError("Ticket is already resolved or closed")

        # Resolve the ticket
        ticket.status = TicketStatus.RESOLVED
        ticket.resolution_notes = resolve_data.resolution_notes
        ticket.resolved_at = utcnow_naive()

        # Also resolve the conversation session
        if ticket.session_id:
            from app.db.models.conversation import ConversationSession, SessionStatus

            session = (
                self.db.query(ConversationSession)
                .filter(ConversationSession.id == ticket.session_id)
                .first()
            )

            if session:
                session.status = SessionStatus.RESOLVED

        self.db.commit()
        self.db.refresh(ticket)

        return self._to_ticket_response(ticket)

    def close_ticket(self, ticket_id: UUID) -> TicketResponse:
        """
        Close a ticket.

        Args:
            ticket_id: Ticket UUID

        Returns:
            Updated ticket response

        Raises:
            ValueError: If ticket not found
        """
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            raise ValueError("Ticket not found")

        ticket.status = TicketStatus.CLOSED

        self.db.commit()
        self.db.refresh(ticket)

        return self._to_ticket_response(ticket)

    def get_tenant_tickets(
        self,
        tenant_id: UUID,
        status: TicketStatus | None = None,
        assigned_to: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        session_id: UUID | None = None,
        q: str | None = None,
    ) -> tuple[list[TicketResponse], int]:
        """
        Get tickets for a tenant with filtering.

        Args:
            tenant_id: Tenant UUID
            status: Optional status filter
            assigned_to: Optional assigned user filter
            limit: Maximum number of tickets to return
            offset: Number of tickets to skip
            session_id: Optional conversation session filter
            q: Optional free-text search (see ``_ticket_search_condition``); ANDed
                with the other filters. Ignored when shorter than 2 chars after strip.

        Returns:
            Tuple of (list of ticket responses, total count)
        """
        # Build query
        filters = [Ticket.tenant_id == tenant_id]

        if status:
            filters.append(Ticket.status == status)

        if assigned_to:
            filters.append(Ticket.assigned_to == assigned_to)

        if session_id:
            filters.append(Ticket.session_id == session_id)

        needle = normalize_ticket_search(q)
        if needle:
            filters.append(self._ticket_search_condition(tenant_id, needle, filters))

        stmt = select(Ticket).where(*filters)

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar() or 0

        # Apply ordering, limit, and offset
        stmt = stmt.order_by(desc(Ticket.created_at)).limit(limit).offset(offset)

        tickets = self.db.execute(stmt).scalars().all()

        return [self._to_ticket_response(t) for t in tickets], total

    def _ticket_search_condition(self, tenant_id: UUID, needle: str, base_filters: list):
        """
        OR-condition matching ``needle`` as a case-insensitive literal substring of the
        ticket number, summary, or the linked visitor's name / email / phone.
        ``user_identifier`` is deliberately NOT searchable: it is the customer socket
        credential, and a substring match would let staff recover it character by character.

        Every sub-match is an IN-subquery on the ticket's own session_id, so no joins
        are added to the main statement: no duplicate rows, and count/page stay correct.
        Tickets without a session-linked visitor still match on their own fields.

        Visitor name/email/phone are Fernet-encrypted at rest (non-deterministic
        ciphertext), so SQL ILIKE cannot see them. They are matched after decryption,
        limited to visitors of this tenant who have a ticket passing the other filters.
        """
        pattern = f"%{_escape_like(needle)}%"
        conditions = [
            Ticket.ticket_number.ilike(pattern, escape=_LIKE_ESCAPE),
            Ticket.summary.ilike(pattern, escape=_LIKE_ESCAPE),
        ]

        candidates = self.db.execute(
            select(Visitor.id, Visitor.name, Visitor.email, Visitor.phone)
            .join(ConversationSession, ConversationSession.visitor_id == Visitor.id)
            .join(Ticket, Ticket.session_id == ConversationSession.id)
            .where(Visitor.tenant_id == tenant_id, *base_filters)
            .distinct()
        ).all()
        folded = needle.casefold()
        visitor_ids = [
            row.id
            for row in candidates
            if any(v and folded in v.casefold() for v in (row.name, row.email, row.phone))
        ]
        if visitor_ids:
            conditions.append(
                Ticket.session_id.in_(
                    select(ConversationSession.id).where(
                        ConversationSession.tenant_id == tenant_id,
                        ConversationSession.visitor_id.in_(visitor_ids),
                    )
                )
            )

        return or_(*conditions)

    def get_user_assigned_tickets(
        self, user_id: UUID, status: TicketStatus | None = None
    ) -> list[TicketResponse]:
        """
        Get tickets assigned to a specific user.

        Args:
            user_id: User UUID
            status: Optional status filter

        Returns:
            List of ticket responses
        """
        stmt = select(Ticket).where(Ticket.assigned_to == user_id)

        if status:
            stmt = stmt.where(Ticket.status == status)

        stmt = stmt.order_by(desc(Ticket.created_at))

        tickets = self.db.execute(stmt).scalars().all()

        return [self._to_ticket_response(t) for t in tickets]

    def get_unassigned_tickets(self, tenant_id: UUID) -> list[TicketResponse]:
        """
        Get unassigned tickets for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of unassigned ticket responses
        """
        stmt = (
            select(Ticket)
            .where(
                Ticket.tenant_id == tenant_id,
                Ticket.assigned_to.is_(None),
                Ticket.status != TicketStatus.CLOSED,
            )
            .order_by(Ticket.priority.desc(), Ticket.created_at)
        )

        tickets = self.db.execute(stmt).scalars().all()

        return [self._to_ticket_response(t) for t in tickets]
