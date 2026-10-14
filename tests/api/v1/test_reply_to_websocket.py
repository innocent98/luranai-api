"""N2 reply-to over the three chat WebSockets (customer, authed customer, agent)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.core.websocket import connection_manager
from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.main import app
from tests.api.v1.test_agent_websocket_auth import agent_ws_env  # noqa: F401 (fixture)


def _seed(env, role, content, meta=None):
    db = env.session_factory()
    try:
        m = Message(session_id=env.session_id, role=role, content=content, meta=meta or {})
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()


def _stored(env):
    db = env.session_factory()
    try:
        return list(
            db.execute(select(Message).where(Message.session_id == env.session_id)).scalars()
        )
    finally:
        db.close()


def _set_status(env, status):
    db = env.session_factory()
    try:
        db.get(ConversationSession, env.session_id).status = status
        db.commit()
    finally:
        db.close()


def _customer_url(env):
    return f"/api/v1/ws/chat/{env.session_id}?user_identifier={env.user_identifier}"


def _recv_until(ws, wanted_type, tries=30):
    for _ in range(tries):
        frame = ws.receive_json()
        if frame.get("type") == wanted_type:
            return frame
    raise AssertionError(f"no {wanted_type} frame")


def _customer_send(env, payload):
    """Send one frame on the customer socket; return (fanout mock, frame received back)."""
    fanout = AsyncMock()
    received = None
    with patch.object(connection_manager, "fanout_event", fanout):
        with TestClient(app) as c, c.websocket_connect(_customer_url(env)) as ws:
            ws.send_json(payload)
            for _ in range(50):
                if fanout.await_count:
                    break
                ws.send_json({"type": "ping"})
                frame = ws.receive_json()
                if frame.get("type") == "error":
                    received = frame
                    break
    return fanout, received


def test_customer_ws_reply_fans_out_reply_to_and_persists(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    original = _seed(env, MessageRole.HUMAN_AGENT, "Try restarting the router")
    fanout, _ = _customer_send(
        env,
        {"type": "message", "content": "did that", "reply_to_message_id": str(original)},
    )
    assert fanout.await_count == 1
    frame = fanout.await_args.kwargs["message"]
    assert frame["type"] == "customer_message"
    assert frame["reply_to"] == {
        "message_id": str(original),
        "sender_type": "agent",
        "kind": "text",
        "snippet": "Try restarting the router",
    }
    saved = next(m for m in _stored(env) if m.content == "did that")
    assert saved.reply_to_message_id == original


def test_customer_ws_plain_message_frame_has_null_reply_to(agent_ws_env):  # noqa: F811
    fanout, _ = _customer_send(agent_ws_env, {"type": "message", "content": "hello"})
    assert fanout.await_args.kwargs["message"]["reply_to"] is None


def test_customer_ws_invalid_reply_returns_error_frame_and_stores_nothing(
    agent_ws_env,  # noqa: F811
):
    env = agent_ws_env
    system = _seed(env, MessageRole.SYSTEM, "Agent joined")
    cases = [
        (str(uuid4()), "REPLY_TARGET_NOT_FOUND"),
        ("not-a-uuid", "REPLY_TARGET_NOT_FOUND"),
        (str(system), "REPLY_TARGET_NOT_QUOTABLE"),
    ]
    for target, code in cases:
        fanout, error = _customer_send(
            env, {"type": "message", "content": "nope", "reply_to_message_id": target}
        )
        assert error is not None and error["type"] == "error", code
        assert error["code"] == code
        assert isinstance(error["message"], str)
        assert fanout.await_count == 0
    assert all(m.content != "nope" for m in _stored(env))


def test_customer_ws_reply_in_resolved_session_rejected(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    original = _seed(env, MessageRole.ASSISTANT, "anything else?")
    _set_status(env, SessionStatus.RESOLVED)
    fanout, error = _customer_send(
        env, {"type": "message", "content": "yes", "reply_to_message_id": str(original)}
    )
    assert error["code"] == "REPLY_SESSION_CLOSED"
    assert fanout.await_count == 0


def test_agent_ws_reply_fans_out_to_customer_and_replays_with_reply_to(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    original = _seed(env, MessageRole.USER, "my order is late")
    fanout = AsyncMock()
    url = f"{env.url}?token={env.valid_token()}"
    with patch.object(connection_manager, "fanout_event", fanout):
        with env.client.websocket_connect(url) as ws:
            ws.send_json(
                {"type": "message", "content": "on it", "reply_to_message_id": str(original)}
            )
            for _ in range(50):
                if fanout.await_count:
                    break
                ws.send_json({"type": "ping"})
                ws.receive_json()
    frame = fanout.await_args.kwargs["message"]
    assert frame["type"] == "agent_message"
    assert fanout.await_args.kwargs["to_customer"] is True
    assert frame["reply_to"] == {
        "message_id": str(original),
        "sender_type": "customer",
        "kind": "text",
        "snippet": "my order is late",
    }

    # a reconnecting agent and customer both get the quote on replay
    with env.client.websocket_connect(url) as ws:
        replayed = [ws.receive_json() for _ in range(2)]
    by_content = {f["content"]: f for f in replayed}
    assert by_content["on it"]["reply_to"]["message_id"] == str(original)
    assert by_content["my order is late"]["reply_to"] is None

    with TestClient(app) as c, c.websocket_connect(_customer_url(env)) as ws:
        customer_replay = ws.receive_json()
    assert customer_replay["type"] == "agent_message"
    assert customer_replay["reply_to"]["snippet"] == "my order is late"
    assert "user_identifier" not in str(customer_replay)


def test_agent_ws_invalid_reply_returns_error_frame(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    url = f"{env.url}?token={env.valid_token()}"
    fanout = AsyncMock()
    with patch.object(connection_manager, "fanout_event", fanout):
        with env.client.websocket_connect(url) as ws:
            ws.send_json({"type": "message", "content": "x", "reply_to_message_id": str(uuid4())})
            error = _recv_until(ws, "error")
    assert error["code"] == "REPLY_TARGET_NOT_FOUND"
    assert fanout.await_count == 0


def test_authed_customer_ws_reply(agent_ws_env):  # noqa: F811
    env = agent_ws_env
    db = env.session_factory()
    try:
        customer = CustomerUser(
            id=uuid4(),
            full_name="Ada Customer",
            email="ada.ws@example.com",
            hashed_password="hashed",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        db.add(customer)
        db.get(ConversationSession, env.session_id).customer_user_id = customer.id
        db.commit()
        token = create_access_token(
            subject=str(customer.id),
            additional_claims={"email": customer.email, "auth_type": "customer"},
        )
    finally:
        db.close()
    original = _seed(env, MessageRole.ASSISTANT, "Need anything else?")

    fanout = AsyncMock()
    with patch.object(connection_manager, "fanout_event", fanout):
        with (
            TestClient(app) as c,
            c.websocket_connect(f"/api/v1/ws/customer/{env.session_id}?token={token}") as ws,
        ):
            ws.send_json(
                {"type": "message", "content": "yes", "reply_to_message_id": str(original)}
            )
            for _ in range(50):
                if fanout.await_count:
                    break
                ws.send_json({"type": "ping"})
                ws.receive_json()
    frame = fanout.await_args.kwargs["message"]
    assert frame["reply_to"] == {
        "message_id": str(original),
        "sender_type": "ai",
        "kind": "text",
        "snippet": "Need anything else?",
    }
