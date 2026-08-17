"""Agent Caller mobile-app API (`/api/v1/agent`).

Every endpoint requires an agent JWT and is scoped to `current_agent.tenant_id`.
A resource belonging to another tenant returns 404, never 403 — matching the
auditor's `_owned_report` pattern, so the API never confirms existence
cross-tenant.
"""

import logging
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent
from app.core.config import settings
from app.db.models.agent_device import AgentDevice
from app.db.models.conversation import ConversationSession, Message, Ticket, TicketStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.agent_app import (
    AcknowledgeLost,
    AcknowledgeWon,
    AgentMeResponse,
    AvailabilityResponse,
    AvailabilityUpdateRequest,
    DeviceRegisterRequest,
    DeviceUnregisterRequest,
    EscalationDetail,
    EscalationSummary,
    TranscriptMessage,
)
from app.schemas.user import UserResponse
from app.services.agent_alert_service import _PRIORITY_WIRE, customer_name
from app.services.notification_enqueue import enqueue_clear_alert_async
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me", response_model=AgentMeResponse)
def get_agent_me(
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Identity, tenant display name and availability in one call (addendum A1)."""
    tenant = db.get(Tenant, current_agent.tenant_id)
    user = db.get(User, current_agent.id)
    return AgentMeResponse(
        user_id=current_agent.id,
        email=current_agent.email,
        full_name=current_agent.full_name,
        role=current_agent.role,
        tenant_id=current_agent.tenant_id,
        tenant_name=(tenant.display_name if tenant else "") or "",
        availability_status=user.availability_status,
    )


@router.post("/devices", status_code=status.HTTP_204_NO_CONTENT)
def register_device(
    payload: DeviceRegisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Upsert this device's FCM token. Called after login and on every cold start."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()

    if device is None:
        device = AgentDevice(
            user_id=current_agent.id,
            tenant_id=current_agent.tenant_id,
            device_token=payload.device_token,
            platform=payload.platform,
        )
        db.add(device)

    device.platform = payload.platform
    device.last_seen_at = utcnow_naive()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/devices", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    payload: DeviceUnregisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Remove this device on logout. Unknown tokens are a silent 204."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()
    if device is not None:
        db.delete(device)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/availability", response_model=AvailabilityResponse)
def set_availability(
    payload: AvailabilityUpdateRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Manual Available/Away toggle. Only `available` agents ring in tier 1."""
    user = db.get(User, current_agent.id)
    user.availability_status = payload.status
    db.commit()
    return AvailabilityResponse(status=payload.status)


def _wire_status(ticket: Ticket) -> str:
    """Contract addendum A5 — derive the app's status vocabulary.

    `pending` means unclaimed and still ringing, distinct from `in_progress`
    (claimed). Collapsing the two would tell an agent someone is already
    handling an escalation that nobody has actually picked up.
    """
    if ticket.status == TicketStatus.MISSED:
        return "missed"
    if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        return "resolved"
    if ticket.status == TicketStatus.IN_PROGRESS:
        return "in_progress"
    return "pending"


def _owned_ticket(db: Session, ticket_id: UUID, tenant_id: UUID) -> Ticket:
    """Load a ticket inside the caller's tenant, or 404 (never 403 — see module docstring)."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")
    return ticket


def _summary(db: Session, ticket: Ticket, session: ConversationSession | None) -> EscalationSummary:
    return EscalationSummary(
        escalation_id=ticket.id,
        session_id=ticket.session_id,
        customer_name=customer_name(db, session) if session else "Customer",
        reason=(session.escalation_reason if session else None) or ticket.summary or "",
        priority=_PRIORITY_WIRE.get(ticket.priority, "normal"),
        status=_wire_status(ticket),
        created_at=ticket.created_at,
    )


@router.get("/escalations", response_model=list[EscalationSummary])
def list_escalations(
    window_hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Active and recent escalations for this tenant, newest first."""
    cutoff = utcnow_naive() - timedelta(hours=window_hours)
    tickets = (
        db.execute(
            select(Ticket)
            .where(Ticket.tenant_id == current_agent.tenant_id, Ticket.created_at >= cutoff)
            .order_by(Ticket.created_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    sessions = {t.session_id: db.get(ConversationSession, t.session_id) for t in tickets}
    return [_summary(db, t, sessions.get(t.session_id)) for t in tickets]


@router.get("/escalations/{ticket_id}", response_model=EscalationDetail)
def get_escalation(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Detail + transcript. Serves both the pre-takeover and missed-summary views."""
    ticket = _owned_ticket(db, ticket_id, current_agent.tenant_id)
    session = db.get(ConversationSession, ticket.session_id)

    messages = (
        db.execute(
            select(Message)
            .where(Message.session_id == ticket.session_id)
            .order_by(Message.created_at.asc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    handler = db.get(User, ticket.assigned_to) if ticket.assigned_to else None

    base = _summary(db, ticket, session)
    return EscalationDetail(
        **base.model_dump(),
        handled_by=handler.full_name if handler else None,
        acknowledged_at=ticket.acknowledged_at,
        transcript=[
            TranscriptMessage(
                role=m.role.value if hasattr(m.role, "value") else str(m.role),
                content=m.content,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


def _ws_url(session_id: UUID) -> str:
    """Contract addendum A2 — absolute agent WebSocket URL for this session."""
    base = (settings.SERVER_HOST or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    else:
        # No recognised scheme on SERVER_HOST — assume TLS rather than silently
        # emitting a schemeless (unconnectable) ws url with no useful error.
        base = f"wss://{base}"
    return f"{base}/api/v1/ws/agent/{session_id}"


async def _finish_claim(db: Session, ticket: Ticket, current_agent: UserResponse) -> AcknowledgeWon:
    """Session handoff + clear fanout for a won claim.

    Called from both the fresh-win path and the idempotent-retry path, so it
    must be safe to run twice: the session fields are only written when they
    don't already point at this agent, and `enqueue_clear_alert_async` carries
    a deterministic ARQ job id per ticket, so a duplicate enqueue on a retry is
    a no-op rather than a double ring. A failed enqueue must never fail the
    claim — the agent has legitimately won either way.
    """
    session = db.get(ConversationSession, ticket.session_id)
    if session is not None and session.agent_id != current_agent.id:
        session.agent_id = current_agent.id
        session.agent_joined_at = utcnow_naive()
        db.commit()

    try:
        await enqueue_clear_alert_async(ticket.id, current_agent.id)
    except Exception:  # noqa: BLE001 — a missed clear must not fail a legitimate win
        logger.exception("Failed to enqueue clear_alert for escalation %s", ticket.id)

    return AcknowledgeWon(won=True, session_id=ticket.session_id, ws_url=_ws_url(ticket.session_id))


@router.post("/escalations/{ticket_id}/acknowledge", response_model=AcknowledgeWon)
async def acknowledge_escalation(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Claim an escalation. Exactly one caller can win.

    The conditional UPDATE *is* the coordination primitive: `assigned_to IS NULL`
    in the WHERE clause means the database serialises concurrent claims for us —
    no locks, no coordinator. `rowcount == 1` means we won. Losing is a normal
    outcome of broadcast routing, not an error: the other agents simply learn,
    by name, who got there first.

    The status filter (OPEN or MISSED) stops this from resurrecting a ticket
    `auto_resolve_stale_tickets` already closed out: that job resolves stale
    OPEN/IN_PROGRESS tickets without ever touching `assigned_to`, so an
    unassigned RESOLVED/CLOSED ticket is exactly what `assigned_to IS NULL`
    alone would let a stray tap claim back into IN_PROGRESS.
    """
    ticket = _owned_ticket(db, ticket_id, current_agent.tenant_id)

    result = db.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.assigned_to.is_(None),
            Ticket.status.in_([TicketStatus.OPEN, TicketStatus.MISSED]),
        )
        .values(
            assigned_to=current_agent.id,
            acknowledged_at=utcnow_naive(),
            status=TicketStatus.IN_PROGRESS,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    won = result.rowcount == 1

    if not won:
        db.refresh(ticket)
        if ticket.assigned_to == current_agent.id:
            # Already ours — a retry of our own successful claim. Idempotent win,
            # not a 409: repeat the session handoff and clear fanout too, since
            # either could have failed on the winning attempt before the
            # response ever reached the mobile app.
            return await _finish_claim(db, ticket, current_agent)
        handler = db.get(User, ticket.assigned_to) if ticket.assigned_to else None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=AcknowledgeLost(
                handled_by=handler.full_name if handler else "another agent"
            ).model_dump(),
        )

    return await _finish_claim(db, ticket, current_agent)
