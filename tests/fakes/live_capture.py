"""Drive a RUNNING backend + worker + fake sidecar and record real request/response pairs.

Used once to produce docs/fe-integration-captures/whatsapp-channel/ for the FE guide. It needs:
a migrated scratch Postgres (DATABASE_URL), Redis, the API (`tests.fakes.live_stub api`), the
arq worker (`tests.fakes.live_stub worker`), and the fake sidecar (`tests.fakes.fake_sidecar`).
NOT part of the test suite (nothing here is imported by pytest).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8100")
FAKE = os.environ.get("CAPTURE_FAKE", "http://127.0.0.1:8099")
OUT = Path(os.environ.get("CAPTURE_OUT", "docs/fe-integration-captures/whatsapp-channel"))
PHONES = {"a1": "2348000000001", "a2": "2348000000002", "a3": "2348000000003"}

OUT.mkdir(parents=True, exist_ok=True)
client = httpx.Client(timeout=20)
_counter = 0


def record(
    name: str, method: str, url: str, *, token: str | None = None, json_body=None, note: str = ""
):
    """Call the API and save the exact exchange. The bearer token is replaced by a placeholder."""
    global _counter
    _counter += 1
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.request(method, API + url, headers=headers, json=json_body)
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = resp.text
    path = url
    entry = {
        "scenario": name,
        "note": note,
        "request": {
            "method": method,
            "path": path,
            "headers": {"Authorization": "Bearer <admin-jwt-placeholder>"} if token else {},
            "body": json_body,
        },
        "response": {"status": resp.status_code, "body": body},
    }
    (OUT / f"{_counter:02d}-{name}.json").write_text(json.dumps(entry, indent=2) + "\n")
    print(f"{_counter:02d} {name}: {resp.status_code}")
    return resp


def wait_for(predicate, what: str, timeout: float = 25.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.4)
    raise SystemExit(f"timed out waiting for {what}")


def seed():
    """Tenants + users + plans + platform admin via the app's own services (throwaway DB)."""
    from scripts.seed_data import create_platform_admin, create_subscription_plans

    from app.db.models.user import UserRole
    from app.db.session import SessionLocal
    from app.schemas.tenant import TenantCreate
    from app.schemas.user import UserCreate
    from app.services.auth_service import AuthService
    from app.services.tenant_service import TenantService

    db = SessionLocal()
    try:
        create_subscription_plans(db)
        create_platform_admin(db)
        out = {}
        for tag in ("alpha", "beta"):
            tenant = TenantService(db).create_tenant(
                TenantCreate(
                    name=f"cap_{tag}_{uuid4().hex[:6]}", display_name=f"Acme {tag.title()} Test Co"
                )
            )
            AuthService(db).create_user(
                UserCreate(
                    email=f"owner-{tag}@example.com",
                    full_name=f"{tag.title()} Owner",
                    role=UserRole.TENANT_ADMIN,
                    password="Capture-Pass-123!",
                    tenant_id=tenant.id,
                )
            )
            out[tag] = str(tenant.id)
        return out
    finally:
        db.close()


def login(email: str, password: str, path="/api/v1/auth/login") -> str:
    r = client.post(API + path, json={"email": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def db_value(sql: str, **params):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(text(sql), params).first()
        return row[0] if row else None
    finally:
        db.close()


def sent() -> list[dict]:
    return client.get(FAKE + "/_fake/sent").json()


def sent_redacted() -> list[dict]:
    """The sidecar's received sends with the opaque session key replaced by a placeholder."""
    return [{**m, "key": "<session-key-placeholder>"} for m in sent()]


def emit(key: str, body: dict) -> dict:
    r = client.post(f"{FAKE}/_fake/emit/{key}", json=body)
    r.raise_for_status()
    return r.json()


def main() -> None:
    tenants = seed()
    pa = os.environ["FIRST_SUPERUSER_EMAIL"], os.environ["FIRST_SUPERUSER_PASSWORD"]
    platform = login(*pa, path="/api/v1/platform/auth/login")
    alpha = login("owner-alpha@example.com", "Capture-Pass-123!")
    beta = login("owner-beta@example.com", "Capture-Pass-123!")

    # --- consent + gating (alpha not granted yet) ---
    consent = record("consent", "GET", "/api/v1/whatsapp/consent", token=alpha).json()
    body = lambda phone, **kw: {  # noqa: E731
        "phone_number": phone,
        "label": kw.get("label", "Main line"),
        "consent": kw.get("consent", {"version": consent["version"], "accepted": True}),
    }
    record(
        "create-not-enabled",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body(PHONES["a1"]),
        note="Tenant has not been granted WhatsApp yet.",
    )

    # --- platform admin grants it by hand ---
    record(
        "platform-grant-alpha",
        "PUT",
        f"/api/v1/platform/tenants/{tenants['alpha']}/capabilities",
        token=platform,
        json_body={"can_use_whatsapp": True, "max_whatsapp_numbers": 2},
    )
    record(
        "platform-grant-beta",
        "PUT",
        f"/api/v1/platform/tenants/{tenants['beta']}/capabilities",
        token=platform,
        json_body={"can_use_whatsapp": True, "max_whatsapp_numbers": 1},
    )

    # --- validation errors ---
    record(
        "create-consent-missing",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body={"phone_number": PHONES["a1"], "label": "Main line"},
    )
    record(
        "create-consent-stale-version",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body(PHONES["a1"], consent={"version": "1999-old", "accepted": True}),
    )
    record(
        "create-invalid-phone",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body("+2348000000001"),
    )

    # --- happy path: create -> code (once) -> connected via webhook ---
    created = record(
        "create-201",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body(PHONES["a1"]),
    ).json()
    cid = created["id"]
    key1 = db_value("SELECT session_key FROM whatsapp_connections WHERE id = :i", i=cid)
    record("get-awaiting-pair", "GET", f"/api/v1/whatsapp/connections/{cid}", token=alpha)
    record(
        "pairing-code-regenerate",
        "POST",
        f"/api/v1/whatsapp/connections/{cid}/pairing-code",
        token=alpha,
    )
    client.post(f"{FAKE}/_fake/status/{key1}", json={"status": "connecting"})
    client.post(f"{FAKE}/_fake/status/{key1}", json={"status": "connected"})
    wait_for(
        lambda: client.get(
            f"{API}/api/v1/whatsapp/connections/{cid}", headers={"Authorization": f"Bearer {alpha}"}
        )
        .json()
        .get("status")
        == "connected",
        "connected",
    )
    record("get-connected", "GET", f"/api/v1/whatsapp/connections/{cid}", token=alpha)
    record(
        "pairing-code-not-pairable",
        "POST",
        f"/api/v1/whatsapp/connections/{cid}/pairing-code",
        token=alpha,
    )
    record("list", "GET", "/api/v1/whatsapp/connections", token=alpha)

    # --- limits ---
    record(
        "create-number-in-use-other-tenant",
        "POST",
        "/api/v1/whatsapp/connections",
        token=beta,
        json_body=body(PHONES["a1"]),
    )
    second = record(
        "create-second-number",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body(PHONES["a2"], label="Sales"),
    ).json()
    record(
        "create-connection-limit",
        "POST",
        "/api/v1/whatsapp/connections",
        token=alpha,
        json_body=body(PHONES["a3"]),
    )
    record(
        "create-capacity-exceeded",
        "POST",
        "/api/v1/whatsapp/connections",
        token=beta,
        json_body=body("2348000000009"),
        note="Backend cap WHATSAPP_MAX_CONNECTIONS=2 and two sessions are live.",
    )

    # --- isolation ---
    record("cross-tenant-get-404", "GET", f"/api/v1/whatsapp/connections/{cid}", token=beta)
    record(
        "cross-tenant-patch-404",
        "PATCH",
        f"/api/v1/whatsapp/connections/{cid}",
        token=beta,
        json_body={"ai_enabled": False},
    )
    record("cross-tenant-delete-404", "DELETE", f"/api/v1/whatsapp/connections/{cid}", token=beta)
    record("get-unknown-id-404", "GET", f"/api/v1/whatsapp/connections/{uuid4()}", token=alpha)

    # New tenants start as first_responder="human" (N3), which never lets the AI answer. A tenant
    # that wants the AI on WhatsApp sets this first (real endpoint, captured for the FE guide).
    record(
        "tenant-ai-config-first-responder-ai",
        "PUT",
        "/api/v1/tenant/ai-config",
        token=alpha,
        json_body={"first_responder": "ai", "grounding_requirement": False},
        note="Required before the AI answers WhatsApp customers: new tenants default to human-only.",
    )

    # --- a customer writes in: AI answers (LLM STUBBED) via the real webhook -> arq -> sidecar ---
    emit(
        key1,
        {
            "type": "message.inbound",
            "message_id": "FAKEIN0001",
            "from_ref": "198765432100@lid",
            "from_phone_digits": None,
            "push_name": "Ada",
            "kind": "text",
            "text": "What are your opening hours?",
        },
    )
    wait_for(lambda: len(sent()) >= 1, "AI reply sent")
    (OUT / "sidecar-sent-after-ai.json").write_text(
        json.dumps({"fake_sidecar_received": sent_redacted()}, indent=2) + "\n"
    )
    sid = wait_for(
        lambda: db_value("SELECT session_id FROM whatsapp_chats LIMIT 1"), "chat session"
    )
    record(
        "staff-session-detail-after-ai",
        "GET",
        f"/api/v1/chat/sessions/{sid}",
        token=alpha,
        note="What an agent/admin sees: source and per-message meta.channel.",
    )

    # --- non-text ---
    emit(
        key1,
        {
            "type": "message.inbound",
            "message_id": "FAKEIN0002",
            "from_ref": "198765432100@lid",
            "from_phone_digits": None,
            "push_name": "Ada",
            "kind": "other",
            "text": "",
        },
    )
    wait_for(lambda: len(sent()) >= 2, "fixed non-text reply")

    # --- owner types from the phone: pause ---
    emit(
        key1,
        {
            "type": "message.outbound_external",
            "message_id": "FAKEOUT0001",
            "to_ref": "198765432100@lid",
            "text": "Hi Ada, it's the owner. I'll take it from here.",
        },
    )
    wait_for(lambda: db_value("SELECT human_until FROM whatsapp_chats LIMIT 1"), "pause set")
    before = len(sent())
    emit(
        key1,
        {
            "type": "message.inbound",
            "message_id": "FAKEIN0003",
            "from_ref": "198765432100@lid",
            "from_phone_digits": None,
            "push_name": "Ada",
            "kind": "text",
            "text": "Great, can you send the price list?",
        },
    )
    wait_for(
        lambda: db_value(
            "SELECT count(*) FROM whatsapp_inbound_messages WHERE provider_message_id = 'FAKEIN0003'"
        ),
        "paused inbound processed",
    )
    time.sleep(1.5)
    assert len(sent()) == before, "AI must stay silent while the owner pause is active"
    record(
        "staff-session-detail-after-owner-pause",
        "GET",
        f"/api/v1/chat/sessions/{sid}",
        token=alpha,
        note="HUMAN_AGENT message with meta.source=owner_phone; the paused customer message is stored; no AI reply.",
    )

    # --- agent replies from the dashboard -> WhatsApp ---
    agent_resp = record(
        "agent-message",
        "POST",
        f"/api/v1/chat/sessions/{sid}/agent-message",
        token=alpha,
        json_body={"message": "Hi Ada, our price list is on its way."},
    )
    wait_for(lambda: len(sent()) == before + 1, "agent message forwarded")
    (OUT / "sidecar-sent-after-agent.json").write_text(
        json.dumps({"fake_sidecar_received": sent_redacted()}, indent=2) + "\n"
    )
    assert agent_resp.status_code in (200, 201)

    # --- AI off ---
    record(
        "patch-ai-off",
        "PATCH",
        f"/api/v1/whatsapp/connections/{cid}",
        token=alpha,
        json_body={"ai_enabled": False, "label": "Support line"},
    )

    # --- unlinked from the phone: status flips, no auto re-pair ---
    client.post(f"{FAKE}/_fake/status/{key1}", json={"status": "logged_out", "code": 401})
    wait_for(
        lambda: client.get(
            f"{API}/api/v1/whatsapp/connections/{cid}", headers={"Authorization": f"Bearer {alpha}"}
        )
        .json()
        .get("status")
        == "logged_out",
        "logged_out",
    )
    record("get-logged-out", "GET", f"/api/v1/whatsapp/connections/{cid}", token=alpha)
    record(
        "pairing-code-after-logout",
        "POST",
        f"/api/v1/whatsapp/connections/{cid}/pairing-code",
        token=alpha,
    )
    record("get-awaiting-pair-again", "GET", f"/api/v1/whatsapp/connections/{cid}", token=alpha)

    # --- delete ---
    record("delete-204", "DELETE", f"/api/v1/whatsapp/connections/{second['id']}", token=alpha)
    record(
        "get-after-delete-404", "GET", f"/api/v1/whatsapp/connections/{second['id']}", token=alpha
    )

    # --- webhook edge cases (for the sidecar track; not an FE surface) ---
    import hmac as _h
    import hashlib as _hl

    secret = os.environ["WHATSAPP_WEBHOOK_SECRET"]
    raw = json.dumps(
        {"id": "x", "at": "x", "session_key": key1, "type": "session.status", "status": "connected"}
    ).encode()
    ts = int(time.time())
    good = f"t={ts},v1={_h.new(secret.encode(), f'{ts}.'.encode() + raw, _hl.sha256).hexdigest()}"
    for name, header in [
        ("webhook-valid", good),
        ("webhook-bad-signature", f"t={ts},v1={'0' * 64}"),
        ("webhook-missing-header", None),
    ]:
        h = {
            "Content-Type": "application/json",
            **({"X-Luran-Signature": header} if header else {}),
        }
        r = client.post(API + "/api/v1/webhooks/whatsapp/linked", content=raw, headers=h)
        (OUT / f"zz-{name}.json").write_text(
            json.dumps(
                {
                    "scenario": name,
                    "request": {
                        "method": "POST",
                        "path": "/api/v1/webhooks/whatsapp/linked",
                        "headers": {
                            "X-Luran-Signature": (
                                "<redacted-signature-placeholder>" if header else None
                            )
                        },
                        "body": {**json.loads(raw), "session_key": "<session-key-placeholder>"},
                    },
                    "response": {"status": r.status_code, "body": r.json()},
                },
                indent=2,
            )
            + "\n"
        )
        print(name, r.status_code)


if __name__ == "__main__":
    sys.exit(main())
