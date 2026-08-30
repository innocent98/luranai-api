"""Auth-source tests for the agent WebSocket endpoint.

Regression coverage for the staging bug where the web dashboard's WS handshake
was rejected with 403 because ``agent_websocket`` required a ``?token=`` query
param, while the dashboard authenticates via the httponly ``access_token``
cookie (same as REST). The endpoint now accepts the agent JWT from EITHER the
query param OR the cookie; the JWT itself and every authorization check applied
to it are unchanged.

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


@pytest.fixture()
def agent_ws_env(tmp_path, monkeypatch):
    """Seed a committed agent user + tenant + session and point SessionLocal at it.

    Returns a small namespace with the TestClient, the session id, and helpers
    to mint a valid or expired agent JWT for that seeded agent.
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
    finally:
        seed.close()

    # The endpoint resolves its DB via SessionLocal, not the get_db override.
    monkeypatch.setattr(ws_module, "SessionLocal", session_factory)

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
    env.url = f"/api/v1/ws/agent/{session_id}"
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


def test_agent_ws_authenticates_via_cookie(agent_ws_env):
    """Dashboard path: JWT supplied via the access_token cookie, no query token."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.valid_token())
    with agent_ws_env.client.websocket_connect(agent_ws_env.url) as websocket:
        _assert_ping_pong(websocket)


def test_agent_ws_authenticates_via_query_token(agent_ws_env):
    """Flutter app path: JWT supplied via the ?token= query param."""
    url = f"{agent_ws_env.url}?token={agent_ws_env.valid_token()}"
    with agent_ws_env.client.websocket_connect(url) as websocket:
        _assert_ping_pong(websocket)


def test_agent_ws_rejects_when_no_token_present(agent_ws_env):
    """Neither query token nor cookie -> rejected with 4003."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with agent_ws_env.client.websocket_connect(agent_ws_env.url):
            pass
    assert exc_info.value.code == 4003


def test_agent_ws_rejects_invalid_query_token(agent_ws_env):
    """Malformed token via the query param -> JWTError -> 4003."""
    url = f"{agent_ws_env.url}?token=not-a-real-jwt"
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with agent_ws_env.client.websocket_connect(url):
            pass
    assert exc_info.value.code == 4003


def test_agent_ws_rejects_invalid_cookie_token(agent_ws_env):
    """Malformed token via the cookie -> JWTError -> 4003 (cookie IS read)."""
    agent_ws_env.client.cookies.set("access_token", "not-a-real-jwt")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with agent_ws_env.client.websocket_connect(agent_ws_env.url):
            pass
    assert exc_info.value.code == 4003


def test_agent_ws_rejects_expired_query_token(agent_ws_env):
    """Expired token via the query param -> 4003."""
    url = f"{agent_ws_env.url}?token={agent_ws_env.expired_token()}"
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with agent_ws_env.client.websocket_connect(url):
            pass
    assert exc_info.value.code == 4003


def test_agent_ws_rejects_expired_cookie_token(agent_ws_env):
    """Expired token via the cookie -> 4003."""
    agent_ws_env.client.cookies.set("access_token", agent_ws_env.expired_token())
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with agent_ws_env.client.websocket_connect(agent_ws_env.url):
            pass
    assert exc_info.value.code == 4003
