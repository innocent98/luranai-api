"""customer_presence on the agent-facing REST surfaces (staff-only, batched)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.websocket import connection_manager
from app.services import customer_presence as cp
from tests.presence.conftest import make_escalated, seed_presence


@pytest.fixture(autouse=True)
def _no_side_effects():
    with patch.object(connection_manager, "fanout_event", AsyncMock()):
        yield


OFFLINE = {"state": "offline", "last_seen_at": None}


def test_escalation_list_and_detail(client, db, agent_tenant, auth_headers):
    s_active, t_active = make_escalated(db, agent_tenant)
    s_away, t_away = make_escalated(db, agent_tenant)
    s_off, t_off = make_escalated(db, agent_tenant)
    seed_presence(s_active.id, "active")
    seed_presence(s_away.id, "away")

    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    by_id = {r["escalation_id"]: r for r in rows}
    assert by_id[str(t_active.id)]["customer_presence"] == {"state": "active", "last_seen_at": None}
    assert by_id[str(t_away.id)]["customer_presence"] == {"state": "away", "last_seen_at": None}
    assert by_id[str(t_off.id)]["customer_presence"] == OFFLINE

    detail = client.get(f"/api/v1/agent/escalations/{t_away.id}", headers=auth_headers).json()
    assert detail["customer_presence"] == {"state": "away", "last_seen_at": None}


def test_offline_detail_carries_db_last_seen(client, db, agent_tenant, auth_headers):
    from datetime import UTC, datetime

    s, t = make_escalated(db, agent_tenant)
    s.customer_last_seen_at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    db.commit()
    p = client.get(f"/api/v1/agent/escalations/{t.id}", headers=auth_headers).json()[
        "customer_presence"
    ]
    assert p["state"] == "offline"
    assert p["last_seen_at"].startswith("2026-10-01T12:00:00")


def test_ticket_list_get_and_mutation_responses(client, db, agent_tenant, admin_headers):
    s1, t1 = make_escalated(db, agent_tenant)
    s2, t2 = make_escalated(db, agent_tenant)
    seed_presence(s1.id, "active")

    listing = client.get("/api/v1/tickets", headers=admin_headers).json()["tickets"]
    by_id = {t["id"]: t for t in listing}
    assert by_id[str(t1.id)]["customer_presence"]["state"] == "active"
    assert by_id[str(t2.id)]["customer_presence"] == OFFLINE

    one = client.get(f"/api/v1/tickets/{t1.id}", headers=admin_headers).json()
    assert one["customer_presence"]["state"] == "active"

    updated = client.put(
        f"/api/v1/tickets/{t1.id}", json={"priority": "low"}, headers=admin_headers
    )
    assert updated.status_code == 200
    assert updated.json()["customer_presence"]["state"] == "active"


def test_session_list_and_detail(client, db, agent_tenant, admin_headers):
    s1, _ = make_escalated(db, agent_tenant)
    s2, _ = make_escalated(db, agent_tenant)
    seed_presence(s1.id, "away")

    rows = client.get(
        f"/api/v1/chat/tenants/{agent_tenant.id}/sessions", headers=admin_headers
    ).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(s1.id)]["customer_presence"] == {"state": "away", "last_seen_at": None}
    assert by_id[str(s2.id)]["customer_presence"] == OFFLINE

    detail = client.get(f"/api/v1/chat/sessions/{s1.id}", headers=admin_headers).json()
    assert detail["customer_presence"] == {"state": "away", "last_seen_at": None}


def test_not_on_customer_facing_payloads(client, db, agent_tenant):
    resp = client.post(
        "/api/v1/chat/sessions",
        json={"tenant_id": str(agent_tenant.id), "user_identifier": "visitor:abc"},
    )
    if resp.status_code == 201:
        assert "customer_presence" not in resp.json()
    from app.schemas.conversation import SessionDetailResponse, SessionResponse

    assert "customer_presence" not in SessionResponse.model_fields
    assert "customer_presence" not in SessionDetailResponse.model_fields


def test_other_tenant_cannot_see_presence(client, db, agent_tenant, other_tenant_headers):
    s, t = make_escalated(db, agent_tenant)
    seed_presence(s.id, "active")
    assert (
        client.get(f"/api/v1/agent/escalations/{t.id}", headers=other_tenant_headers).status_code
        == 404
    )
    assert client.get(
        f"/api/v1/chat/sessions/{s.id}", headers=other_tenant_headers
    ).status_code in (
        403,
        404,
    )
    rows = client.get("/api/v1/agent/escalations", headers=other_tenant_headers).json()
    assert rows == []


def test_list_uses_one_redis_pipeline(client, db, agent_tenant, auth_headers, monkeypatch):
    for _ in range(6):
        make_escalated(db, agent_tenant)
    client_ = cp._get_sync_client()
    real = client_.pipeline
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(client_, "pipeline", counting)
    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    assert len(rows) == 6
    assert calls["n"] == 1


def test_redis_outage_degrades_to_null_not_500(client, db, agent_tenant, auth_headers, monkeypatch):
    import redis

    s, t = make_escalated(db, agent_tenant)

    def boom():
        raise redis.ConnectionError("down")

    monkeypatch.setattr(cp, "_get_sync_client", boom)
    resp = client.get(f"/api/v1/agent/escalations/{t.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["customer_presence"] is None


def test_assign_resolve_close_responses_carry_presence(
    client, db, agent_tenant, admin_headers, admin_user
):
    s, t = make_escalated(db, agent_tenant)
    seed_presence(s.id, "away")

    assigned = client.post(
        f"/api/v1/tickets/{t.id}/assign",
        json={"assigned_to": str(admin_user.id)},
        headers=admin_headers,
    )
    assert assigned.status_code == 200
    assert assigned.json()["customer_presence"]["state"] == "away"

    with patch(
        "app.api.v1.endpoints.tickets.auto_request_on_resolve",
        AsyncMock(return_value=(None, "none")),
    ):
        resolved = client.post(f"/api/v1/tickets/{t.id}/resolve", json={}, headers=admin_headers)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["customer_presence"]["state"] == "away"

    closed = client.post(f"/api/v1/tickets/{t.id}/close", headers=admin_headers)
    assert closed.status_code == 200, closed.text
    assert closed.json()["customer_presence"]["state"] == "away"


def test_takeover_reads_presence_off_the_event_loop(
    client, db, agent_tenant, auth_headers, monkeypatch
):
    import asyncio

    from app.api.v1.endpoints import chat as chat_module

    s, t = make_escalated(db, agent_tenant)
    seen = {}
    real = chat_module.read_presence

    def spy(db_, sid):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(db_, sid)

    monkeypatch.setattr(chat_module, "read_presence", spy)
    with patch("app.api.v1.endpoints.chat.enqueue_clear_alert_async", AsyncMock()):
        resp = client.post(f"/api/v1/chat/sessions/{s.id}/takeover", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert seen["on_loop"] is False
    assert resp.json()["customer_presence"]["state"] == "offline"
