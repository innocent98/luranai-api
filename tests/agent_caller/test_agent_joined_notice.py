"""The customer-facing "agent joined" notice on a mobile acknowledge.

Regression coverage for the parity bug: taking over an escalated chat from the
mobile Agent Caller (`POST /agent/escalations/{id}/acknowledge`) must produce the
same customer-visible join notice the web dashboard's takeover emits — a persisted
SYSTEM message plus an `agent_joined` WebSocket fanout to the customer. Before the
fix only the dashboard path emitted these, so mobile takeovers left the customer's
widget silent.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.core.websocket import connection_manager
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
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
def _fanout():
    """The customer-facing agent_joined WS fanout is asserted here, not delivered.

    Patching the method on the shared `connection_manager` singleton intercepts
    the call `emit_agent_joined` makes regardless of which module reaches it.
    """
    with patch.object(connection_manager, "fanout_event", AsyncMock()) as fan:
        yield fan


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


def _join_messages(db, session_id):
    """Persisted SYSTEM join notices for a session, newest-query order."""
    return [
        m
        for m in db.query(Message).filter(Message.session_id == session_id).all()
        if m.role == MessageRole.SYSTEM and (m.meta or {}).get("event") == "agent_joined"
    ]


def test_acknowledge_posts_join_notice_and_fans_out_to_customer(
    client, db, auth_headers, escalation, agent_user, _fanout
):
    ticket_id, session_id = escalation.id, escalation.session_id
    agent_user_id = agent_user.id
    agent_full_name = agent_user.full_name

    resp = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["won"] is True

    # (a) exactly one persisted SYSTEM join message, with the dashboard's exact
    #     content string and meta shape.
    joins = _join_messages(db, session_id)
    assert len(joins) == 1
    msg = joins[0]
    assert msg.content == f"{agent_full_name} joined the conversation"
    assert msg.meta == {"event": "agent_joined", "agent_id": str(agent_user_id)}

    # (b) the agent_joined event fanned out to the customer only, in the exact
    #     shape the widget renders on.
    _fanout.assert_awaited_once()
    kwargs = _fanout.await_args.kwargs
    assert kwargs["session_id"] == session_id
    assert kwargs["to_customer"] is True
    assert kwargs["to_agents"] is False
    payload = kwargs["message"]
    assert payload["type"] == "agent_joined"
    assert payload["agent_name"] == agent_full_name
    assert payload["agent_id"] == str(agent_user_id)
    assert payload["timestamp"] is not None


def test_reacknowledge_does_not_double_post_the_join_notice(
    client, db, auth_headers, escalation, agent_user, _fanout
):
    """The acknowledge retry path re-runs `_finish_claim`; the `agent_id` guard
    must make the join notice idempotent so a mobile retry (after a lost response)
    does not post a second "joined" message or re-ring the customer's widget."""
    ticket_id, session_id = escalation.id, escalation.session_id

    first = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    second = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert first.status_code == 200
    assert second.status_code == 200

    assert len(_join_messages(db, session_id)) == 1
    _fanout.assert_awaited_once()


def test_losing_the_claim_posts_no_join_notice(
    client, db, auth_headers, escalation, admin_user, _fanout
):
    """A losing claimant never joined the session, so it must emit no notice —
    only the winner's join is announced to the customer."""
    ticket_id, session_id = escalation.id, escalation.session_id
    # Another agent wins first via the same conditional UPDATE the endpoint uses.
    escalation.assigned_to = admin_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    db.commit()

    resp = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)
    assert resp.status_code == 409

    assert _join_messages(db, session_id) == []
    _fanout.assert_not_awaited()
