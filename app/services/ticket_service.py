"""Ticket service for managing support tickets."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func

from app.db.models.conversation import Ticket, TicketStatus, TicketPriority
from app.schemas.ticket import (
    TicketCreate,
    TicketUpdate,
    TicketResponse,
    TicketAssign,
    TicketResolve,
)


class TicketService:
    """Service for ticket management and escalation."""

    def __init__(self, db: Session):
        """Initialize ticket service with database session."""
        self.db = db

    def _generate_ticket_number(self) -> str:
        """
        Generate a unique ticket number.

        Returns:
            Ticket number in format TKT-YYYY-NNNN
        """
        # Get count of tickets created this year
        current_year = datetime.utcnow().year
        year_prefix = f"TKT-{current_year}-"

        stmt = select(func.count(Ticket.id)).where(
            Ticket.ticket_number.startswith(year_prefix)
        )
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

        return TicketResponse.model_validate(db_ticket)

    def get_ticket(self, ticket_id: UUID) -> Optional[TicketResponse]:
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

        return TicketResponse.model_validate(ticket)

    def get_ticket_by_number(self, ticket_number: str) -> Optional[TicketResponse]:
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

        return TicketResponse.model_validate(ticket)

    def get_ticket_by_session(self, session_id: UUID) -> Optional[TicketResponse]:
        """
        Get ticket for a conversation session.

        Args:
            session_id: Session UUID

        Returns:
            Most recent ticket for session if found, None otherwise
        """
        stmt = (
            select(Ticket)
            .where(Ticket.session_id == session_id)
            .order_by(desc(Ticket.created_at))
        )

        ticket = self.db.execute(stmt).scalar_one_or_none()

        if not ticket:
            return None

        return TicketResponse.model_validate(ticket)

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

        return TicketResponse.model_validate(ticket)

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

        # Update status to in_progress if it's open
        if ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.IN_PROGRESS

        self.db.commit()
        self.db.refresh(ticket)

        return TicketResponse.model_validate(ticket)

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

        # Resolve the ticket
        ticket.status = TicketStatus.RESOLVED
        ticket.resolution_notes = resolve_data.resolution_notes
        ticket.resolved_at = datetime.utcnow()

        # Also resolve the conversation session
        if ticket.session_id:
            from app.db.models.conversation import ConversationSession, SessionStatus
            session = self.db.query(ConversationSession).filter(
                ConversationSession.id == ticket.session_id
            ).first()

            if session:
                session.status = SessionStatus.RESOLVED

        self.db.commit()
        self.db.refresh(ticket)

        return TicketResponse.model_validate(ticket)

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

        return TicketResponse.model_validate(ticket)

    def get_tenant_tickets(
        self,
        tenant_id: UUID,
        status: Optional[TicketStatus] = None,
        assigned_to: Optional[UUID] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TicketResponse], int]:
        """
        Get tickets for a tenant with filtering.

        Args:
            tenant_id: Tenant UUID
            status: Optional status filter
            assigned_to: Optional assigned user filter
            limit: Maximum number of tickets to return
            offset: Number of tickets to skip

        Returns:
            Tuple of (list of ticket responses, total count)
        """
        # Build query
        stmt = select(Ticket).where(Ticket.tenant_id == tenant_id)

        if status:
            stmt = stmt.where(Ticket.status == status)

        if assigned_to:
            stmt = stmt.where(Ticket.assigned_to == assigned_to)

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.db.execute(count_stmt).scalar()

        # Apply ordering, limit, and offset
        stmt = stmt.order_by(desc(Ticket.created_at)).limit(limit).offset(offset)

        tickets = self.db.execute(stmt).scalars().all()

        return [TicketResponse.model_validate(t) for t in tickets], total

    def get_user_assigned_tickets(
        self, user_id: UUID, status: Optional[TicketStatus] = None
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

        return [TicketResponse.model_validate(t) for t in tickets]

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

        return [TicketResponse.model_validate(t) for t in tickets]
