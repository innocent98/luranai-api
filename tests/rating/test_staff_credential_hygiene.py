"""Customer bearer credentials never reach a staff surface.

`visitor.public_token`, `visitor_token` / `visitor_session_token` (stored in session
meta by the widget) and the raw `user_identifier` (the customer socket authenticates with
session_id + user_identifier) let a staff member impersonate the customer, read the
N4 rating token and forge a rating. Customer-facing surfaces must keep returning them.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_current_customer
from app.core.websocket import connection_manager
from app.api.v1.endpoints.chat import _fanout_customer_images
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.visitor import Visitor, VisitorSource
from app.main import app
from app.schemas.conversation import AttachmentOut
from app.schemas.customer_auth import CustomerUserResponse
from app.services.visitor_service import VisitorService
from app.utils.datetime import utcnow_naive
from tests.api.v1.test_agent_websocket_auth import agent_ws_env  # noqa: F401 (fixture)
from tests.rating.conftest import headers, make_rating, make_ticket

VISITOR_TOKEN = "vtok-SECRET-0123456789"
VISITOR_SESSION_TOKEN = "vsess-SECRET-9876543210"
USER_IDENTIFIER = "uident-SECRET-abcdef"


@pytest.fixture
def exposed(db, tenant, agent):
    """A ticketed session whose visitor/meta/identifier all carry secrets."""
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=VISITOR_TOKEN,
        name="Ada Customer",
        email="ada@example.test",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.flush()
    ticket = make_ticket(db, tenant, assigned_to=agent.id)
    session = db.get(ConversationSession, ticket.session_id)
    session.user_identifier = USER_IDENTIFIER
    session.visitor_id = visitor.id
    session.meta = {
        "entry_point": "widget",
        "visitor_token": VISITOR_TOKEN,
        "visitor_session_token": VISITOR_SESSION_TOKEN,
        "page": "/pricing",
    }
    db.add(Message(session_id=session.id, role=MessageRole.USER, content="help me"))
    db.commit()
    make_rating(
        db,
        session_id=session.id,
        tenant_id=tenant.id,
        rated_agent_id=agent.id,
        user_identifier=USER_IDENTIFIER,
    )
    return ticket, session, visitor


def assert_clean(text: str) -> None:
    for secret in (VISITOR_TOKEN, VISITOR_SESSION_TOKEN, USER_IDENTIFIER):
        assert secret not in text
    assert "public_token" not in text
    assert "user_identifier" not in text


# ---- staff REST ----------------------------------------------------------------------


def test_admin_session_detail_hides_credentials(client, exposed, admin):
    _, session, visitor = exposed
    r = client.get(f"/api/v1/chat/sessions/{session.id}", headers=headers(admin))
    assert r.status_code == 200, r.text
    assert_clean(r.text)
    body = r.json()
    assert body["visitor"]["id"] == str(visitor.id)
    assert body["visitor"]["name"] == "Ada Customer"
    assert body["customer_label"] == "Ada Customer"
    assert body["meta"]["page"] == "/pricing"  # non-secret meta survives
    assert "visitor_token" not in body["meta"]
    assert [m["content"] for m in body["messages"]] == ["help me"]


def test_admin_session_list_hides_credentials(client, exposed, admin, tenant):
    r = client.get(f"/api/v1/chat/tenants/{tenant.id}/sessions", headers=headers(admin))
    assert r.status_code == 200, r.text
    assert r.json(), "fixture session must be listed"
    assert_clean(r.text)
    assert r.json()[0]["customer_label"] == "Ada Customer"


def test_session_without_visitor_gets_non_secret_label(client, db, tenant, admin):
    ticket = make_ticket(db, tenant)
    session = db.get(ConversationSession, ticket.session_id)
    session.user_identifier = USER_IDENTIFIER
    db.commit()
    r = client.get(f"/api/v1/chat/sessions/{session.id}", headers=headers(admin))
    assert r.status_code == 200, r.text
    assert_clean(r.text)
    assert r.json()["customer_label"] == f"Visitor {str(session.id)[:8]}"


def test_takeover_response_hides_credentials(client, exposed, agent):
    ticket, session, _ = exposed
    with (
        patch("app.api.v1.endpoints.chat.enqueue_clear_alert_async", AsyncMock()),
        patch("app.api.v1.endpoints.chat.emit_agent_joined", AsyncMock()),
    ):
        r = client.post(f"/api/v1/chat/sessions/{session.id}/takeover", headers=headers(agent))
    assert r.status_code == 200, r.text
    assert_clean(r.text)


def test_ticket_detail_and_list_hide_credentials(client, exposed, admin):
    ticket, _, visitor = exposed
    detail = client.get(f"/api/v1/tickets/{ticket.id}", headers=headers(admin))
    assert detail.status_code == 200, detail.text
    assert_clean(detail.text)
    assert detail.json()["visitor"]["id"] == str(visitor.id)
    assert detail.json()["visitor"]["email"] == "ada@example.test"

    listing = client.get("/api/v1/tickets", headers=headers(admin))
    assert listing.status_code == 200, listing.text
    assert str(ticket.id) in listing.text
    assert_clean(listing.text)


def test_staff_ratings_list_hides_credentials(client, exposed, admin):
    _, session, _ = exposed
    r = client.get("/api/v1/reviews/session", headers=headers(admin))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert_clean(r.text)
    item = r.json()["ratings"][0]
    assert item["customer_label"] == "Ada Customer"
    assert item["session_id"] == str(session.id)


def test_agent_own_ratings_list_hides_credentials(client, exposed, agent):
    r = client.get("/api/v1/reviews/session", headers=headers(agent))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert_clean(r.text)


def test_agent_app_escalations_hide_credentials(client, exposed, agent):
    ticket, _, _ = exposed
    for path in ("/api/v1/agent/escalations", f"/api/v1/agent/escalations/{ticket.id}"):
        r = client.get(path, headers=headers(agent))
        assert r.status_code == 200, r.text
        assert_clean(r.text)


def test_ticket_search_cannot_probe_user_identifier(client, db, tenant, admin):
    """ILIKE on user_identifier is a char-by-char oracle for the socket credential."""
    ticket = make_ticket(db, tenant)
    session = db.get(ConversationSession, ticket.session_id)
    session.user_identifier = USER_IDENTIFIER
    db.commit()
    r = client.get("/api/v1/tickets", params={"q": "uident-SECRET"}, headers=headers(admin))
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


# ---- customer REST keeps what customers need ------------------------------------------


def test_public_create_session_still_returns_user_identifier(client, tenant):
    r = client.post(
        "/api/v1/chat/sessions",
        json={"tenant_id": str(tenant.id), "user_identifier": USER_IDENTIFIER},
    )
    assert r.status_code == 201, r.text
    assert r.json()["user_identifier"] == USER_IDENTIFIER


def test_customer_history_still_returns_user_identifier(client, exposed):
    _, session, _ = exposed
    now = datetime.now(UTC)
    customer = CustomerUserResponse(
        id=uuid.uuid4(),
        full_name="Cus Tomer",
        email="customer@example.com",
        chat_user_key=USER_IDENTIFIER,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_customer] = lambda: customer
    try:
        r = client.get(f"/api/v1/customer/conversations/{session.id}")
    finally:
        app.dependency_overrides.pop(get_current_customer, None)
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["user_identifier"] == USER_IDENTIFIER


def test_rating_submit_response_still_echoes_user_identifier_to_customer(client, db, exposed):
    ticket, session, _ = exposed
    r = client.post(
        "/api/v1/reviews/session",
        json={"session_id": str(session.id), "user_identifier": USER_IDENTIFIER, "score": 4},
    )
    assert r.status_code == 201, r.text
    assert r.json()["user_identifier"] == USER_IDENTIFIER


# ---- agent WebSocket frames -----------------------------------------------------------


def _seed_user_message(env, content="hello from customer"):
    db = env.session_factory()
    try:
        db.add(Message(session_id=env.session_id, role=MessageRole.USER, content=content))
        db.commit()
    finally:
        db.close()


def test_agent_ws_replay_frames_hide_user_identifier(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    _seed_user_message(env)
    url = f"{env.url}?token={env.valid_token()}"
    with env.client.websocket_connect(url) as ws:
        frame = None
        for _ in range(10):
            candidate = ws.receive_json()
            if candidate.get("type") == "customer_message":
                frame = candidate
                break
    assert frame is not None, "replay must emit the customer message"
    assert env.user_identifier not in str(frame)
    assert "user_identifier" not in frame
    assert frame["customer_label"] == f"Visitor {str(env.session_id)[:8]}"
    assert frame["content"] == "hello from customer"


def test_customer_ws_live_message_fanout_to_agents_hides_user_identifier(
    agent_ws_env,  # noqa: F811
):
    env = agent_ws_env
    fanout = AsyncMock()
    with patch.object(connection_manager, "fanout_event", fanout):
        url = f"/api/v1/ws/chat/{env.session_id}?user_identifier={env.user_identifier}"
        with TestClient(app) as customer_client:
            with customer_client.websocket_connect(url) as ws:
                ws.send_json({"type": "message", "content": "ping from customer"})
                for _ in range(50):
                    if fanout.await_count:
                        break
                    ws.send_json({"type": "ping"})
                    ws.receive_json()
    assert fanout.await_count == 1
    frame = fanout.await_args.kwargs["message"]
    assert fanout.await_args.kwargs["to_agents"] is True
    assert frame["type"] == "customer_message"
    assert "user_identifier" not in frame
    assert env.user_identifier not in str(frame)
    assert frame["customer_label"] == f"Visitor {str(env.session_id)[:8]}"


@pytest.mark.asyncio
async def test_image_fanout_to_agents_hides_user_identifier(db, exposed):
    _, session, _ = exposed
    msg = db.execute(select(Message).where(Message.session_id == session.id)).scalar_one()
    attachment = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        message_id=msg.id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="chat/x",
        url="https://c/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=AttachmentStatus.UPLOADED,
    )
    db.add(attachment)
    db.commit()
    fanout = AsyncMock()
    with patch.object(connection_manager, "fanout_event", fanout):
        await _fanout_customer_images(db, session.id, [AttachmentOut.model_validate(attachment)])
    assert fanout.await_count == 1
    frame = fanout.await_args.kwargs["message"]
    assert frame["type"] == "customer_message"
    assert "user_identifier" not in frame
    assert USER_IDENTIFIER not in str(frame)
    assert frame["customer_label"] == "Ada Customer"


# ---- bootstrap: newest 50, chronological ----------------------------------------------


def test_bootstrap_returns_newest_fifty_messages_in_order(db, tenant):
    tenant.public_slug = f"t-{uuid.uuid4().hex[:8]}"
    tenant.is_publicly_listed = True
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=f"tok-{uuid.uuid4().hex}",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.flush()
    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="anon",
        visitor_id=visitor.id,
        status=SessionStatus.ACTIVE,
    )
    db.add(session)
    db.flush()
    base = now - timedelta(hours=1)
    for i in range(60):
        db.add(
            Message(
                session_id=session.id,
                role=MessageRole.USER,
                content=f"m{i:02d}",
                created_at=base + timedelta(seconds=i),
            )
        )
    db.commit()

    summary = VisitorService(db)._get_latest_conversation_summary(visitor.id, tenant.id)
    assert summary is not None
    assert [m.content for m in summary.messages] == [f"m{i:02d}" for i in range(10, 60)]
