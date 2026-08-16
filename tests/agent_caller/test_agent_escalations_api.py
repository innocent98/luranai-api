"""API tests for the escalation list and detail views."""

import uuid
from datetime import timedelta

import pytest

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


@pytest.fixture
def escalation(db, agent_tenant):
    session = ConversationSession(
        tenant_id=agent_tenant.id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
        escalation_reason="Customer requested a refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    for role, content in (
        (MessageRole.USER, "I want my money back"),
        (MessageRole.ASSISTANT, "Let me get a human for you"),
    ):
        db.add(Message(session_id=session.id, role=role, content=content))
    ticket = Ticket(
        tenant_id=agent_tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund request",
        priority=TicketPriority.URGENT,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def test_list_returns_pending_escalation(client, auth_headers, escalation):
    resp = client.get("/api/v1/agent/escalations", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["escalation_id"] == str(escalation.id)
    assert row["status"] == "pending"
    assert row["priority"] == "critical"
    assert row["reason"] == "Customer requested a refund"
    assert row["customer_name"] == "Customer"


@pytest.mark.parametrize(
    "ticket_status,assigned,expected",
    [
        (TicketStatus.OPEN, False, "pending"),
        (TicketStatus.IN_PROGRESS, True, "in_progress"),
        (TicketStatus.RESOLVED, True, "resolved"),
        (TicketStatus.CLOSED, True, "resolved"),
        (TicketStatus.MISSED, False, "missed"),
    ],
)
def test_status_derivation(
    client, db, auth_headers, escalation, agent_user, ticket_status, assigned, expected
):
    escalation.status = ticket_status
    escalation.assigned_to = agent_user.id if assigned else None
    db.commit()
    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    assert rows[0]["status"] == expected


def test_window_hours_filters_old_escalations(client, db, auth_headers, escalation):
    escalation.created_at = utcnow_naive() - timedelta(hours=48)
    db.commit()
    rows = client.get("/api/v1/agent/escalations?window_hours=24", headers=auth_headers).json()
    assert rows == []
    rows = client.get("/api/v1/agent/escalations?window_hours=72", headers=auth_headers).json()
    assert len(rows) == 1


def test_list_is_tenant_scoped(client, db, auth_headers, escalation):
    other_tenant_id = uuid.uuid4()
    session = ConversationSession(
        tenant_id=other_tenant_id,
        user_identifier=f"visitor:{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ESCALATED,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(
        Ticket(
            tenant_id=other_tenant_id,
            session_id=session.id,
            ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
            summary="Not yours",
            priority=TicketPriority.LOW,
            status=TicketStatus.OPEN,
        )
    )
    db.commit()

    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    assert [r["escalation_id"] for r in rows] == [str(escalation.id)]


def test_detail_includes_transcript(client, auth_headers, escalation):
    resp = client.get(f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalation_id"] == str(escalation.id)
    assert [m["content"] for m in body["transcript"]] == [
        "I want my money back",
        "Let me get a human for you",
    ]
    assert body["handled_by"] is None


def test_detail_reports_handler_once_claimed(client, db, auth_headers, escalation, agent_user):
    escalation.assigned_to = agent_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    escalation.acknowledged_at = utcnow_naive()
    db.commit()
    body = client.get(f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers).json()
    assert body["handled_by"] == agent_user.full_name
    assert body["acknowledged_at"] is not None


def test_cross_tenant_detail_is_404_not_403(client, db, auth_headers):
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

    resp = client.get(f"/api/v1/agent/escalations/{ticket.id}", headers=auth_headers)
    assert resp.status_code == 404


def test_unknown_detail_is_404(client, auth_headers):
    resp = client.get(f"/api/v1/agent/escalations/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
