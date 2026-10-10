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


# --- round 3: optional resolution note, ticket_id exposure, session_id filter --


def test_resolve_without_notes_is_200_with_null_notes(client, db, agent_tenant, admin_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS, assigned_to=admin_user.id)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve", json={}, headers=_headers(admin_user)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["resolution_notes"] is None


def test_resolve_blank_notes_stored_as_null_and_note_is_stripped(
    client, db, agent_tenant, admin_user
):
    blank = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS)
    noted = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS)
    h = _headers(admin_user)
    blank_id, noted_id = blank.id, noted.id

    r1 = client.post(
        f"/api/v1/tickets/{blank_id}/resolve", json={"resolution_notes": "   "}, headers=h
    )
    r2 = client.post(
        f"/api/v1/tickets/{noted_id}/resolve", json={"resolution_notes": "  Fixed it  "}, headers=h
    )

    assert r1.status_code == 200 and r1.json()["resolution_notes"] is None
    assert r2.status_code == 200 and r2.json()["resolution_notes"] == "Fixed it"


def test_resolve_overlong_notes_is_422(client, db, agent_tenant, admin_user):
    ticket = _make(db, agent_tenant, status=TicketStatus.IN_PROGRESS)

    resp = client.post(
        f"/api/v1/tickets/{ticket.id}/resolve",
        json={"resolution_notes": "x" * 5001},
        headers=_headers(admin_user),
    )

    assert resp.status_code == 422


def test_session_detail_exposes_latest_ticket_id(client, db, agent_tenant, admin_user):
    old = _make(db, agent_tenant, status=TicketStatus.RESOLVED)
    session_id = old.session_id
    newer = Ticket(
        tenant_id=agent_tenant.id,
        session_id=session_id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Again",
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
        created_at=utcnow_naive() + timedelta(minutes=1),
    )
    db.add(newer)
    db.commit()
    newer_id = str(newer.id)

    resp = client.get(f"/api/v1/chat/sessions/{session_id}", headers=_headers(admin_user))

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticket_id"] == newer_id


def test_session_detail_ticket_id_null_without_ticket(client, db, agent_tenant, admin_user):
    session = ConversationSession(
        tenant_id=agent_tenant.id,
        user_identifier="visitor:none",
        status=SessionStatus.ACTIVE,
    )
    db.add(session)
    db.commit()
    sid = session.id

    resp = client.get(f"/api/v1/chat/sessions/{sid}", headers=_headers(admin_user))

    assert resp.status_code == 200
    assert resp.json()["ticket_id"] is None


def test_tickets_list_filters_by_session_id(client, db, agent_tenant, admin_user):
    t1 = _make(db, agent_tenant, status=TicketStatus.OPEN)
    _make(db, agent_tenant, status=TicketStatus.OPEN)
    t1_id, t1_session = str(t1.id), t1.session_id

    resp = client.get(f"/api/v1/tickets?session_id={t1_session}", headers=_headers(admin_user))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["tickets"]] == [t1_id]
    assert body["total"] == 1


def test_tickets_session_id_filter_is_tenant_scoped(client, db, agent_tenant, admin_user):
    from app.db.models.tenant import Tenant, TenantStatus

    other = Tenant(
        name=f"other-{uuid.uuid4().hex[:6]}",
        display_name="Other",
        status=TenantStatus.ACTIVE,
        api_key_hash=f"hash-{uuid.uuid4().hex}",
        is_publicly_listed=False,
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(other)
    db.commit()
    foreign = _make(db, other, status=TicketStatus.OPEN)
    foreign_session = foreign.session_id

    resp = client.get(f"/api/v1/tickets?session_id={foreign_session}", headers=_headers(admin_user))

    assert resp.status_code == 200
    assert resp.json()["tickets"] == []
    assert resp.json()["total"] == 0
