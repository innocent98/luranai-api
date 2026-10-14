"""Customer presence over the real WebSocket endpoints.

Endpoints open their own DB sessions via ``SessionLocal``, so (like
test_agent_websocket_auth) these bind it to an on-disk SQLite file with committed
rows. Redis is the real one from REDIS_URL. ``with TestClient`` keeps every socket on
one event loop so cross-socket fanout behaves as in production.
"""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.api.v1.endpoints.websocket as ws_module
from app.core.security import create_access_token
from app.db.base import Base
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.main import app
from app.services import customer_presence as cp
from app.services.customer_presence import presence_tracker


def _tenant(seed, prefix):
    t = Tenant(
        name=f"ws_{prefix}",
        display_name=f"WS {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="pro",
        subscription_status="active",
    )
    seed.add(t)
    seed.flush()
    return t


def _agent(seed, tenant, prefix):
    a = User(
        email=f"a_{prefix}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"Agent {prefix}",
        role=UserRole.SUPPORT_AGENT,
        tenant_id=tenant.id,
        is_active=True,
    )
    seed.add(a)
    seed.flush()
    return a


def _token(agent):
    return create_access_token(
        subject=str(agent.id),
        additional_claims={"role": UserRole.SUPPORT_AGENT.value, "tenant_id": str(agent.tenant_id)},
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'presence_ws.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed = factory()
    p1, p2 = uuid4().hex[:8], uuid4().hex[:8]
    t1, t2 = _tenant(seed, p1), _tenant(seed, p2)
    a1, a2 = _agent(seed, t1, p1), _agent(seed, t2, p2)
    customer = CustomerUser(
        id=uuid4(),
        full_name="Cust",
        email=f"c_{p1}@example.com",
        hashed_password="x",
        is_active=True,
    )
    seed.add(customer)
    seed.flush()
    s1 = ConversationSession(
        tenant_id=t1.id,
        user_identifier=f"visitor-{p1}",
        status=SessionStatus.ACTIVE,
        customer_user_id=customer.id,
    )
    s2 = ConversationSession(
        tenant_id=t2.id, user_identifier=f"visitor-{p2}", status=SessionStatus.ACTIVE
    )
    seed.add_all([s1, s2])
    seed.commit()

    class E:
        pass

    e = E()
    e.factory = factory
    e.sid, e.uid = s1.id, s1.user_identifier
    e.other_sid = s2.id
    e.agent_url = f"/api/v1/ws/agent/{s1.id}?token={_token(a1)}"
    e.other_agent_url = f"/api/v1/ws/agent/{s2.id}?token={_token(a2)}"
    e.customer_url = f"/api/v1/ws/chat/{s1.id}?user_identifier={s1.user_identifier}"
    cust_token = create_access_token(
        subject=str(customer.id), additional_claims={"auth_type": "customer"}
    )
    e.authed_customer_url = f"/api/v1/ws/customer/{s1.id}?token={cust_token}"
    monkeypatch.setattr(ws_module, "SessionLocal", factory)
    monkeypatch.setattr(cp, "_MIN_CHANGE_INTERVAL_S", 0)
    monkeypatch.setattr(cp, "_MIN_REFRESH_INTERVAL_S", 0)
    monkeypatch.setattr(presence_tracker, "grace_s", 0.2)
    with TestClient(app) as client:
        e.client = client
        yield e
    engine.dispose()


def _presence_frames(ws, n_expected_before_pong: int | None = None):
    """Drain frames until a pong; return the customer_presence ones."""
    ws.send_json({"type": "ping"})
    frames = []
    while True:
        msg = ws.receive_json()
        if msg["type"] == "pong":
            return frames
        if msg["type"] == "customer_presence":
            frames.append(msg)


def _next_presence(ws):
    while True:
        msg = ws.receive_json()
        if msg["type"] == "customer_presence":
            return msg


def test_connect_away_back_disconnect_flow(env):
    with env.client.websocket_connect(env.agent_url) as agent:
        with env.client.websocket_connect(env.customer_url) as customer:
            assert _next_presence(agent)["state"] == "active"
            customer.send_json({"type": "presence", "state": "away"})
            assert _next_presence(agent)["state"] == "away"
            customer.send_json({"type": "presence", "state": "away"})  # heartbeat: no frame
            customer.send_json({"type": "presence", "state": "active"})
            assert _next_presence(agent)["state"] == "active"
        # socket closed -> grace -> offline with last_seen_at
        off = _next_presence(agent)
        assert off["state"] == "offline"
        assert off["last_seen_at"] is not None
        assert off["session_id"] == str(env.sid)

    db = env.factory()
    try:
        row = db.get(ConversationSession, env.sid)
        assert row.customer_last_seen_at is not None
    finally:
        db.close()


def test_heartbeat_same_state_emits_no_frames(env):
    with env.client.websocket_connect(env.agent_url) as agent:
        with env.client.websocket_connect(env.customer_url) as customer:
            assert _next_presence(agent)["state"] == "active"
            for _ in range(3):
                customer.send_json({"type": "presence", "state": "active"})
            assert _presence_frames(agent) == []


def test_old_client_without_presence_frames(env):
    """No presence frame ever sent: connect -> active, disconnect -> offline."""
    with env.client.websocket_connect(env.agent_url) as agent:
        with env.client.websocket_connect(env.customer_url) as customer:
            customer.send_json({"type": "ping"})
            assert customer.receive_json()["type"] == "pong"
            assert _next_presence(agent)["state"] == "active"
        assert _next_presence(agent)["state"] == "offline"


def test_reload_inside_grace_does_not_flap(env, monkeypatch):
    monkeypatch.setattr(presence_tracker, "grace_s", 5)
    with env.client.websocket_connect(env.agent_url) as agent:
        with env.client.websocket_connect(env.customer_url):
            assert _next_presence(agent)["state"] == "active"
        with env.client.websocket_connect(env.customer_url):
            pass
        assert _presence_frames(agent) == []


def test_authenticated_customer_socket_reports_presence(env):
    with env.client.websocket_connect(env.agent_url) as agent:
        with env.client.websocket_connect(env.authed_customer_url) as customer:
            assert _next_presence(agent)["state"] == "active"
            customer.send_json({"type": "presence", "state": "away"})
            assert _next_presence(agent)["state"] == "away"
        assert _next_presence(agent)["state"] == "offline"


def test_agent_connect_sends_no_unsolicited_presence_frame(env):
    """Frames are change-only; a (re)connecting agent re-reads presence over REST."""
    with env.client.websocket_connect(env.customer_url) as customer:
        customer.send_json({"type": "presence", "state": "away"})
        time.sleep(0.2)
        with env.client.websocket_connect(env.agent_url) as agent:
            assert _presence_frames(agent) == []


def test_presence_never_reaches_other_tenant_agent(env):
    with env.client.websocket_connect(env.other_agent_url) as other_agent:
        with env.client.websocket_connect(env.customer_url):
            time.sleep(0.2)
            assert _presence_frames(other_agent) == []


def test_agent_of_other_tenant_cannot_subscribe_to_session(env):
    from starlette.websockets import WebSocketDisconnect

    url = f"/api/v1/ws/agent/{env.sid}?token={env.other_agent_url.split('token=')[1]}"
    with pytest.raises(WebSocketDisconnect) as exc:
        with env.client.websocket_connect(url):
            pass
    assert exc.value.code == 4003


def test_customer_never_receives_presence_frames(env):
    with env.client.websocket_connect(env.customer_url) as customer:
        customer.send_json({"type": "presence", "state": "away"})
        customer.send_json({"type": "ping"})
        assert customer.receive_json()["type"] == "pong"
