"""Route-level regressions for the Track B ticket follow-ups.

- GET /tickets/unassigned was shadowed by GET /tickets/{ticket_id} (422 on UUID parse).
- Manual POST /tickets and voice escalation must go through the same per-session
  lock + live-ticket guard as the chat escalation path.
"""

import uuid
from unittest.mock import patch

from sqlalchemy import select

from app.db.models.conversation import ConversationSession, SessionStatus, Ticket, TicketStatus
from app.services.ticket_service import TicketService
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user, _ticket


def test_unassigned_route_is_not_shadowed_by_ticket_id(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    open_unassigned = _ticket(db_session, user.tenant_id, number="TKT-U-1")
    assigned = _ticket(db_session, user.tenant_id, number="TKT-U-2")
    assigned.assigned_to = user.id
    closed = _ticket(db_session, user.tenant_id, number="TKT-U-3", status=TicketStatus.CLOSED)
    db_session.flush()

    resp = pg_client.get("/api/v1/tickets/unassigned", headers=_auth(token))

    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()}
    assert str(open_unassigned.id) in ids
    assert str(assigned.id) not in ids
    assert str(closed.id) not in ids


def test_ticket_by_id_still_resolves_and_bad_id_is_422(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    t = _ticket(db_session, user.tenant_id, number="TKT-U-9")

    ok = pg_client.get(f"/api/v1/tickets/{t.id}", headers=_auth(token))
    bad = pg_client.get("/api/v1/tickets/not-a-uuid", headers=_auth(token))

    assert ok.status_code == 200
    assert ok.json()["ticket_number"] == "TKT-U-9"
    assert bad.status_code == 422


def _session(db_session, tenant_id) -> ConversationSession:
    s = ConversationSession(
        tenant_id=tenant_id, user_identifier="manual-visitor", status=SessionStatus.ACTIVE
    )
    db_session.add(s)
    db_session.flush()
    return s


def test_manual_create_takes_session_lock(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    session = _session(db_session, user.tenant_id)

    with patch.object(TicketService, "lock_session_for_escalation", autospec=True) as lock:
        resp = pg_client.post(
            "/api/v1/tickets",
            json={"session_id": str(session.id), "summary": "manual one"},
            headers=_auth(token),
        )

    assert resp.status_code == 201, resp.text
    lock.assert_called_once()
    assert lock.call_args.args[1] == session.id


def test_manual_create_reuses_live_ticket_instead_of_duplicating(pg_client, db_session):
    user, token = _make_tenant_user(db_session)
    session = _session(db_session, user.tenant_id)
    body = {"session_id": str(session.id), "summary": "manual"}

    first = pg_client.post("/api/v1/tickets", json=body, headers=_auth(token))
    second = pg_client.post("/api/v1/tickets", json=body, headers=_auth(token))

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    rows = db_session.execute(select(Ticket).where(Ticket.session_id == session.id)).scalars().all()
    assert len(rows) == 1


def test_voice_escalation_takes_session_lock(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    from app.core.config import settings

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with (
        patch.object(settings, "VOICE_SHARED_SECRET", "s3cret"),
        patch.object(TicketService, "lock_session_for_escalation", autospec=True) as lock,
    ):
        resp = pg_client.post(
            "/api/v1/voice/tools/escalate_to_human",
            json={"voice_token": token, "reason": "human please"},
            headers={"Authorization": "Bearer s3cret"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticket_id"]
    lock.assert_called_once()
    assert lock.call_args.args[1] == session.id
    assert uuid.UUID(resp.json()["ticket_id"])


def test_guarded_create_reopens_missed_ticket_and_resets_ring(db_session):
    from unittest.mock import patch as _patch

    from app.schemas.ticket import TicketCreate
    from app.services.agent_alert_service import AgentAlertService

    user, _token = _make_tenant_user(db_session)
    missed = _ticket(db_session, user.tenant_id, number="TKT-M-1", status=TicketStatus.MISSED)

    with _patch.object(AgentAlertService, "reset_ring", autospec=True) as reset:
        ticket, created = TicketService(db_session).get_or_create_live_ticket(
            TicketCreate(tenant_id=user.tenant_id, session_id=missed.session_id, summary="again")
        )

    assert created is True
    assert ticket.id == missed.id
    assert ticket.status == TicketStatus.OPEN
    reset.assert_called_once()
    assert reset.call_args.args[1] == missed.id


def test_guarded_create_leaves_assigned_missed_ticket_alone(db_session):
    from app.schemas.ticket import TicketCreate

    user, _token = _make_tenant_user(db_session)
    missed = _ticket(db_session, user.tenant_id, number="TKT-M-2", status=TicketStatus.MISSED)
    missed.assigned_to = user.id
    db_session.flush()

    ticket, created = TicketService(db_session).get_or_create_live_ticket(
        TicketCreate(tenant_id=user.tenant_id, session_id=missed.session_id, summary="again")
    )

    assert created is False
    assert ticket.id == missed.id
    assert ticket.status == TicketStatus.MISSED
