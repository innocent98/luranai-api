"""Two concurrent escalations of one session must yield exactly one ticket.

`_get_or_create_escalation_ticket` was check-then-create with no lock: both
callers saw "no open ticket", both inserted. The fix serialises them on the
conversation_sessions row (SELECT ... FOR UPDATE) and re-checks inside the lock.

Needs real Postgres with committed rows from independent connections (the
`db_session` rollback fixture shares one connection, so it cannot race), so
this test creates and cleans up its own rows.
"""

import threading
import time
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import sessionmaker

from app.api.v1.endpoints import chat
from app.db.models.conversation import ConversationSession, SessionStatus, Ticket
from app.db.models.tenant import Tenant
from app.schemas.ticket import TicketCreate
from app.services.ticket_service import TicketService


@pytest.fixture
def committed_session(pg_engine):
    """A tenant + conversation session committed for real; removed afterwards."""
    factory = sessionmaker(bind=pg_engine)
    setup = factory()
    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"race_{prefix}",
        display_name=f"Race {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    setup.add(tenant)
    setup.flush()
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="race-visitor",
        status=SessionStatus.ACTIVE,
    )
    setup.add(session)
    setup.commit()
    tenant_id, session_id = tenant.id, session.id
    setup.close()

    yield factory, tenant_id, session_id

    cleanup = factory()
    cleanup.execute(delete(Ticket).where(Ticket.session_id == session_id))
    cleanup.execute(delete(ConversationSession).where(ConversationSession.id == session_id))
    cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
    cleanup.commit()
    cleanup.close()


def _ticket_data(tenant_id, session_id) -> TicketCreate:
    return TicketCreate(tenant_id=tenant_id, session_id=session_id, summary="race")


def test_second_escalation_waits_then_reuses_first_ticket(committed_session):
    factory, tenant_id, session_id = committed_session

    # Caller A is mid-escalation: it holds the session row lock and has not yet
    # created its ticket (the exact window where the old code let B in).
    a_db = factory()
    a_db.execute(
        text("SELECT id FROM conversation_sessions WHERE id = :id FOR UPDATE"),
        {"id": session_id},
    )

    result: dict = {}

    def caller_b():
        b_db = factory()
        try:
            result["ticket"], result["created"] = chat._get_or_create_escalation_ticket(
                TicketService(b_db), session_id, _ticket_data(tenant_id, session_id)
            )
        finally:
            b_db.close()

    b = threading.Thread(target=caller_b)
    b.start()
    try:
        time.sleep(1.0)
        assert b.is_alive(), "B must block on A's session row lock instead of racing ahead"
        a_ticket = TicketService(a_db).create_ticket(_ticket_data(tenant_id, session_id))
    finally:
        # Release A's lock on every path; a failed assertion above must not
        # leave the row locked and hang the fixture teardown's DELETE.
        a_db.rollback()
        a_db.close()
    b.join(timeout=10)
    assert not b.is_alive()

    assert result["created"] is False
    assert result["ticket"].id == a_ticket.id

    check = factory()
    try:
        count = len(check.execute(select(Ticket.id).where(Ticket.session_id == session_id)).all())
    finally:
        check.close()
    assert count == 1


def test_simultaneous_escalations_create_one_ticket(committed_session):
    factory, tenant_id, session_id = committed_session
    barrier = threading.Barrier(4)
    outcomes: list = []
    errors: list = []

    def caller():
        db = factory()
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                chat._get_or_create_escalation_ticket(
                    TicketService(db), session_id, _ticket_data(tenant_id, session_id)
                )
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced via assert below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, errors
    assert len({ticket.id for ticket, _ in outcomes}) == 1
    assert sum(1 for _, created in outcomes if created) == 1
