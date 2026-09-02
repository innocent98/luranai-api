"""Unit tests for TicketService.get_open_ticket_for_session.

Added alongside the voice escalate-to-human tool (Task 10), which relies on
this method for idempotency: a caller repeating "get me a person" must reuse
the session's existing open ticket rather than open a second one.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models.conversation import Ticket, TicketStatus
from app.services.ticket_service import TicketService


def _make_ticket(
    db_session, *, tenant_id, session_id, status: TicketStatus, created_at=None
) -> Ticket:
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        ticket_number=f"TKT-TEST-{uuid4().hex[:10]}",
        status=status,
        summary="test ticket",
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    if created_at is not None:
        # Backdate deterministically so ordering-by-created_at assertions
        # don't depend on real-clock resolution between inserts.
        ticket.created_at = created_at
        db_session.commit()
        db_session.refresh(ticket)
    return ticket


def test_returns_none_when_session_has_no_tickets(db_session):
    service = TicketService(db_session)

    assert service.get_open_ticket_for_session(uuid4()) is None


def test_returns_none_when_only_closed_or_resolved_tickets_exist(
    db_session, voice_tenant_factory, voice_session_factory
):
    """A session whose only ticket was resolved/closed has no *open* ticket —
    it must be free to escalate again, not be stuck reusing a dead ticket."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    _make_ticket(db_session, tenant_id=tenant.id, session_id=session.id, status=TicketStatus.CLOSED)
    _make_ticket(
        db_session, tenant_id=tenant.id, session_id=session.id, status=TicketStatus.RESOLVED
    )
    service = TicketService(db_session)

    assert service.get_open_ticket_for_session(session.id) is None


def test_ignores_closed_ticket_and_returns_open_one(
    db_session, voice_tenant_factory, voice_session_factory
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    _make_ticket(db_session, tenant_id=tenant.id, session_id=session.id, status=TicketStatus.CLOSED)
    open_ticket = _make_ticket(
        db_session, tenant_id=tenant.id, session_id=session.id, status=TicketStatus.OPEN
    )
    service = TicketService(db_session)

    result = service.get_open_ticket_for_session(session.id)

    assert result is not None
    assert result.id == open_ticket.id


def test_returns_newest_open_ticket_without_raising_multiple_results_found(
    db_session, voice_tenant_factory, voice_session_factory
):
    """Regression guard for the bug documented on the sibling
    `get_ticket_by_session`: that method's `scalar_one_or_none()` with no
    `.limit(1)` raises `MultipleResultsFound` the moment a session has more
    than one matching ticket. `get_open_ticket_for_session` must not repeat
    that mistake, and must resolve ties by returning the newest."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    now = datetime.now(UTC)
    older = _make_ticket(
        db_session,
        tenant_id=tenant.id,
        session_id=session.id,
        status=TicketStatus.IN_PROGRESS,
        created_at=now - timedelta(hours=1),
    )
    newer = _make_ticket(
        db_session,
        tenant_id=tenant.id,
        session_id=session.id,
        status=TicketStatus.OPEN,
        created_at=now,
    )
    service = TicketService(db_session)

    result = service.get_open_ticket_for_session(session.id)

    assert result is not None
    assert result.id == newer.id
    assert result.id != older.id
