"""Ticket numbers must be unique under concurrency and keep the TKT-YYYY-NNNN format.

`_generate_ticket_number` used to be COUNT(*)+1: two different sessions escalating at
once both read the same count and collided on the unique index
`ix_tickets_ticket_number`. Numbers now come from a per-year counter row that is
incremented atomically (INSERT ... ON CONFLICT DO UPDATE ... RETURNING).

The race tests need independent connections with committed rows, so they create and
clean up their own data (the `db_session` rollback fixture shares one connection).
"""

import re
import threading
import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.db.models.conversation import ConversationSession, SessionStatus, Ticket
from app.db.models.tenant import Tenant
from app.schemas.ticket import TicketCreate
from app.services import ticket_service as ticket_service_module
from app.services.ticket_service import TicketService

NUMBER_RE = re.compile(r"^TKT-\d{4}-\d{4,}$")


def _tenant(db) -> Tenant:
    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"seq_{prefix}",
        display_name=f"Seq {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    return tenant


def _session(db, tenant_id) -> ConversationSession:
    s = ConversationSession(
        tenant_id=tenant_id, user_identifier="seq-visitor", status=SessionStatus.ACTIVE
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def committed_sessions(pg_engine):
    """One tenant + 8 committed sessions; everything removed afterwards."""
    factory = sessionmaker(bind=pg_engine)
    setup = factory()
    tenant = _tenant(setup)
    session_ids = [_session(setup, tenant.id).id for _ in range(8)]
    setup.commit()
    tenant_id = tenant.id
    setup.close()

    yield factory, tenant_id, session_ids

    cleanup = factory()
    cleanup.execute(delete(Ticket).where(Ticket.tenant_id == tenant_id))
    cleanup.execute(delete(ConversationSession).where(ConversationSession.tenant_id == tenant_id))
    cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
    cleanup.commit()
    cleanup.close()


def test_concurrent_escalations_of_different_sessions_get_distinct_numbers(committed_sessions):
    factory, tenant_id, session_ids = committed_sessions
    barrier = threading.Barrier(len(session_ids))
    numbers: list[str] = []
    errors: list[BaseException] = []

    def caller(session_id):
        db = factory()
        try:
            barrier.wait(timeout=10)
            ticket = TicketService(db).create_ticket(
                TicketCreate(tenant_id=tenant_id, session_id=session_id, summary="seq race")
            )
            numbers.append(ticket.ticket_number)
        except BaseException as exc:  # noqa: BLE001 -- surfaced via assert below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=caller, args=(sid,)) for sid in session_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(numbers) == len(session_ids)
    assert len(set(numbers)) == len(numbers), f"duplicate ticket numbers: {sorted(numbers)}"
    assert all(NUMBER_RE.match(n) for n in numbers), numbers


def test_numbers_are_sequential_and_keep_visible_format(db_session, monkeypatch):
    monkeypatch.setattr(ticket_service_module, "utcnow_naive", lambda: datetime(2099, 3, 1))
    tenant = _tenant(db_session)
    service = TicketService(db_session)

    first = service.create_ticket(
        TicketCreate(
            tenant_id=tenant.id, session_id=_session(db_session, tenant.id).id, summary="a"
        )
    )
    second = service.create_ticket(
        TicketCreate(
            tenant_id=tenant.id, session_id=_session(db_session, tenant.id).id, summary="b"
        )
    )

    assert first.ticket_number == "TKT-2099-0001"
    assert second.ticket_number == "TKT-2099-0002"


def test_counter_resets_per_year(db_session, monkeypatch):
    tenant = _tenant(db_session)
    service = TicketService(db_session)

    monkeypatch.setattr(ticket_service_module, "utcnow_naive", lambda: datetime(2098, 12, 31))
    a = service.create_ticket(
        TicketCreate(
            tenant_id=tenant.id, session_id=_session(db_session, tenant.id).id, summary="a"
        )
    )
    monkeypatch.setattr(ticket_service_module, "utcnow_naive", lambda: datetime(2097, 1, 1))
    b = service.create_ticket(
        TicketCreate(
            tenant_id=tenant.id, session_id=_session(db_session, tenant.id).id, summary="b"
        )
    )

    assert a.ticket_number == "TKT-2098-0001"
    assert b.ticket_number == "TKT-2097-0001"


def test_guarded_create_serialises_same_session(committed_sessions):
    """Manual POST /tickets and voice escalation share this path: two callers on one
    session must end up with exactly one ticket."""
    factory, tenant_id, session_ids = committed_sessions
    session_id = session_ids[0]
    barrier = threading.Barrier(4)
    outcomes: list = []
    errors: list[BaseException] = []

    def caller():
        db = factory()
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                TicketService(db).get_or_create_live_ticket(
                    TicketCreate(tenant_id=tenant_id, session_id=session_id, summary="dup")
                )
            )
        except BaseException as exc:  # noqa: BLE001 -- surfaced via assert below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len({ticket.id for ticket, _ in outcomes}) == 1
    assert sum(1 for _, created in outcomes if created) == 1
    check = factory()
    try:
        rows = check.execute(select(Ticket.id).where(Ticket.session_id == session_id)).all()
    finally:
        check.close()
    assert len(rows) == 1
