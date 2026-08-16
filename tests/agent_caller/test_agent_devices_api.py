"""API tests for agent identity, device registration and availability.

Note: `agent_user`/`admin_user` are loaded via the shared `db` session, and the
`client` fixture's dependency override closes that same session after every
request (see tests/conftest.py). That detaches any ORM objects already loaded
in it, so — matching the existing pattern elsewhere in this suite (e.g.
tests/api/v1/test_conversation_attribution.py) — tests capture ids *before*
issuing a request and re-query fresh rows afterward instead of touching the
original (now-detached) fixture objects.
"""

import uuid

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.user import AvailabilityStatus, User

# Realistic-length stand-ins for FCM registration tokens (~140+ chars in production).
# Short literals like "tok-1" would pass a lenient schema but must never be used here —
# they'd mask the min_length floor the boundary test below is pinning.
TOKEN_1 = f"tok-{uuid.uuid4().hex}"
TOKEN_SHARED = f"tok-shared-{uuid.uuid4().hex}"
TOKEN_NEVER_SEEN = f"tok-never-seen-{uuid.uuid4().hex}"


def test_me_returns_identity_and_tenant_name(client, auth_headers, agent_user, agent_tenant):
    resp = client.get("/api/v1/agent/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(agent_user.id)
    assert body["tenant_name"] == "Acme Insurance"
    assert body["role"] == "support_agent"
    assert body["availability_status"] == "available"


def test_me_requires_auth(client):
    assert client.get("/api/v1/agent/me").status_code == 401


def test_viewer_is_rejected(client, db, viewer_user):
    from app.core.security import create_access_token

    token = create_access_token(
        subject=str(viewer_user.id),
        additional_claims={
            "role": viewer_user.role.value,
            "tenant_id": str(viewer_user.tenant_id),
        },
    )
    resp = client.get("/api/v1/agent/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_register_device_creates_a_row(client, db, auth_headers, agent_user):
    agent_user_id, agent_tenant_id = agent_user.id, agent_user.tenant_id
    resp = client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": TOKEN_1, "platform": "android"},
    )
    assert resp.status_code == 204
    device = db.query(AgentDevice).filter(AgentDevice.user_id == agent_user_id).one()
    assert device.device_token == TOKEN_1
    assert device.platform is DevicePlatform.ANDROID
    assert device.tenant_id == agent_tenant_id


def test_register_device_is_an_upsert(client, db, auth_headers, agent_user):
    agent_user_id = agent_user.id
    for _ in range(2):
        resp = client.post(
            "/api/v1/agent/devices",
            headers=auth_headers,
            json={"device_token": TOKEN_1, "platform": "android"},
        )
        assert resp.status_code == 204
    assert db.query(AgentDevice).filter(AgentDevice.user_id == agent_user_id).count() == 1


def test_register_device_updates_last_seen(client, db, auth_headers, agent_user):
    agent_user_id = agent_user.id
    client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": TOKEN_1, "platform": "android"},
    )
    device = db.query(AgentDevice).filter(AgentDevice.user_id == agent_user_id).one()
    assert device.last_seen_at is not None


def test_unregister_device_deletes_only_own_row(client, db, auth_headers, agent_user, admin_user):
    agent_user_id, admin_user_id = agent_user.id, admin_user.id
    db.add(
        AgentDevice(
            user_id=admin_user_id,
            tenant_id=admin_user.tenant_id,
            device_token=TOKEN_SHARED,
            platform=DevicePlatform.IOS,
        )
    )
    db.commit()
    client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": TOKEN_SHARED, "platform": "android"},
    )

    resp = client.request(
        "DELETE",
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": TOKEN_SHARED},
    )
    assert resp.status_code == 204
    assert db.query(AgentDevice).filter(AgentDevice.user_id == agent_user_id).count() == 0
    assert db.query(AgentDevice).filter(AgentDevice.user_id == admin_user_id).count() == 1


def test_unregister_unknown_token_is_still_204(client, auth_headers):
    resp = client.request(
        "DELETE",
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": TOKEN_NEVER_SEEN},
    )
    assert resp.status_code == 204


def test_register_device_rejects_too_short_token(client, auth_headers):
    resp = client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "short12", "platform": "android"},
    )
    assert resp.status_code == 422


def test_set_availability(client, db, auth_headers, agent_user):
    agent_user_id = agent_user.id
    resp = client.put("/api/v1/agent/availability", headers=auth_headers, json={"status": "away"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "away"}
    refreshed = db.query(User).filter(User.id == agent_user_id).one()
    assert refreshed.availability_status is AvailabilityStatus.AWAY


def test_invalid_availability_is_rejected(client, auth_headers):
    resp = client.put("/api/v1/agent/availability", headers=auth_headers, json={"status": "lunch"})
    assert resp.status_code == 422
