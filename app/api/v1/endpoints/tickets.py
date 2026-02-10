"""Ticket management endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.services.ticket_service import TicketService

router = APIRouter()


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket_data: TicketManualCreate,
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

    ticket = ticket_service.create_ticket(create_data)
    return ticket


@router.get("", response_model=TicketListResponse, status_code=status.HTTP_200_OK)
def list_tickets(
    tenant_id: UUID | None = Query(None, description="Filter by tenant"),
    status_filter: TicketStatus | None = Query(None, description="Filter by status"),
    assigned_to_me: bool = Query(False, description="Show only tickets assigned to me"),
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
        limit=limit,
        offset=offset,
    )

    return TicketListResponse(
        tickets=tickets, total=total, page=offset // limit + 1, page_size=limit
    )


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

    return ticket


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
        return updated_ticket
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
        return updated_ticket
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

    try:
        resolved_ticket = ticket_service.resolve_ticket(ticket_id, resolve_data)

        # Broadcast ticket_resolved event via WebSocket
        if resolved_ticket.session_id:
            # Send to customer
            await connection_manager.send_to_customer(
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
                },
            )

            # Send to all agents watching this session
            await connection_manager.send_to_agents(
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
                },
            )

        return resolved_ticket
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
            # Send to customer
            await connection_manager.send_to_customer(
                session_id=closed_ticket.session_id,
                message={
                    "type": "ticket_closed",
                    "ticket_id": str(closed_ticket.id),
                    "ticket_number": closed_ticket.ticket_number,
                    "closed_by": current_user.full_name,
                    "closed_at": datetime.utcnow().isoformat(),
                },
            )

            # Send to all agents watching this session
            await connection_manager.send_to_agents(
                session_id=closed_ticket.session_id,
                message={
                    "type": "ticket_closed",
                    "ticket_id": str(closed_ticket.id),
                    "ticket_number": closed_ticket.ticket_number,
                    "closed_by": current_user.full_name,
                    "closed_at": datetime.utcnow().isoformat(),
                },
            )

        return closed_ticket
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


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

    return tickets
