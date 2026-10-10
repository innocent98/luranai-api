"""Auth-source tests for the agent WebSocket endpoint.

Regression coverage for the staging bug where the web dashboard's WS handshake
was rejected with 403 because ``agent_websocket`` required a ``?token=`` query
param, while the dashboard authenticates via the httponly ``access_token``
cookie (same as REST). The endpoint now accepts the agent JWT from EITHER the
query param OR the cookie; the JWT itself and every authorization check applied
to it are unchanged.

Because the cookie is an ambient credential the browser attaches automatically
and WebSocket handshakes bypass CORS, the cookie path is Origin-gated against
``settings.BACKEND_CORS_ORIGINS`` to prevent Cross-Site WebSocket Hijacking
(CSWSH). The query-token path is a bearer secret and is intentionally NOT
Origin-gated (the Flutter app sends no Origin header). These tests cover both
the auth source and the Origin gate.

The endpoint opens its own DB sessions via ``SessionLocal`` (it does not use the
``get_db`` dependency), so these tests bind ``SessionLocal`` to a self-contained
SQLite database seeded with a committed agent user, tenant, and session. The
TestClient runs the ASGI app in a worker thread, so an on-disk SQLite file (not
``:memory:``) is used to keep the committed rows visible across connections.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

import app.api.v1.endpoints.websocket as ws_module
from app.core.security import create_access_token
from app.db.base import Base
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.main import app

ALLOWED_ORIGIN = "https://dashboard.luran.test"
DISALLOWED_ORIGIN = "https://evil.example"


@pytest.fixture()
def agent_ws_env(tmp_path, monkeypatch):
    """Seed a committed agent user + tenant + session and point SessionLocal at it.

    Returns a small namespace with the TestClient, the session id/url, the
    trusted Origin, and helpers to mint a valid or expired agent JWT. The CORS
    allowlist is pinned to ``ALLOWED_ORIGIN`` so the Origin gate is deterministic
    regardless of the ambient ``.env``.
    """
    db_path = tmp_path / "agent_ws.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    prefix = uuid4().hex[:8]
    seed = session_factory()
    try:
        tenant = Tenant(
            name=f"ws_tenant_{prefix}",
            display_name=f"WS Tenant {prefix}",
            api_key_hash=uuid4().hex,
            subscription_tier="pro",
            subscription_status="active",
        )
        seed.add(tenant)
        seed.flush()

        agent = User(
            email=f"agent_{prefix}@example.com",
            hashed_password="$2b$12$placeholder_hash_not_real",
            full_name=f"Agent {prefix}",
            role=UserRole.SUPPORT_AGENT,
            tenant_id=tenant.id,
            is_active=True,
        )
        seed.add(agent)

        session = ConversationSession(
            tenant_id=tenant.id,
            user_identifier=f"visitor-{prefix}",
            status=SessionStatus.ACTIVE,
        )
        seed.add(session)
        seed.commit()

        tenant_id = tenant.id
        agent_id = agent.id
        session_id = session.id
        user_identifier = session.user_identifier
    finally:
        seed.close()

    # The endpoint resolves its DB via SessionLocal, not the get_db override.
    monkeypatch.setattr(ws_module, "SessionLocal", session_factory)
    # Pin the CORS allowlist the Origin gate checks against.
    monkeypatch.setattr(ws_module.settings, "BACKEND_CORS_ORIGINS", [ALLOWED_ORIGIN])

    def valid_token() -> str:
        return create_access_token(
            subject=str(agent_id),
            additional_claims={"role": UserRole.SUPPORT_AGENT.value, "tenant_id": str(tenant_id)},
        )

    def expired_token() -> str:
        return create_access_token(
            subject=str(agent_id),
            additional_claims={"role": UserRole.SUPPORT_AGENT.value, "tenant_id": str(tenant_id)},
            expires_delta=timedelta(seconds=-30),
        )

    class Env:
        pass

    env = Env()
    env.session_id = session_id
    env.tenant_id = tenant_id
    env.user_identifier = user_identifier
    env.session_factory = session_factory
    env.url = f"/api/v1/ws/agent/{session_id}"
    env.allowed_origin = ALLOWED_ORIGIN
    env.valid_token = valid_token
    env.expired_token = expired_token
    env.client = TestClient(app)

    yield env

    env.client.close()
    engine.dispose()


def _assert_ping_pong(websocket) -> None:
    """A connected agent socket must answer ping with pong (proves accept())."""
    websocket.send_json({"type": "ping"})
    reply = websocket.receive_json()
    assert reply["type"] == "pong"


def _expect_close(env, url, *, headers=None):
    """Attempt a handshake expected to be rejected; return the close code."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with env.client.websocket_connect(url, headers=headers or {}):
            pass
    return exc_info.value.code


# --- Cookie path (dashboard): Origin-gated ---------------------------------


def test_agent_ws_authenticates_via_cookie_allowed_origin(agent_ws_env):
    """Dashboard path: JWT via access_token cookie + trusted Origin -> accepted."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.valid_token())
    with agent_ws_env.client.websocket_connect(
        agent_ws_env.url, headers={"Origin": agent_ws_env.allowed_origin}
    ) as websocket:
        _assert_ping_pong(websocket)


def test_agent_ws_cookie_disallowed_origin_rejected_4403(agent_ws_env):
    """CSWSH guard: cookie present but Origin NOT in allowlist -> 4403."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.valid_token())
    code = _expect_close(agent_ws_env, agent_ws_env.url, headers={"Origin": DISALLOWED_ORIGIN})
    assert code == 4403


def test_agent_ws_cookie_missing_origin_rejected_4403(agent_ws_env):
    """CSWSH guard: cookie present but no usable Origin -> 4403 (never reaches decode)."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.valid_token())
    code = _expect_close(agent_ws_env, agent_ws_env.url)
    assert code == 4403


def test_agent_ws_invalid_cookie_token_allowed_origin_rejected_4003(agent_ws_env):
    """Trusted Origin but malformed cookie token -> passes Origin gate, JWTError -> 4003."""
    agent_ws_env.client.cookies.set("access_token", "not-a-real-jwt")
    code = _expect_close(
        agent_ws_env, agent_ws_env.url, headers={"Origin": agent_ws_env.allowed_origin}
    )
    assert code == 4003


def test_agent_ws_expired_cookie_token_allowed_origin_rejected_4003(agent_ws_env):
    """Trusted Origin but expired cookie token -> 4003."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.expired_token())
    code = _expect_close(
        agent_ws_env, agent_ws_env.url, headers={"Origin": agent_ws_env.allowed_origin}
    )
    assert code == 4003


def test_agent_ws_no_token_no_cookie_allowed_origin_rejected_4003(agent_ws_env):
    """Trusted Origin but neither query token nor cookie present -> 4003."""
    code = _expect_close(
        agent_ws_env, agent_ws_env.url, headers={"Origin": agent_ws_env.allowed_origin}
    )
    assert code == 4003


# --- Query-token path (native app): NOT Origin-gated -----------------------


def test_agent_ws_authenticates_via_query_token_no_origin(agent_ws_env):
    """Flutter app path: JWT via ?token= with NO Origin header -> accepted."""
    url = f"{agent_ws_env.url}?token={agent_ws_env.valid_token()}"
    with agent_ws_env.client.websocket_connect(url) as websocket:
        _assert_ping_pong(websocket)


def test_agent_ws_query_token_bypasses_origin_gate(agent_ws_env):
    """A valid query token is a secret and is accepted even with a hostile Origin."""
    url = f"{agent_ws_env.url}?token={agent_ws_env.valid_token()}"
    with agent_ws_env.client.websocket_connect(
        url, headers={"Origin": DISALLOWED_ORIGIN}
    ) as websocket:
        _assert_ping_pong(websocket)


def test_agent_ws_invalid_query_token_rejected_4003(agent_ws_env):
    """Malformed token via the query param -> JWTError -> 4003 (no Origin gate)."""
    url = f"{agent_ws_env.url}?token=not-a-real-jwt"
    code = _expect_close(agent_ws_env, url)
    assert code == 4003


def test_agent_ws_expired_query_token_rejected_4003(agent_ws_env):
    """Expired token via the query param -> 4003."""
    url = f"{agent_ws_env.url}?token={agent_ws_env.expired_token()}"
    code = _expect_close(agent_ws_env, url)
    assert code == 4003


# --- attachments ride on every agent_message / customer_message frame --------


def _seed_agent_message_with_images(env):
    """HUMAN_AGENT message whose images were uploaded a then b but sent [b, a]."""
    from datetime import datetime

    from app.db.models.conversation import (
        AttachmentDirection,
        AttachmentModeration,
        AttachmentStatus,
        Message,
        MessageAttachment,
        MessageRole,
    )

    db = env.session_factory()
    try:
        msg = Message(
            session_id=env.session_id,
            role=MessageRole.HUMAN_AGENT,
            content="see photos",
            meta={"agent_name": "A", "agent_id": str(uuid4())},
        )
        db.add(msg)
        db.flush()
        ids = []
        for pos, (name, created) in enumerate([("b", 2), ("a", 1)]):
            att = MessageAttachment(
                message_id=msg.id,
                session_id=env.session_id,
                tenant_id=env.tenant_id,
                direction=AttachmentDirection.OUTBOUND,
                cloudinary_public_id=f"chat/{name}",
                url=f"https://c/{name}.jpg",
                content_type="image/png",
                width=4,
                height=3,
                bytes=10,
                moderation_status=AttachmentModeration.PASSED,
                status=AttachmentStatus.LINKED,
                position=pos,
                created_at=datetime(2026, 1, 1, 0, 0, created),
            )
            db.add(att)
            db.flush()
            ids.append(str(att.id))
        db.commit()
        return str(msg.id), ids
    finally:
        db.close()


EXPECTED_KEYS = {"id", "url", "content_type", "width", "height"}


def test_agent_socket_replay_agent_message_carries_attachments_in_send_order(agent_ws_env):
    env = agent_ws_env
    msg_id, att_ids = _seed_agent_message_with_images(env)
    token = env.valid_token()
    with env.client.websocket_connect(f"{env.url}?token={token}") as websocket:
        frame = websocket.receive_json()
    assert frame["type"] == "agent_message"
    assert frame["message_id"] == msg_id
    assert [a["id"] for a in frame["attachments"]] == att_ids
    assert set(frame["attachments"][0]) == EXPECTED_KEYS
    assert frame["attachments"][0]["width"] == 4


def test_customer_socket_replay_agent_message_carries_attachments(agent_ws_env):
    env = agent_ws_env
    msg_id, att_ids = _seed_agent_message_with_images(env)
    url = f"/api/v1/ws/chat/{env.session_id}?user_identifier={env.user_identifier}"
    with env.client.websocket_connect(url) as websocket:
        frame = websocket.receive_json()
    assert frame["type"] == "agent_message"
    assert frame["message_id"] == msg_id
    assert [a["id"] for a in frame["attachments"]] == att_ids
    assert set(frame["attachments"][0]) == EXPECTED_KEYS


def test_live_agent_text_frame_has_empty_attachments_array(agent_ws_env):
    from unittest.mock import AsyncMock, patch

    from app.core.websocket import connection_manager

    env = agent_ws_env
    token = env.valid_token()
    with patch.object(connection_manager, "fanout_event", AsyncMock()) as fan:
        with env.client.websocket_connect(f"{env.url}?token={token}") as websocket:
            websocket.send_json({"type": "message", "content": "hello"})
            _assert_ping_pong(websocket)
    sent = fan.await_args.kwargs["message"]
    assert sent["type"] == "agent_message"
    assert sent["attachments"] == []
