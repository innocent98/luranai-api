"""API tests for the escalation list and detail views."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.visitor import Visitor, VisitorSource
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


@pytest.fixture
def identified_visitor(db, agent_tenant):
    """A visitor who has given a name — the identified-customer case."""
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=agent_tenant.id,
        public_token=f"tok-{uuid.uuid4().hex}",
        name="Ada Lovelace",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.commit()
    db.refresh(visitor)
    return visitor


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
    # Unidentified public-widget visitor: the app must be able to tell this
    # apart from a named customer, so the name is null (not the "Customer"
    # placeholder the ring push uses) and it renders `#<ticket_number>` instead.
    assert row["customer_name"] is None
    assert row["ticket_number"] == escalation.ticket_number


def test_list_customer_name_is_real_name_for_identified_visitor(
    client, db, auth_headers, escalation, identified_visitor
):
    session = db.get(ConversationSession, escalation.session_id)
    session.visitor_id = identified_visitor.id
    db.commit()
    row = client.get("/api/v1/agent/escalations", headers=auth_headers).json()[0]
    assert row["customer_name"] == "Ada Lovelace"
    assert row["ticket_number"] == escalation.ticket_number


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
    assert body["handled_by_user_id"] is None
    # Detail extends the summary, so it carries ticket_number and the same
    # nullable customer_name contract (unidentified visitor -> null).
    assert body["ticket_number"] == escalation.ticket_number
    assert body["customer_name"] is None


def test_detail_customer_name_is_real_name_for_identified_visitor(
    client, db, auth_headers, escalation, identified_visitor
):
    session = db.get(ConversationSession, escalation.session_id)
    session.visitor_id = identified_visitor.id
    db.commit()
    escalation_id = escalation.id
    body = client.get(f"/api/v1/agent/escalations/{escalation_id}", headers=auth_headers).json()
    assert body["customer_name"] == "Ada Lovelace"
    assert body["ticket_number"] == escalation.ticket_number


def test_detail_reports_handler_once_claimed(client, db, auth_headers, escalation, agent_user):
    escalation.assigned_to = agent_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    escalation.acknowledged_at = utcnow_naive()
    db.commit()
    body = client.get(f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers).json()
    # Assert both together: a future refactor must not silently swap the
    # display name for the id, or vice versa — the app needs both, for
    # different purposes (name to render, id to gate the Rejoin action).
    assert body["handled_by"] == agent_user.full_name
    assert body["handled_by_user_id"] == str(agent_user.id)
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


def test_device_token_never_appears_in_any_response_body(client, auth_headers, escalation):
    """The FCM registration token is written by POST /devices but must never
    come back out of any endpoint — not /me, not the escalations list, not
    the detail view, not the acknowledge response. `AgentDevice.device_token`
    exists purely to drive server-side pushes; asserting the negative here
    guards every response model in this router at once, not just the one
    that currently happens to serialise the field."""
    # Capture the id before issuing any request — the `client` fixture's
    # dependency override closes the shared `db` session after every request
    # (see tests/conftest.py), which detaches `escalation` from it.
    escalation_id = escalation.id
    token = f"tok-{uuid.uuid4().hex}-{uuid.uuid4().hex}"  # realistic FCM-length token
    register_resp = client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": token, "platform": "android"},
    )
    assert register_resp.status_code == 204
    assert token not in register_resp.text

    me_resp = client.get("/api/v1/agent/me", headers=auth_headers)
    assert token not in me_resp.text

    list_resp = client.get("/api/v1/agent/escalations", headers=auth_headers)
    assert token not in list_resp.text

    detail_resp = client.get(f"/api/v1/agent/escalations/{escalation_id}", headers=auth_headers)
    assert token not in detail_resp.text

    with patch("app.api.v1.endpoints.agent_app.enqueue_clear_alert_async", AsyncMock()):
        ack_resp = client.post(
            f"/api/v1/agent/escalations/{escalation_id}/acknowledge", headers=auth_headers
        )
    assert ack_resp.status_code == 200
    assert token not in ack_resp.text


def test_detail_transcript_includes_attachments(client, db, auth_headers, escalation):
    """Image refs ride on the transcript row; messages without images return []."""
    user_msg = db.execute(
        select(Message).where(
            Message.session_id == escalation.session_id, Message.role == MessageRole.USER
        )
    ).scalar_one()
    att = MessageAttachment(
        session_id=escalation.session_id,
        tenant_id=escalation.tenant_id,
        message_id=user_msg.id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="chat/x",
        url="https://c/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.LINKED,
    )
    db.add(att)
    db.commit()

    body = client.get(f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers).json()
    with_img, without_img = body["transcript"]
    assert with_img["attachments"] == [
        {
            "id": str(att.id),
            "url": "https://c/x.jpg",
            "content_type": "image/png",
            "width": 4,
            "height": 3,
            "kind": "image",
            "duration_ms": None,
            "poster_url": None,
            "expired": False,
        }
    ]
    assert without_img["attachments"] == []
