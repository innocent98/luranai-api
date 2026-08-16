"""API tests for the atomic escalation claim."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.utils.datetime import utcnow_naive


@pytest.fixture(autouse=True)
def _no_redis():
    """The clear fanout is enqueued, not executed, in these tests."""
    with patch("app.api.v1.endpoints.agent_app.enqueue_clear_alert_async", AsyncMock()) as enqueue:
        yield enqueue


@pytest.fixture
def escalation(db, agent_tenant):
    session = ConversationSession(
        tenant_id=agent_tenant.id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=agent_tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def test_first_claim_wins(client, db, auth_headers, escalation, agent_user):
    # The `client` fixture's dependency override closes the shared `db` session
    # after every request (see tests/conftest.py), detaching any ORM objects
    # already loaded in it. Capture ids before the request and re-query fresh
    # rows afterward instead of touching the original (now-detached) objects —
    # matching the pattern in tests/agent_caller/test_agent_devices_api.py.
    ticket_id, session_id = escalation.id, escalation.session_id
    agent_user_id = agent_user.id

    resp = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["won"] is True
    assert body["session_id"] == str(session_id)
    assert body["ws_url"].endswith(f"/api/v1/ws/agent/{session_id}")
    assert body["ws_url"].startswith("ws")

    ticket = db.get(Ticket, ticket_id)
    assert ticket.assigned_to == agent_user_id
    assert ticket.status == TicketStatus.IN_PROGRESS
    assert ticket.acknowledged_at is not None

    session = db.get(ConversationSession, session_id)
    assert session.agent_id == agent_user_id
    assert session.agent_joined_at is not None


def test_second_claim_loses_with_409(client, db, auth_headers, escalation, admin_user):
    ticket_id = escalation.id
    # Simulate the racing winner with the same conditional UPDATE the endpoint uses.
    db.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.assigned_to.is_(None))
        .values(
            assigned_to=admin_user.id,
            acknowledged_at=utcnow_naive(),
            status=TicketStatus.IN_PROGRESS,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    admin_full_name = admin_user.full_name

    resp = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"won": False, "handled_by": admin_full_name}


def test_reclaiming_your_own_escalation_is_idempotent(client, auth_headers, escalation):
    ticket_id = escalation.id
    first = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    second = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]


def test_winning_enqueues_the_clear(client, auth_headers, escalation, agent_user, _no_redis):
    ticket_id = escalation.id
    agent_user_id = agent_user.id
    client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    _no_redis.assert_awaited_once()
    args = _no_redis.await_args.args
    assert args[0] == ticket_id
    assert args[1] == agent_user_id


def test_losing_does_not_enqueue_a_clear(
    client, db, auth_headers, escalation, admin_user, _no_redis
):
    ticket_id = escalation.id
    escalation.assigned_to = admin_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    db.commit()
    client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    _no_redis.assert_not_awaited()


def test_idempotent_retry_repairs_side_effects_after_a_lost_response(
    client, db, auth_headers, escalation, agent_user, _no_redis
):
    """F1 regression: the winning attempt's clear-enqueue (or session handoff)
    can fail, and then the HTTP response itself can be lost to a flaky mobile
    network. The retry lands in the idempotent-win branch and must repeat
    BOTH side effects, not silently skip them forever."""
    ticket_id, session_id = escalation.id, escalation.session_id
    agent_user_id = agent_user.id
    _no_redis.side_effect = RuntimeError("redis blip")

    first = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert first.status_code == 200

    second = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert second.status_code == 200

    session = db.get(ConversationSession, session_id)
    assert session.agent_id == agent_user_id
    assert session.agent_joined_at is not None

    assert _no_redis.await_count == 2
    assert _no_redis.await_args.args == (ticket_id, agent_user_id)


def test_cross_tenant_acknowledge_is_404(client, db, auth_headers):
    other_tenant_id = uuid.uuid4()
    session = ConversationSession(
        tenant_id=other_tenant_id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=other_tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Not yours",
        priority=TicketPriority.LOW,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    resp = client.post(f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers)
    assert resp.status_code == 404


def test_ws_url_uses_wss_for_https_host(monkeypatch):
    from app.api.v1.endpoints import agent_app

    monkeypatch.setattr(agent_app.settings, "SERVER_HOST", "https://api.luranai.com")
    sid = uuid.uuid4()
    assert agent_app._ws_url(sid) == f"wss://api.luranai.com/api/v1/ws/agent/{sid}"

    monkeypatch.setattr(agent_app.settings, "SERVER_HOST", "http://localhost:8000")
    assert agent_app._ws_url(sid) == f"ws://localhost:8000/api/v1/ws/agent/{sid}"


def test_ws_url_defaults_to_wss_for_a_schemeless_host(monkeypatch):
    from app.api.v1.endpoints import agent_app

    monkeypatch.setattr(agent_app.settings, "SERVER_HOST", "api.luranai.com")
    sid = uuid.uuid4()
    assert agent_app._ws_url(sid) == f"wss://api.luranai.com/api/v1/ws/agent/{sid}"
