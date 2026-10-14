"""Staff see an allow-list of session meta, never a block-list remainder.

Session meta is client-supplied. Anything not explicitly allowed (a future credential,
an internal id, flow state) must stay off every staff surface by default.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from jose import jwt

from app.api.deps import get_current_platform_admin
from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import ConversationSession, Message, MessageRole
from app.db.models.customer_user import CustomerUser
from app.db.models.visitor import Visitor, VisitorSource
from app.main import app
from app.utils.datetime import utcnow_naive
from app.utils.staff_redaction import STAFF_VISIBLE_SESSION_META_KEYS, redact_staff_session_meta
from tests.api.v1.test_agent_websocket_auth import agent_ws_env  # noqa: F401 (fixture)
from tests.rating.conftest import (
    admin,  # noqa: F401 (fixture)
    agent,  # noqa: F401 (fixture)
    headers,
    make_rating,
    make_ticket,
    tenant,  # noqa: F401 (fixture)
)

SECRETS = {
    "visitor_token": "vtok-SECRET-0123456789",
    "visitor_session_token": "vsess-SECRET-9876543210",
    "future_api_token": "futuretok-SECRET-abcdef",
    "customer_user_id": "00000000-0000-4000-8000-0000000000aa",
    "rating_token": "ratetok-SECRET-123456",
}
USER_IDENTIFIER = "uident-SECRET-abcdef"


def _leaky_meta() -> dict:
    return {
        "entry_point": "widget",
        "page_url": "https://shop.example.test/pricing",
        "locale": "en-GB",
        "contact_email": "ada@example.test",
        **SECRETS,
        "nested": {"visitor_token": SECRETS["visitor_token"]},
    }


def assert_no_secrets(text: str) -> None:
    for value in (*SECRETS.values(), USER_IDENTIFIER):
        assert value not in text
    assert "user_identifier" not in text


def test_allow_list_unit():
    assert redact_staff_session_meta(None) is None
    assert redact_staff_session_meta({}) == {}
    out = redact_staff_session_meta(_leaky_meta())
    assert out == {
        "entry_point": "widget",
        "page_url": "https://shop.example.test/pricing",
        "locale": "en-GB",
        "contact_email": "ada@example.test",
    }
    assert set(out) <= STAFF_VISIBLE_SESSION_META_KEYS


def test_allow_list_drops_non_scalar_values_even_for_allowed_keys():
    assert redact_staff_session_meta({"page_url": {"token": "x"}, "source": "widget"}) == {
        "source": "widget"
    }


def test_allow_list_does_not_mutate_input():
    src = _leaky_meta()
    redact_staff_session_meta(src)
    assert src["visitor_token"] == SECRETS["visitor_token"]


def _seed(db, tenant, agent):
    ticket = make_ticket(db, tenant, assigned_to=agent.id)
    session = db.get(ConversationSession, ticket.session_id)
    session.user_identifier = USER_IDENTIFIER
    session.meta = _leaky_meta()
    db.add(
        Message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            content="Please rate",
            meta={"system_notice": "rating_request", "rating_token": SECRETS["rating_token"]},
        )
    )
    db.commit()
    return ticket, session


def test_staff_session_detail_and_list_use_allow_list(client, db, tenant, agent, admin):
    ticket, session = _seed(db, tenant, agent)
    detail = client.get(f"/api/v1/chat/sessions/{session.id}", headers=headers(admin))
    assert detail.status_code == 200, detail.text
    assert_no_secrets(detail.text)
    assert detail.json()["meta"] == {
        "entry_point": "widget",
        "page_url": "https://shop.example.test/pricing",
        "locale": "en-GB",
        "contact_email": "ada@example.test",
    }
    listing = client.get(f"/api/v1/chat/tenants/{tenant.id}/sessions", headers=headers(admin))
    assert listing.status_code == 200, listing.text
    assert str(session.id) in listing.text
    assert_no_secrets(listing.text)


def test_staff_messages_route_hides_credentials(client, db, tenant, agent, admin):
    _, session = _seed(db, tenant, agent)
    r = client.get(f"/api/v1/chat/sessions/{session.id}/messages", headers=headers(admin))
    assert r.status_code == 200, r.text
    assert_no_secrets(r.text)
    notice = [m for m in r.json() if (m["meta"] or {}).get("system_notice") == "rating_request"]
    assert notice, "the notice itself stays visible to staff"
    assert "rating_token" not in notice[0]["meta"]


def test_platform_ratings_route_hides_credentials(client, db, tenant, agent):
    ticket, session = _seed(db, tenant, agent)
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=SECRETS["visitor_token"],
        name="Ada Customer",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.flush()
    session.visitor_id = visitor.id
    db.commit()
    make_rating(
        db,
        session_id=session.id,
        tenant_id=tenant.id,
        rated_agent_id=agent.id,
        user_identifier=USER_IDENTIFIER,
    )
    app.dependency_overrides[get_current_platform_admin] = lambda: object()
    try:
        r = client.get("/api/v1/platform/reviews/ratings/all")
    finally:
        app.dependency_overrides.pop(get_current_platform_admin, None)
    assert r.status_code == 200, r.text
    assert r.json()["ratings"], "seeded rating must be listed"
    assert_no_secrets(r.text)
    assert r.json()["ratings"][0]["customer_label"] == "Ada Customer"


def test_authed_customer_socket_fanout_to_agents_hides_credentials(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    chat_user_key = "ckey-SECRET-0f0f0f0f"
    s = env.session_factory()
    try:
        customer = CustomerUser(
            full_name="Cus Tomer",
            email="cus@example.test",
            email_hash=uuid4().hex + uuid4().hex,
            hashed_password="x",
            chat_user_key=chat_user_key,
            is_active=True,
        )
        s.add(customer)
        s.flush()
        row = s.get(ConversationSession, env.session_id)
        row.customer_user_id = customer.id
        customer_id = customer.id
        s.commit()
    finally:
        s.close()
    token = jwt.encode(
        {
            "sub": str(customer_id),
            "auth_type": "customer",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    fanout = AsyncMock()
    with patch.object(connection_manager, "fanout_event", fanout):
        with TestClient(app) as customer_client:
            with customer_client.websocket_connect(
                f"/api/v1/ws/customer/{env.session_id}?token={token}"
            ) as ws:
                ws.send_json({"type": "message", "content": "hello from authed customer"})
                for _ in range(50):
                    if any(
                        c.kwargs["message"]["type"] == "customer_message"
                        for c in fanout.await_args_list
                    ):
                        break
                    ws.send_json({"type": "ping"})
                    ws.receive_json()
    # Presence frames also go through fanout_event; this test is about the message frame.
    message_calls = [
        c for c in fanout.await_args_list if c.kwargs["message"]["type"] == "customer_message"
    ]
    assert len(message_calls) == 1
    frame = message_calls[0].kwargs["message"]
    assert message_calls[0].kwargs["to_agents"] is True
    assert frame["type"] == "customer_message"
    dumped = json.dumps(frame)
    assert chat_user_key not in dumped and env.user_identifier not in dumped
    assert "user_identifier" not in frame and "chat_user_key" not in frame
    assert frame["customer_label"] == f"Visitor {str(env.session_id)[:8]}"
    assert UUID(frame["message_id"])
