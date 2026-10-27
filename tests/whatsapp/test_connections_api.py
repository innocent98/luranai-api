"""Dashboard API (contract B): gating, consent, capacity, isolation, pairing-code handling."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from sqlalchemy import select, text

from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection
from app.schemas.tenant import TenantCapabilitiesUpdate
from app.services.whatsapp_service import CONSENT_VERSION
from tests.whatsapp.conftest import (
    PHONE_A,
    PHONE_B,
    PHONE_C,
    consent_body,
    login_admin,
    make_tenant,
)

CODE_RE = re.compile(r"^FAKE\d{4}$")


def _create(client, body=None):
    return client.post("/api/v1/whatsapp/connections", json=body or consent_body())


def test_consent_is_versioned_and_marked_draft(client, db, wa_settings):
    login_admin(make_tenant(db))
    r = client.get("/api/v1/whatsapp/consent")
    assert r.status_code == 200
    assert r.json()["version"] == CONSENT_VERSION
    assert "DRAFT" in r.json()["text"]


def test_create_returns_pairing_code_once_and_never_stores_or_logs_it(
    client, db, wa_settings, fake_sidecar, log_lines, caplog
):
    tenant = make_tenant(db)
    login_admin(tenant)
    r = _create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "status", "pairing_code", "expires_in_s"}
    assert body["status"] == "awaiting_pair" and body["expires_in_s"] == 180
    assert CODE_RE.match(body["pairing_code"])

    code = body["pairing_code"]
    # never persisted: scan every column of every WhatsApp table
    db.expire_all()
    for table in ("whatsapp_connections", "whatsapp_chats", "whatsapp_inbound_messages"):
        for row in db.execute(text(f"SELECT * FROM {table}")).mappings():  # noqa: S608
            assert code not in " ".join(str(v) for v in row.values())
    # never logged (loguru + stdlib)
    assert code not in "".join(log_lines)
    assert code not in caplog.text
    # not readable back either
    got = client.get(f"/api/v1/whatsapp/connections/{body['id']}")
    assert code not in got.text


def test_phone_and_session_key_are_not_plaintext_lookup_keys(client, db, wa_settings, fake_sidecar):
    tenant = make_tenant(db)
    login_admin(tenant)
    _create(client)
    raw = (
        db.execute(text("SELECT phone_e164, phone_hmac FROM whatsapp_connections")).mappings().one()
    )
    assert PHONE_A not in raw["phone_e164"]  # Fernet ciphertext, not plaintext
    import hashlib

    assert raw["phone_hmac"] != hashlib.sha256(PHONE_A.encode()).hexdigest()  # peppered, not bare


def test_list_get_patch_shape(client, db, wa_settings, fake_sidecar):
    login_admin(make_tenant(db))
    cid = _create(client).json()["id"]
    one = client.get(f"/api/v1/whatsapp/connections/{cid}").json()
    assert set(one) == {
        "id",
        "label",
        "status",
        "phone_masked",
        "ai_enabled",
        "last_connected_at",
        "last_disconnect_reason",
    }
    assert one["status"] == "awaiting_pair" and one["ai_enabled"] is True
    assert PHONE_A not in one["phone_masked"] and one["phone_masked"].endswith(PHONE_A[-4:])
    assert client.get("/api/v1/whatsapp/connections").json() == [one]
    patched = client.patch(
        f"/api/v1/whatsapp/connections/{cid}", json={"ai_enabled": False, "label": "Sales"}
    )
    assert patched.status_code == 200
    assert patched.json()["ai_enabled"] is False and patched.json()["label"] == "Sales"
    assert (
        client.patch(
            f"/api/v1/whatsapp/connections/{cid}", json={"status": "connected"}
        ).status_code
        == 422
    )


def test_gating_global_switch_and_capability(client, db, wa_settings, fake_sidecar, monkeypatch):
    login_admin(make_tenant(db, can_use=False))
    r = _create(client)
    assert (r.status_code, r.json()["code"]) == (403, "whatsapp_not_enabled")
    login_admin(make_tenant(db))
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    r = _create(client)
    assert (r.status_code, r.json()["code"]) == (403, "whatsapp_not_enabled")


@pytest.mark.parametrize(
    "consent",
    [
        None,
        {"version": CONSENT_VERSION, "accepted": False},
        {"version": "1999-old", "accepted": True},
        {"accepted": True},
    ],
)
def test_consent_version_enforced(client, db, wa_settings, fake_sidecar, consent):
    login_admin(make_tenant(db))
    body = consent_body()
    if consent is None:
        body.pop("consent")
    else:
        body["consent"] = consent
    r = _create(client, body)
    assert (r.status_code, r.json()["code"]) == (422, "consent_required")
    assert db.execute(select(WhatsAppConnection)).first() is None
    assert fake_sidecar.sessions == {}  # sidecar never called


@pytest.mark.parametrize(
    "phone", ["+2348000000001", "0801234567", "12345", "abc", "23480000000011111"]
)
def test_invalid_phone(client, db, wa_settings, fake_sidecar, phone):
    login_admin(make_tenant(db))
    r = _create(client, consent_body(phone))
    assert (r.status_code, r.json()["code"]) == (422, "invalid_phone")


def test_number_in_use_across_tenants_and_connection_limit(client, db, wa_settings, fake_sidecar):
    a, b = make_tenant(db, max_numbers=1), make_tenant(db, max_numbers=2)
    login_admin(a)
    assert _create(client, consent_body(PHONE_A)).status_code == 201
    r = _create(client, consent_body(PHONE_B))
    assert (r.status_code, r.json()["code"]) == (409, "connection_limit")
    login_admin(b)
    r = _create(client, consent_body(PHONE_A))
    assert (r.status_code, r.json()["code"]) == (409, "number_in_use")
    assert PHONE_A not in r.text


def test_backend_capacity_guard_blocks_before_calling_sidecar(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db, max_numbers=3))
    assert _create(client, consent_body(PHONE_A)).status_code == 201
    calls_before = list(fake_sidecar.calls)
    r = _create(client, consent_body(PHONE_B))
    assert (r.status_code, r.json()["code"]) == (503, "capacity_exceeded")
    assert fake_sidecar.calls == calls_before  # guard ran before any sidecar call
    assert len(db.execute(select(WhatsAppConnection)).all()) == 1


def test_sidecar_503_capacity_maps_through_and_leaves_no_row(client, db, wa_settings, fake_sidecar):
    fake_sidecar.max_sessions = 0
    login_admin(make_tenant(db))
    r = _create(client)
    assert (r.status_code, r.json()["code"]) == (503, "capacity_exceeded")
    assert db.execute(select(WhatsAppConnection)).first() is None


def test_sidecar_unreachable_is_502_and_leaves_no_row(client, db, wa_settings, monkeypatch):
    monkeypatch.setattr(wa_settings, "WHATSAPP_SIDECAR_URL", "")
    login_admin(make_tenant(db))
    r = _create(client)
    assert (r.status_code, r.json()["code"]) == (502, "sidecar_unavailable")
    assert db.execute(select(WhatsAppConnection)).first() is None


def test_cross_tenant_is_404_never_403(client, db, wa_settings, fake_sidecar):
    a, b = make_tenant(db), make_tenant(db)
    login_admin(a)
    cid = _create(client).json()["id"]
    login_admin(b)
    base = f"/api/v1/whatsapp/connections/{cid}"
    for method, url, kw in [
        ("get", base, {}),
        ("patch", base, {"json": {"ai_enabled": False}}),
        ("delete", base, {}),
        ("post", base + "/pairing-code", {}),
    ]:
        r = getattr(client, method)(url, **kw)
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), (method, r.text)
    assert client.get("/api/v1/whatsapp/connections").json() == []
    # still intact for the owner
    login_admin(a)
    assert client.get(base).status_code == 200


def test_pairing_code_regenerate_rules(client, db, wa_settings, fake_sidecar):
    login_admin(make_tenant(db))
    cid = _create(client).json()["id"]
    url = f"/api/v1/whatsapp/connections/{cid}/pairing-code"
    r = client.post(url)
    assert r.status_code == 200 and CODE_RE.match(r.json()["pairing_code"])
    assert r.json()["expires_in_s"] == 180
    conn_id = UUID(cid)

    def set_status(value):  # the client fixture detaches ORM objects: always re-fetch
        db.get(WhatsAppConnection, conn_id).status = value
        db.commit()

    set_status("connected")
    r = client.post(url)
    assert (r.status_code, r.json()["code"]) == (409, "not_pairable")
    set_status("logged_out")
    assert client.post(url).status_code == 200
    assert db.get(WhatsAppConnection, conn_id).status == "awaiting_pair"


def test_delete_logs_out_wipes_and_keeps_conversations(client, db, wa_settings, fake_sidecar):
    tenant = make_tenant(db)
    login_admin(tenant)
    cid = _create(client).json()["id"]
    conn = db.get(WhatsAppConnection, UUID(cid))
    session = ConversationSession(
        tenant_id=tenant.id, user_identifier="wa_x", status=SessionStatus.ACTIVE, source="whatsapp"
    )
    db.add(session)
    db.flush()
    session_id = session.id
    db.add(
        WhatsAppChat(
            tenant_id=tenant.id,
            connection_id=conn.id,
            contact_hmac="h" * 64,
            contact_ref="ref",
            session_id=session_id,
        )
    )
    db.commit()
    key, conn_id = conn.session_key, conn.id
    r = client.delete(f"/api/v1/whatsapp/connections/{cid}")
    assert r.status_code == 204 and r.content == b""
    assert key not in fake_sidecar.sessions  # logged out at the sidecar
    db.expire_all()
    assert db.get(WhatsAppConnection, conn_id) is None
    assert db.execute(select(WhatsAppChat)).first() is None  # contact refs wiped
    assert db.get(ConversationSession, session_id) is not None  # conversations kept
    assert client.delete(f"/api/v1/whatsapp/connections/{cid}").status_code == 404


def test_delete_keeps_row_when_sidecar_unreachable(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    login_admin(make_tenant(db))
    cid = _create(client).json()["id"]
    monkeypatch.setattr(wa_settings, "WHATSAPP_SIDECAR_URL", "")
    r = client.delete(f"/api/v1/whatsapp/connections/{cid}")
    assert (r.status_code, r.json()["code"]) == (502, "sidecar_unavailable")
    assert db.execute(select(WhatsAppConnection)).first() is not None


def test_sidecar_requires_bearer_token(client, db, wa_settings, fake_sidecar, monkeypatch):
    monkeypatch.setattr(wa_settings, "WHATSAPP_SIDECAR_TOKEN", "wrong")
    login_admin(make_tenant(db))
    r = _create(client)  # fake answers 401 -> adapter treats as unavailable
    assert r.status_code == 502


def test_capabilities_schema_accepts_whatsapp_fields_and_still_forbids_unknown(db):
    update = TenantCapabilitiesUpdate(can_use_whatsapp=True, max_whatsapp_numbers=2)
    assert update.model_dump(exclude_unset=True) == {
        "can_use_whatsapp": True,
        "max_whatsapp_numbers": 2,
    }
    with pytest.raises(ValueError):
        TenantCapabilitiesUpdate(can_use_whatsapp=True, bogus=1)
    with pytest.raises(ValueError):
        TenantCapabilitiesUpdate(max_whatsapp_numbers=-1)


def test_platform_capabilities_service_persists_grant(db):
    from app.services.tenant_service import TenantService

    tenant = make_tenant(db, can_use=False, max_numbers=0)
    resp = TenantService(db).update_capabilities(
        tenant.id, TenantCapabilitiesUpdate(can_use_whatsapp=True, max_whatsapp_numbers=1)
    )
    assert resp.can_use_whatsapp is True and resp.max_whatsapp_numbers == 1


def test_adapter_registry_cloud_api_slot_is_a_stub():
    from app.channels.registry import get_adapter

    assert get_adapter("cloud_api").provider == "cloud_api"
    with pytest.raises(NotImplementedError):
        getattr(get_adapter("cloud_api"), "send")  # noqa: B009
    with pytest.raises(ValueError):
        get_adapter("nope")


def test_revoked_grant_cannot_mint_new_pairing_codes(client, db, wa_settings, fake_sidecar):
    from app.db.models.tenant import TenantCapabilities

    tenant = make_tenant(db)
    login_admin(tenant)
    cid = _create(client).json()["id"]
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one()
    caps.can_use_whatsapp = False
    db.commit()
    r = client.post(f"/api/v1/whatsapp/connections/{cid}/pairing-code")
    assert (r.status_code, r.json()["code"]) == (403, "whatsapp_not_enabled")


def test_repairing_a_logged_out_connection_respects_the_capacity_guard(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db, max_numbers=3))
    first = _create(client, consent_body(PHONE_A)).json()["id"]
    db.get(WhatsAppConnection, UUID(first)).status = "logged_out"  # frees its slot
    db.commit()
    assert _create(client, consent_body(PHONE_B)).status_code == 201  # takes the only slot
    r = client.post(f"/api/v1/whatsapp/connections/{first}/pairing-code")
    assert (r.status_code, r.json()["code"]) == (503, "capacity_exceeded")


class _Counter:
    """In-memory stand-in for `enforce_identifier_limit` (the real one needs one event loop)."""

    def __init__(self, limits):
        self.limits, self.hits = limits, {}

    async def __call__(self, limiter, identifier):
        from fastapi import HTTPException

        key = (limiter.key_prefix, identifier)
        self.hits[key] = self.hits.get(key, 0) + 1
        if self.hits[key] > self.limits.get(limiter.key_prefix, 10**6):
            raise HTTPException(status_code=429, detail={"error": "Rate limit exceeded"})


def test_create_is_rate_limited_per_tenant_and_per_target_number(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    from app.services import whatsapp_service as svc

    counter = _Counter({"http_whatsapp_phone": 1, "http_whatsapp_create": 3})
    monkeypatch.setattr(svc, "enforce_identifier_limit", counter)
    login_admin(make_tenant(db, max_numbers=5))
    first = _create(client, consent_body(PHONE_A))
    assert first.status_code == 201
    client.delete(f"/api/v1/whatsapp/connections/{first.json()['id']}")
    # same target number again: a tenant cannot make WhatsApp ping one number repeatedly
    assert _create(client, consent_body(PHONE_A)).status_code == 429
    assert _create(client, consent_body(PHONE_B)).status_code == 201
    assert _create(client, consent_body(PHONE_C)).status_code == 429  # 4th create for the tenant
    # the phone limiter is keyed by the peppered hash, never the raw number
    assert not any(PHONE_A in ident for (_, ident) in counter.hits)
    assert any(prefix == "http_whatsapp_create" for (prefix, _) in counter.hits)


def test_pairing_code_regeneration_is_rate_limited_per_connection(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    from app.services import whatsapp_service as svc

    counter = _Counter({"http_whatsapp_pairing_conn": 2})
    monkeypatch.setattr(svc, "enforce_identifier_limit", counter)
    login_admin(make_tenant(db))
    cid = _create(client).json()["id"]
    url = f"/api/v1/whatsapp/connections/{cid}/pairing-code"
    assert [client.post(url).status_code for _ in range(3)] == [200, 200, 429]


def test_unpaired_attempts_expire_and_free_the_number_and_slot(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    from datetime import timedelta

    from app.utils.datetime import utcnow_aware

    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db, max_numbers=1))
    cid = _create(client, consent_body(PHONE_A)).json()["id"]
    # inside the window the number and the only slot are held
    login_admin(make_tenant(db, max_numbers=1))
    assert _create(client, consent_body(PHONE_A)).json()["code"] == "number_in_use"
    row = db.get(WhatsAppConnection, UUID(cid))
    row.created_at = utcnow_aware() - timedelta(minutes=45)
    db.commit()
    r = _create(client, consent_body(PHONE_A))  # another tenant can now take it
    assert r.status_code == 201, r.text
    assert db.get(WhatsAppConnection, UUID(cid)) is None
    assert len(fake_sidecar.sessions) == 1  # the stale sidecar session was logged out


def test_probing_an_in_use_number_does_not_spend_its_per_number_budget(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    from app.services import whatsapp_service as svc

    counter = _Counter({"http_whatsapp_phone": 1})
    monkeypatch.setattr(svc, "enforce_identifier_limit", counter)
    login_admin(make_tenant(db, max_numbers=3))
    assert _create(client, consent_body(PHONE_A)).status_code == 201
    login_admin(make_tenant(db, max_numbers=3))
    for _ in range(3):
        assert _create(client, consent_body(PHONE_A)).json()["code"] == "number_in_use"
    assert (
        counter.hits[
            (
                "http_whatsapp_phone",
                next(i for (p, i) in counter.hits if p == "http_whatsapp_phone"),
            )
        ]
        == 1
    )


def test_sidecar_client_refuses_to_run_without_a_token(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_SIDECAR_TOKEN", "")
    login_admin(make_tenant(db))
    assert _create(client).json()["code"] == "sidecar_unavailable"


def test_new_codes_do_not_extend_an_unpaired_attempt_and_spend_the_number_budget(
    client, db, wa_settings, fake_sidecar, monkeypatch
):
    from datetime import timedelta

    from app.services import whatsapp_service as svc
    from app.utils.datetime import utcnow_aware

    counter = _Counter({})
    monkeypatch.setattr(svc, "enforce_identifier_limit", counter)
    monkeypatch.setattr(wa_settings, "WHATSAPP_MAX_CONNECTIONS", 1)
    login_admin(make_tenant(db))
    cid = _create(client, consent_body(PHONE_A)).json()["id"]
    assert client.post(f"/api/v1/whatsapp/connections/{cid}/pairing-code").status_code == 200
    assert (
        counter.hits[
            (
                "http_whatsapp_phone",
                next(i for (p, i) in counter.hits if p == "http_whatsapp_phone"),
            )
        ]
        == 2
    )
    db.get(WhatsAppConnection, UUID(cid)).created_at = utcnow_aware() - timedelta(minutes=45)
    db.commit()
    client.post(f"/api/v1/whatsapp/connections/{cid}/pairing-code")  # refreshing does not save it
    login_admin(make_tenant(db))
    assert _create(client, consent_body(PHONE_A)).status_code == 201  # squatter's hold is gone
