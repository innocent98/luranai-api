"""Ticket management endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_tenant_admin,
    get_current_user,
    get_db,
    verify_subscription_active,
    verify_tenant_access,
)
from app.core.websocket import connection_manager
from app.db.models.conversation import TicketStatus
from app.db.models.user import UserRole
from app.schemas.ticket import (
    TicketAssign,
    TicketListResponse,
    TicketManualCreate,
    TicketResolve,
    TicketResponse,
    TicketUpdate,
)
from app.schemas.user import UserResponse
from app.services.customer_presence import read_presence_many
from app.services.rating_request_service import auto_request_on_resolve, rating_frame_fields
from app.services.ticket_service import TicketAlreadyFinalError, TicketService

router = APIRouter()


def _with_presence_many(db: Session, tickets: list[TicketResponse]) -> list[TicketResponse]:
    """Attach `customer_presence` (one batched Redis read). Staff endpoints only."""
    presence = read_presence_many(db, [t.session_id for t in tickets])
    for ticket in tickets:
        ticket.customer_presence = presence.get(ticket.session_id)
    return tickets


def _with_presence(db: Session, ticket: TicketResponse) -> TicketResponse:
    return _with_presence_many(db, [ticket])[0]


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket_data: TicketManualCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
    _subscription_check: None = Depends(verify_subscription_active),
):
    """
    Manually create a support ticket.

    Args:
        ticket_data: Ticket creation data
        db: Database session
        current_user: Current user (support agent or admin)

    Returns:
        Created ticket
    """
    from app.schemas.ticket import TicketCreate
    from app.services.conversation_service import ConversationService

    # Get session to verify tenant
    conversation_service = ConversationService(db)
    session = conversation_service.get_session(ticket_data.session_id)

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Verify tenant access
    verify_tenant_access(session.tenant_id, current_user)

    ticket_service = TicketService(db)

    # Create ticket
    create_data = TicketCreate(
        tenant_id=session.tenant_id,
        session_id=ticket_data.session_id,
        summary=ticket_data.summary,
        priority=ticket_data.priority,
    )

    # Same per-session lock + live-ticket guard as the chat escalation path: a
    # session that already has an OPEN/IN_PROGRESS/MISSED ticket gets that one back
    # (200) instead of a duplicate.
    ticket, created = ticket_service.get_or_create_live_ticket(create_data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return ticket


@router.get("", response_model=TicketListResponse, status_code=status.HTTP_200_OK)
def list_tickets(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    status_filter: TicketStatus | None = Query(None, description="Filter by status"),
    assigned_to_me: bool = Query(False, description="Show only tickets assigned to me"),
    session_id: UUID | None = Query(None, description="Filter by conversation session"),
    q: str | None = Query(
        None,
        max_length=100,
        description=(
            "Case-insensitive substring search over ticket number, summary and "
            "visitor name/email/phone. Ignored if < 2 chars after trim."
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    List tickets with filters.

    Args:
        tenant_id: Optional tenant filter
        status_filter: Optional status filter
        assigned_to_me: Show only tickets assigned to current user
        session_id: Optional conversation session filter
        q: Optional free-text search, ANDed with the other filters
        limit: Maximum tickets to return
        offset: Number of tickets to skip
        db: Database session
        current_user: Current user

    Returns:
        Paginated list of tickets
    """
    ticket_service = TicketService(db)

    # Determine tenant_id from current user
    # Tenant users can only access their own tenant's tickets
    if not tenant_id:
        tenant_id = current_user.tenant_id

    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    # Get assigned user filter
    assigned_to = current_user.id if assigned_to_me else None

    tickets, total = ticket_service.get_tenant_tickets(
        tenant_id=tenant_id,
        status=status_filter,
        assigned_to=assigned_to,
        session_id=session_id,
        q=q,
        limit=limit,
        offset=offset,
    )

    _with_presence_many(db, tickets)
    return TicketListResponse(
        tickets=tickets, total=total, page=offset // limit + 1, page_size=limit
    )


# Literal-path routes must be registered before "/{ticket_id}": FastAPI matches in
# declaration order, so "/unassigned" would otherwise be parsed as a UUID (422).
@router.get("/unassigned", response_model=list[TicketResponse], status_code=status.HTTP_200_OK)
def get_unassigned_tickets(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get unassigned tickets.

    Args:
        tenant_id: Optional tenant filter
        db: Database session
        current_user: Current admin user

    Returns:
        List of unassigned tickets
    """
    # Determine tenant_id from current user
    # Tenant users can only access their own tenant's tickets
    if not tenant_id:
        tenant_id = current_user.tenant_id

    # Verify tenant access
    verify_tenant_access(tenant_id, current_user)

    ticket_service = TicketService(db)
    tickets = ticket_service.get_unassigned_tickets(tenant_id)

    return _with_presence_many(db, tickets)


@router.get("/{ticket_id}", response_model=TicketResponse, status_code=status.HTTP_200_OK)
def get_ticket(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Get ticket by ID.

    Args:
        ticket_id: Ticket UUID
        db: Database session
        current_user: Current user

    Returns:
        Ticket details
    """
    ticket_service = TicketService(db)
    ticket = ticket_service.get_ticket(ticket_id)

    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Verify tenant access
    verify_tenant_access(ticket.tenant_id, current_user)

    return _with_presence(db, ticket)


@router.put("/{ticket_id}", response_model=TicketResponse, status_code=status.HTTP_200_OK)
def update_ticket(
    ticket_id: UUID,
    ticket_data: TicketUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Update ticket information.

    Args:
        ticket_id: Ticket UUID
        ticket_data: Updated ticket data
        db: Database session
        current_user: Current admin user

    Returns:
        Updated ticket
    """
    ticket_service = TicketService(db)

    # Get ticket to verify tenant access
    ticket = ticket_service.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Verify tenant access
    verify_tenant_access(ticket.tenant_id, current_user)

    try:
        updated_ticket = ticket_service.update_ticket(ticket_id, ticket_data)
        return _with_presence(db, updated_ticket)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.post("/{ticket_id}/assign", response_model=TicketResponse, status_code=status.HTTP_200_OK)
def assign_ticket(
    ticket_id: UUID,
    assign_data: TicketAssign,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Assign ticket to a support agent.

    Args:
        ticket_id: Ticket UUID
        assign_data: Assignment data with user ID
        db: Database session
        current_user: Current admin user

    Returns:
        Updated ticket
    """
    ticket_service = TicketService(db)

    # Get ticket to verify tenant access
    ticket = ticket_service.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Verify tenant access
    verify_tenant_access(ticket.tenant_id, current_user)

    try:
        updated_ticket = ticket_service.assign_ticket(ticket_id, assign_data)
        return _with_presence(db, updated_ticket)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.post("/{ticket_id}/resolve", response_model=TicketResponse, status_code=status.HTTP_200_OK)
async def resolve_ticket(
    ticket_id: UUID,
    resolve_data: TicketResolve,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Resolve a ticket.

    Broadcasts real-time notification to customer and agents via WebSocket.

    Args:
        ticket_id: Ticket UUID
        resolve_data: Resolution notes
        db: Database session
        current_user: Current user (support agent or admin)

    Returns:
        Resolved ticket
    """
    ticket_service = TicketService(db)

    # Get ticket to verify tenant access
    ticket = ticket_service.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Verify tenant access
    verify_tenant_access(ticket.tenant_id, current_user)

    # Verify user can resolve (assigned agent or admin)
    if current_user.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only support agents or admins can resolve tickets",
        )

    # Admins may resolve anything (incl. unassigned MISSED); an agent only the
    # ticket they currently hold, so they must take it over first.
    if current_user.role != UserRole.TENANT_ADMIN and ticket.assigned_to != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assigned agent or an admin can resolve this ticket",
        )

    try:
        resolved_ticket = ticket_service.resolve_ticket(ticket_id, resolve_data)

        # N4 §4.4: the auto rating request (and its customer card) must be in the widget's
        # state before ticket_resolved, because the widget clears its session on that frame.
        rating_request_id, rating_state = await auto_request_on_resolve(
            db, resolved_ticket.id, resolved_ticket.assigned_to
        )
        resolved_ticket.rating_request_id = rating_request_id

        # Broadcast ticket_resolved event via WebSocket
        if resolved_ticket.session_id:
            await connection_manager.fanout_event(
                session_id=resolved_ticket.session_id,
                message={
                    "type": "ticket_resolved",
                    "ticket_id": str(resolved_ticket.id),
                    "ticket_number": resolved_ticket.ticket_number,
                    "resolution_notes": resolved_ticket.resolution_notes,
                    "resolved_by": current_user.full_name,
                    "resolved_at": (
                        resolved_ticket.resolved_at.isoformat()
                        if resolved_ticket.resolved_at
                        else None
                    ),
                    "rating_request_id": str(rating_request_id) if rating_request_id else None,
                    "rating_state": rating_state,
                },
                to_customer=True,
                to_agents=True,
            )

        return _with_presence(db, resolved_ticket)
    except TicketAlreadyFinalError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.post("/{ticket_id}/close", response_model=TicketResponse, status_code=status.HTTP_200_OK)
async def close_ticket(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Close a ticket.

    Broadcasts real-time notification to customer and agents via WebSocket.

    Args:
        ticket_id: Ticket UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Closed ticket
    """
    ticket_service = TicketService(db)

    # Get ticket to verify tenant access
    ticket = ticket_service.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Verify tenant access
    verify_tenant_access(ticket.tenant_id, current_user)

    try:
        closed_ticket = ticket_service.close_ticket(ticket_id)

        # Broadcast ticket_closed event via WebSocket
        if closed_ticket.session_id:
            await connection_manager.fanout_event(
                session_id=closed_ticket.session_id,
                message={
                    "type": "ticket_closed",
                    "ticket_id": str(closed_ticket.id),
                    "ticket_number": closed_ticket.ticket_number,
                    "closed_by": current_user.full_name,
                    "closed_at": datetime.utcnow().isoformat(),
                    **rating_frame_fields(db, closed_ticket),
                },
                to_customer=True,
                to_agents=True,
            )

        return _with_presence(db, closed_ticket)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
