"""Agent-requested ratings on a ticket (spec §4.3.1, §4.3.2). Mounted under /tickets."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.conversation import Ticket
from app.db.models.review import RatingSource
from app.db.models.user import UserRole
from app.schemas.rating import OpenRatingRequest, RatingRequestOut, RatingStatusResponse
from app.schemas.user import UserResponse
from app.services.rating_read_service import agent_names, to_brief
from app.services.rating_request_service import (
    RatingRequestError,
    RatingRequestService,
    fanout_rating_request,
)

router = APIRouter()

_STAFF = (UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT)


def _load_ticket(db: Session, ticket_id: UUID, user: UserResponse) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if user.role not in _STAFF:
        err = RatingRequestError("rating_request_forbidden")
        raise HTTPException(status_code=err.status_code, detail=err.detail())
    return ticket


def _create_manual_request(db: Session, ticket_id: UUID, user: UserResponse):
    """Sync: ticket load, FOR UPDATE create_request and commit. Returns the result."""
    _load_ticket(db, ticket_id, user)
    try:
        result = RatingRequestService(db).create_request(
            ticket_id, source=RatingSource.MANUAL_REQUEST, requested_by=user
        )
    except RatingRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
    return result


@router.post(
    "/{ticket_id}/rating-requests",
    response_model=RatingRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def request_rating(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Ask the customer to rate the ticket's assignee. The response never carries the token."""
    # Locking DB work + commit run in the threadpool so a row-lock wait never blocks the
    # event loop; the (async) WS fanout happens afterwards, once the lock is released.
    result = await run_in_threadpool(_create_manual_request, db, ticket_id, current_user)
    await fanout_rating_request(result)
    req = result.request
    return RatingRequestOut(
        request_id=req.id,
        ticket_id=req.ticket_id,
        session_id=req.session_id,
        agent_id=req.agent_id,
        agent_name=result.agent_name,
        source=req.source,
        message_id=req.message_id,
        created_at=req.created_at,
        expires_at=req.expires_at,
    )


@router.get("/{ticket_id}/rating-status", response_model=RatingStatusResponse)
def rating_status(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Drives the Request-rating button and the ticket rating badge.

    `rating` is the human rating for (session, current assignee), shown only to tenant
    admins and to the rated agent (N4-D5); everyone else gets null.
    """
    ticket = _load_ticket(db, ticket_id, current_user)
    svc = RatingRequestService(db)
    elig = svc.evaluate(ticket, current_user)
    rating = None
    if ticket.assigned_to is not None:
        row = svc.agent_rating(ticket.session_id, ticket.assigned_to)
        if row is not None and (
            current_user.role == UserRole.TENANT_ADMIN or row.rated_agent_id == current_user.id
        ):
            rating = to_brief(row, agent_names(db, [row.rated_agent_id]))
    open_req = elig.open_request
    return RatingStatusResponse(
        can_request=elig.can_request,
        reason_code=elig.reason_code,
        retry_after_seconds=elig.retry_after_seconds,
        open_request=(
            OpenRatingRequest(
                request_id=open_req.id,
                source=open_req.source,
                created_at=open_req.created_at,
                expires_at=open_req.expires_at,
            )
            if open_req is not None
            else None
        ),
        rating=rating,
    )
