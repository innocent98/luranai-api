"""Unit tests for TicketService.reopen_missed_ticket (N1)."""

import uuid

from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.services.ticket_service import TicketService


def _session_and_ticket(db, tenant_id, *, status: TicketStatus, assigned_to=None):
    session = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund request",
        priority=TicketPriority.HIGH,
        status=status,
        assigned_to=assigned_to,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return session, ticket


def test_reopen_missed_ticket_flips_to_open(db, agent_tenant):
    _, ticket = _session_and_ticket(db, agent_tenant.id, status=TicketStatus.MISSED)

    reopened = TicketService(db).reopen_missed_ticket(ticket.id)

    assert reopened is not None
    assert reopened.status == TicketStatus.OPEN
    assert reopened.assigned_to is None
    db_ticket = db.get(Ticket, ticket.id)
    assert db_ticket.status == TicketStatus.OPEN


def test_reopen_missed_ticket_is_none_if_already_claimed(db, agent_tenant):
    winner_id = uuid.uuid4()
    _, ticket = _session_and_ticket(
        db, agent_tenant.id, status=TicketStatus.MISSED, assigned_to=winner_id
    )

    reopened = TicketService(db).reopen_missed_ticket(ticket.id)

    assert reopened is None
    db_ticket = db.get(Ticket, ticket.id)
    assert (
        db_ticket.status == TicketStatus.MISSED
    ), "must not touch a ticket someone already claimed"
    assert db_ticket.assigned_to == winner_id


def test_reopen_missed_ticket_is_none_for_a_non_missed_ticket(db, agent_tenant):
    """Do-not-over-tighten's mirror image: an OPEN ticket is already live and
    ringing — reopening it is a no-op, not an error, but must not be reported
    as a successful reopen (the caller uses that signal to decide whether to
    re-ring)."""
    _, ticket = _session_and_ticket(db, agent_tenant.id, status=TicketStatus.OPEN)

    reopened = TicketService(db).reopen_missed_ticket(ticket.id)

    assert reopened is None
    db_ticket = db.get(Ticket, ticket.id)
    assert db_ticket.status == TicketStatus.OPEN
