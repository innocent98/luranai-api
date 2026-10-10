"""A MISSED ticket must be claimable from every surface and must not stay MISSED.

Regression for prod TKT-2026-0007: dashboard takeover of a MISSED escalation
left `status=missed` with `assigned_to` set, and the transcript endpoint
returned the oldest 50 messages instead of the newest.
"""

import uuid
from datetime import timedelta
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
from app.schemas.ticket import TicketAssign
from app.services.ticket_service import TicketService
from app.utils.datetime import utcnow_naive


@pytest.fixture(autouse=True)
def _no_side_effects():
    with (
        patch("app.api.v1.endpoints.agent_app.enqueue_clear_alert_async", AsyncMock()),
        patch.object(connection_manager, "fanout_event", AsyncMock()),
    ):
        yield


def _make(db, tenant, *, status, assigned_to=None):
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=status,
        assigned_to=assigned_to,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


# --- service level -----------------------------------------------------------


@pytest.mark.parametrize("start", [TicketStatus.OPEN, TicketStatus.MISSED])
def test_assign_ticket_moves_open_and_missed_to_in_progress(db, agent_tenant, agent_user, start):
    ticket = _make(db, agent_tenant, status=start)

    result = TicketService(db).assign_ticket(ticket.id, TicketAssign(assigned_to=agent_user.id))

    assert result.status == TicketStatus.IN_PROGRESS
    assert result.assigned_to == agent_user.id


@pytest.mark.parametrize("terminal", [TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_assign_ticket_does_not_reopen_terminal_tickets(db, agent_tenant, agent_user, terminal):
    ticket = _make(db, agent_tenant, status=terminal)

    result = TicketService(db).assign_ticket(ticket.id, TicketAssign(assigned_to=agent_user.id))

    assert result.status == terminal


# --- dashboard takeover endpoint --------------------------------------------


def test_dashboard_takeover_of_missed_ticket_goes_in_progress(
    client, db, agent_tenant, auth_headers, agent_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)
    ticket_id, session_id, agent_id = ticket.id, ticket.session_id, agent_user.id

    resp = client.post(f"/api/v1/chat/sessions/{session_id}/takeover", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    fresh = db.get(Ticket, ticket_id)
    db.refresh(fresh)
    assert fresh.status == TicketStatus.IN_PROGRESS
    assert fresh.assigned_to == agent_id


# --- agent-app acknowledge ---------------------------------------------------


def test_acknowledge_missed_unassigned_transitions_to_in_progress(
    client, db, agent_tenant, auth_headers, agent_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)
    ticket_id, agent_id = ticket.id, agent_user.id

    resp = client.post(f"/api/v1/agent/escalations/{ticket_id}/acknowledge", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["won"] is True
    fresh = db.get(Ticket, ticket_id)
    db.refresh(fresh)
    assert fresh.status == TicketStatus.IN_PROGRESS
    assert fresh.assigned_to == agent_id


def test_acknowledge_in_progress_ticket_claimed_by_other_is_409(
    client, db, agent_tenant, auth_headers, admin_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=admin_user.id)

    resp = client.post(f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers)

    assert resp.status_code == 409


def test_acknowledge_same_agent_reclaim_is_idempotent(
    client, db, agent_tenant, auth_headers, agent_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=agent_user.id)

    resp = client.post(f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["won"] is True


# --- resolve a missed ticket -------------------------------------------------


def test_resolve_missed_ticket_works(client, db, agent_tenant, auth_headers, agent_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED, assigned_to=agent_user.id)
    ticket_id = ticket.id

    resp = client.post(
        f"/api/v1/tickets/{ticket_id}/resolve",
        json={"resolution_notes": "Sorted out by phone"},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"


# --- transcript window -------------------------------------------------------


def test_detail_transcript_is_newest_50_in_chronological_order(
    client, db, agent_tenant, auth_headers
):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)
    base = utcnow_naive() - timedelta(hours=1)
    for i in range(60):
        db.add(
            Message(
                session_id=ticket.session_id,
                role=MessageRole.USER,
                content=f"m{i:02d}",
                created_at=base + timedelta(seconds=i),
            )
        )
    db.commit()

    body = client.get(f"/api/v1/agent/escalations/{ticket.id}", headers=auth_headers).json()

    assert [m["content"] for m in body["transcript"]] == [f"m{i:02d}" for i in range(10, 60)]


def test_detail_transcript_short_conversation_unchanged(client, db, agent_tenant, auth_headers):
    ticket = _make(db, agent_tenant, status=TicketStatus.OPEN)
    base = utcnow_naive() - timedelta(minutes=5)
    for i in range(3):
        db.add(
            Message(
                session_id=ticket.session_id,
                role=MessageRole.USER,
                content=f"m{i}",
                created_at=base + timedelta(seconds=i),
            )
        )
    db.commit()

    body = client.get(f"/api/v1/agent/escalations/{ticket.id}", headers=auth_headers).json()

    assert [m["content"] for m in body["transcript"]] == ["m0", "m1", "m2"]


# --- review round: takeover atomicity, resolve guard, 409 reasons -------------


def _headers(user):
    from app.core.security import create_access_token

    return {
        "Authorization": "Bearer "
        + create_access_token(
            subject=str(user.id),
            additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
        )
    }


def test_takeover_by_second_agent_is_409_and_changes_nothing(
    client, db, agent_tenant, agent_user, admin_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=agent_user.id)
    ticket_id, session_id, holder_id = ticket.id, ticket.session_id, agent_user.id
    holder_name = agent_user.full_name

    resp = client.post(f"/api/v1/chat/sessions/{session_id}/takeover", headers=_headers(admin_user))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {"won": False, "handled_by": holder_name, "reason": "claimed"}
    db.expire_all()
    assert db.get(Ticket, ticket_id).assigned_to == holder_id
    assert db.get(ConversationSession, session_id).agent_id is None


def test_takeover_same_agent_is_idempotent(client, db, agent_tenant, auth_headers, agent_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=agent_user.id)
    session_id = ticket.session_id

    for _ in range(2):
        resp = client.post(f"/api/v1/chat/sessions/{session_id}/takeover", headers=auth_headers)
        assert resp.status_code == 200, resp.text


def test_takeover_of_resolved_ticket_is_409_resolved(client, db, agent_tenant, auth_headers):
    ticket = _make(db, agent_tenant, status=TicketStatus.RESOLVED)

    resp = client.post(f"/api/v1/chat/sessions/{ticket.session_id}/takeover", headers=auth_headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "won": False,
        "handled_by": "another agent",
        "reason": "resolved",
    }


def test_takeover_enqueues_ring_clear(client, db, agent_tenant, auth_headers, agent_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)
    ticket_id, session_id, agent_id = ticket.id, ticket.session_id, agent_user.id

    with patch("app.api.v1.endpoints.chat.enqueue_clear_alert_async", AsyncMock()) as enqueue:
        resp = client.post(f"/api/v1/chat/sessions/{session_id}/takeover", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    enqueue.assert_awaited_once_with(ticket_id, agent_id)


def test_acknowledge_resolved_unassigned_is_409_resolved(client, db, agent_tenant, auth_headers):
    ticket = _make(db, agent_tenant, status=TicketStatus.RESOLVED)

    resp = client.post(f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "won": False,
        "handled_by": "another agent",
        "reason": "resolved",
    }


def test_acknowledge_held_by_other_has_reason_claimed(
    client, db, agent_tenant, auth_headers, admin_user
):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=admin_user.id)

    resp = client.post(f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "won": False,
        "handled_by": admin_user.full_name,
        "reason": "claimed",
    }


def test_resolve_by_non_owner_agent_is_403(client, db, agent_tenant, auth_headers, admin_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=admin_user.id)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve", json={"resolution_notes": "x"}, headers=auth_headers
    )

    assert resp.status_code == 403


def test_resolve_unassigned_by_agent_is_403(client, db, agent_tenant, auth_headers):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve", json={"resolution_notes": "x"}, headers=auth_headers
    )

    assert resp.status_code == 403


def test_resolve_unassigned_missed_by_admin_works(client, db, agent_tenant, admin_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.MISSED)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve",
        json={"resolution_notes": "x"},
        headers=_headers(admin_user),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"


@pytest.mark.parametrize("final", [TicketStatus.RESOLVED, TicketStatus.CLOSED])
def test_resolve_already_final_ticket_is_409(client, db, agent_tenant, admin_user, final):
    ticket = _make(db, agent_tenant, status=final)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve",
        json={"resolution_notes": "x"},
        headers=_headers(admin_user),
    )

    assert resp.status_code == 409
