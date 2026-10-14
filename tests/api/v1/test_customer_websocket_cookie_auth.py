"""Cookie auth for the authenticated customer WebSocket (``/ws/customer/{id}``).

Mirrors the agent socket's CSWSH defence: the ambient cookie path requires a
trusted Origin, the ``?token=`` bearer path (non-browser clients) does not.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

import app.api.v1.endpoints.websocket as ws_module
from app.core.security import create_access_token, get_password_hash
from app.db.base import Base
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.main import app

ALLOWED_ORIGIN = "https://chat.luran.test"
DISALLOWED_ORIGIN = "https://evil.example"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cust_ws.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed = factory()
    try:
        tenant = Tenant(
            name=f"t_{uuid4().hex[:8]}",
            display_name="WS Tenant",
            api_key_hash=uuid4().hex,
            subscription_tier="pro",
            subscription_status="active",
        )
        seed.add(tenant)
        seed.flush()
        customer = CustomerUser(
            id=uuid4(),
            full_name="WS Customer",
            email=f"ws-{uuid4().hex[:8]}@example.com",
            hashed_password=get_password_hash("x"),
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        seed.add(customer)
        seed.flush()
        session = ConversationSession(
            tenant_id=tenant.id,
            user_identifier=customer.chat_user_key,
            customer_user_id=customer.id,
            status=SessionStatus.ACTIVE,
        )
        seed.add(session)
        seed.commit()
        customer_id, session_id = customer.id, session.id
        email = customer.email
    finally:
        seed.close()

    monkeypatch.setattr(ws_module, "SessionLocal", factory)
    monkeypatch.setattr(ws_module.settings, "BACKEND_CORS_ORIGINS", [ALLOWED_ORIGIN])

    def token(**kw) -> str:
        return create_access_token(
            subject=str(customer_id),
            additional_claims={"email": email, "auth_type": "customer"},
            **kw,
        )

    class E:
        pass

    e = E()
    e.url = f"/api/v1/ws/customer/{session_id}"
    e.token = token
    e.client = TestClient(app)
    yield e
    e.client.close()
    engine.dispose()


def _close_code(env, url, headers=None):
    with pytest.raises(WebSocketDisconnect) as exc:
        with env.client.websocket_connect(url, headers=headers or {}):
            pass
    return exc.value.code


def _ping(ws):
    ws.send_json({"type": "ping"})
    assert ws.receive_json()["type"] == "pong"


def _skip_replay(ws):
    return ws


def test_cookie_auth_with_allowed_origin_connects(env):
    env.client.cookies.set("customer_access_token", env.token())
    with env.client.websocket_connect(env.url, headers={"Origin": ALLOWED_ORIGIN}) as ws:
        _ping(ws)


def test_legacy_access_token_cookie_still_connects(env):
    env.client.cookies.set("access_token", env.token())
    with env.client.websocket_connect(env.url, headers={"Origin": ALLOWED_ORIGIN}) as ws:
        _ping(ws)


def test_cookie_with_disallowed_origin_rejected_4403(env):
    env.client.cookies.set("customer_access_token", env.token())
    assert _close_code(env, env.url, {"Origin": DISALLOWED_ORIGIN}) == 4403


def test_cookie_without_origin_rejected_4403(env):
    env.client.cookies.set("customer_access_token", env.token())
    assert _close_code(env, env.url) == 4403


def test_no_cookie_no_token_rejected_4003(env):
    assert _close_code(env, env.url, {"Origin": ALLOWED_ORIGIN}) == 4003


def test_expired_cookie_rejected_4003(env):
    env.client.cookies.set("customer_access_token", env.token(expires_delta=timedelta(seconds=-30)))
    assert _close_code(env, env.url, {"Origin": ALLOWED_ORIGIN}) == 4003


def test_tenant_jwt_in_customer_cookie_rejected_4003(env):
    env.client.cookies.set("customer_access_token", create_access_token(subject=str(uuid4())))
    assert _close_code(env, env.url, {"Origin": ALLOWED_ORIGIN}) == 4003


def test_query_token_still_works_without_origin(env):
    with env.client.websocket_connect(f"{env.url}?token={env.token()}") as ws:
        _ping(ws)


def test_revoked_cookie_token_rejected_4003(env, monkeypatch):
    monkeypatch.setattr(ws_module, "is_token_revoked", lambda jti: True)
    env.client.cookies.set("customer_access_token", env.token())
    assert _close_code(env, env.url, {"Origin": ALLOWED_ORIGIN}) == 4003
