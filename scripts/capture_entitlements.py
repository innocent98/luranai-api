"""Capture REAL responses for docs/fe-integration-guide-entitlements.md from a running local app.

Everything in the guide is copied from the files this writes to
docs/fe-integration-captures/entitlements/. Re-run it to re-derive any claim.

Prerequisites (local, throwaway infrastructure only; never point this at staging/prod):
  * Postgres migrated to head and plans seeded (scripts/seed_data.py)
  * API on --base (default http://127.0.0.1:8583), env pointing at the same database

    poetry run python scripts/capture_entitlements.py --base http://127.0.0.1:8583

It registers throwaway tenants with @example.com addresses, activates them directly in the local
database, moves them between tiers there (the Paystack round-trip is not exercised) and sets a
throwaway password. Authorization headers are replaced with placeholders in the written files.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "entitlements"

PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture
_counter = {"n": 0}


class Capturer:
    def __init__(self, api: str):
        self.api = api
        self.http = httpx.Client(timeout=60)

    def call(
        self,
        scenario: str,
        note: str,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body=None,
        capture: bool = True,
    ) -> httpx.Response:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.http.request(method, self.api + path, headers=headers, json=json_body)
        if capture:
            _counter["n"] += 1
            try:
                body = r.json()
            except ValueError:
                body = r.text
            record = {
                "scenario": scenario,
                "note": note,
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {"Authorization": "Bearer <jwt-placeholder>"} if token else {},
                    "body": json_body,
                },
                "response": {
                    "status": r.status_code,
                    "content_type": r.headers.get("content-type"),
                    "body": body,
                },
            }
            OUT.mkdir(parents=True, exist_ok=True)
            name = f"{_counter['n']:02d}-{scenario}.json"
            (OUT / name).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            print(f"  {name}: {method} {path.split('?')[0]} -> {r.status_code}")
        return r


def _session():
    from app.db.session import SessionLocal

    return SessionLocal()


def db_activate(email: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.core.security import get_password_hash
    from app.db.models.user import User

    db = _session()
    try:
        user = next(u for u in db.execute(select(User)).scalars() if u.email == email)
        user.is_active = True
        if hasattr(user, "is_verified"):
            user.is_verified = True
        user.hashed_password = get_password_hash(PASSWORD)
        db.commit()
        return user.tenant_id
    finally:
        db.close()


def db_set_tier(tenant_id: str, tier: str, status: str = "active") -> None:
    """Move a tenant to a tier the way a confirmed payment does (same sync code path)."""
    from sqlalchemy import select

    from app.db.models.tenant import Tenant
    from app.services.subscription_service import SubscriptionService

    db = _session()
    try:
        tenant = db.execute(select(Tenant).where(Tenant.id == uuid.UUID(tenant_id))).scalar_one()
        SubscriptionService(db)._update_capabilities_for_tier(tenant.id, tier)
        tenant.subscription_tier = tier
        tenant.subscription_status = status
        db.commit()
    finally:
        db.close()


def db_override(tenant_id: str, **fields) -> None:
    """What a platform admin capability edit does: set the value and record the override."""
    from sqlalchemy import select

    from app.db.models.tenant import TenantCapabilities

    db = _session()
    try:
        caps = db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == uuid.UUID(tenant_id))
        ).scalar_one()
        for key, value in fields.items():
            setattr(caps, key, value)
        caps.override_flags = {**(caps.override_flags or {}), **dict.fromkeys(fields, True)}
        db.commit()
    finally:
        db.close()


def db_seed_booking(tenant_id: str) -> str:
    """Leave meeting booking switched ON in the stored config plus a lead, as a downgrade or a
    promotion expiry would. Returns the conversation id the public picker uses."""
    from sqlalchemy import select

    from app.db.models.lead import Lead
    from app.db.models.sales_agent_config import TenantSalesAgentConfig

    db = _session()
    try:
        cfg = db.execute(
            select(TenantSalesAgentConfig).where(
                TenantSalesAgentConfig.tenant_id == uuid.UUID(tenant_id)
            )
        ).scalar_one()
        cfg.meeting_booking_enabled = True
        cfg.availability = {
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "start": "00:00",
            "end": "23:30",
            "slot_minutes": 30,
            "buffer_minutes": 0,
            "tenant_timezone": "UTC",
        }
        conv = uuid.uuid4()
        db.add(Lead(tenant_id=uuid.UUID(tenant_id), conversation_id=conv, email="visitor@example.com"))
        db.commit()
        return str(conv)
    finally:
        db.close()


def db_add_agent(tenant_id: str, label: str) -> str:
    from app.core.security import get_password_hash
    from app.db.models.user import User, UserRole

    email = f"ent-agent-{label}-{uuid.uuid4().hex[:6]}@example.com"
    db = _session()
    try:
        db.add(
            User(
                email=email,
                hashed_password=get_password_hash(PASSWORD),
                full_name="Support Person",
                role=UserRole.SUPPORT_AGENT,
                tenant_id=uuid.UUID(tenant_id),
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()
    return email


def clear_local_rate_limits() -> None:
    """Local throwaway Redis only: registration is capped at 3/hour per IP."""
    import redis

    from app.core.config import settings

    client = redis.Redis.from_url(settings.REDIS_URL)
    for key in client.scan_iter(match="http_*", count=500):
        client.delete(key)
    client.close()


def register(cap: Capturer, label: str):
    clear_local_rate_limits()
    email = f"ent-{label}-{uuid.uuid4().hex[:6]}@example.com"
    r = cap.call(
        "register",
        "setup",
        "POST",
        "/auth/register-company",
        json_body={
            "company_name": f"Demo Bakery {label} {uuid.uuid4().hex[:6]}",
            "admin_full_name": "Demo Owner",
            "admin_email": email,
        },
        capture=False,
    )
    assert r.status_code == 201, r.text
    return str(db_activate(email)), email


def login(cap: Capturer, email: str) -> str:
    r = cap.call(
        "login", "setup", "POST", "/auth/login", json_body={"email": email, "password": PASSWORD},
        capture=False,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8583")
    args = ap.parse_args()
    cap = Capturer(args.base + "/api/v1")

    print("== public plans")
    cap.call("public-plans", "Prices and generated feature strings (Enterprise is not listed).",
             "GET", "/subscription/plans")

    print("== tenant A: trial -> lite")
    a_id, a_email = register(cap, "a")
    a = login(cap, a_email)
    cap.call("entitlements-unauthenticated", "No token.", "GET", "/tenant/entitlements")
    cap.call("entitlements-trial", "A brand-new trial tenant: everything on except lookup chains, "
             "webhooks and analytics. WhatsApp shows enabled=false: the platform grant is not set.",
             "GET", "/tenant/entitlements", token=a)
    cap.call("sales-config-trial-enable-quotes-and-booking",
             "Trial includes the sales features, so the save succeeds.", "PATCH", "/sales/config",
             token=a, json_body={"agent_enabled": True, "meeting_booking_enabled": True,
                                 "quotation_enabled": True})
    cap.call("sales-config-trial-enable-slack",
             "Trial includes Slack and CRM notifications.", "PATCH", "/sales/config", token=a,
             json_body={"slack_webhook_url": "https://hooks.slack.com/services/T000/B000/placeholder",
                        "notify_channels": {"email": True, "slack": True, "in_app": False, "crm": False}})

    # Switch every gated option off again, then downgrade to Lite the way the sync does.
    cap.call("sales-config-turn-gated-options-off",
             "Switch the gated options off (a downgrade is blocked while they are on).",
             "PATCH", "/sales/config", token=a,
             json_body={"meeting_booking_enabled": False, "quotation_enabled": False,
                        "notify_channels": {"email": True, "slack": False, "in_app": False, "crm": False}})
    db_set_tier(a_id, "lite")
    cap.call("entitlements-lite", "Same tenant after the move to Lite.", "GET",
             "/tenant/entitlements", token=a)
    cap.call("sales-config-lite-enable-booking", "Gate error for a gated save.", "PATCH",
             "/sales/config", token=a, json_body={"meeting_booking_enabled": True})
    cap.call("sales-config-lite-enable-quotes", "Same gate, other option.", "PATCH",
             "/sales/config", token=a, json_body={"quotation_enabled": True})
    cap.call("sales-config-lite-enable-slack-url", "Slack URL is the crm_notifications gate.",
             "PATCH", "/sales/config", token=a,
             json_body={"slack_webhook_url": "https://hooks.slack.com/services/T111/B111/other-placeholder"})
    cap.call("sales-config-lite-enable-crm-channel", "Turning the CRM channel on is the same gate.",
             "PATCH", "/sales/config", token=a,
             json_body={"notify_channels": {"email": True, "crm": True}})
    cap.call("sales-config-lite-ungated-save", "Everything not gated still saves on Lite.",
             "PATCH", "/sales/config", token=a,
             json_body={"notification_email": "sales@example.com", "currency": "NGN"})
    cap.call("ratings-list-lite-admin", "Admin ratings breakdown on Lite.", "GET",
             "/reviews/session", token=a)
    cap.call("ratings-export-lite-admin", "CSV export on Lite.", "GET",
             "/reviews/session/export", token=a)
    cap.call("analytics-agents-lite", "Analytics stays open on Lite; the ratings columns are "
             "zero/null (empty list here because the tenant has no agents with ratings).",
             "GET", "/analytics/agents", token=a)
    cap.call("widget-config-lite-admin", "Dashboard widget settings: can_use_screen_recording is "
             "the plan decision.", "GET", "/widget/config", token=a)
    conv = db_seed_booking(a_id)
    cap.call("public-meeting-availability-lite",
             "Runtime path, public widget endpoint: booking is still switched on in the stored config "
             "but the plan lacks it. The visitor gets an empty picker, never a plan error.",
             "GET", f"/chat/meetings/availability?conversation_id={conv}&days=2")
    cap.call("public-meeting-book-lite",
             "Same tenant: booking is refused exactly like a tenant that switched it off (BOOKING_DISABLED).",
             "POST", "/chat/meetings",
             json_body={"conversation_id": conv, "scheduled_at": "2030-01-07T10:00:00Z",
                        "visitor_timezone": "UTC"})
    agent_email = db_add_agent(a_id, "a")
    agent_token = login(cap, agent_email)
    cap.call("entitlements-lite-support-agent", "Any tenant role can read it.", "GET",
             "/tenant/entitlements", token=agent_token)
    cap.call("ratings-list-lite-agent-self-view", "A support agent still sees their own ratings "
             "on Lite.", "GET", "/reviews/session", token=agent_token)

    print("== tenant B: professional")
    b_id, b_email = register(cap, "b")
    db_set_tier(b_id, "professional")
    b = login(cap, b_email)
    cap.call("entitlements-professional", "Professional tenant.", "GET", "/tenant/entitlements",
             token=b)
    cap.call("sales-config-professional-enable-all", "All gated options save.", "PATCH",
             "/sales/config", token=b,
             json_body={"meeting_booking_enabled": True, "quotation_enabled": True,
                        "notify_channels": {"email": True, "slack": True, "crm": True}})
    cap.call("ratings-list-professional-admin", "Empty list, 200.", "GET", "/reviews/session", token=b)
    cap.call("ratings-export-professional-admin", "CSV header row only (no ratings yet).", "GET",
             "/reviews/session/export", token=b)
    cap.call("downgrade-blocked-by-gated-options",
             "Downgrade to Lite while booking/quotes/Slack/CRM are on: 400 with violations. "
             "(Nothing is sent to Paystack: validation runs first.)", "POST",
             "/subscription/downgrade", token=b, json_body={"tier": "lite"})

    print("== tenant C: lite with a platform override, then trial expired")
    c_id, c_email = register(cap, "c")
    db_set_tier(c_id, "lite")
    db_override(c_id, can_use_sales_close=True)
    c = login(cap, c_email)
    cap.call("entitlements-lite-with-override",
             "Lite plus a manual platform override of one feature: enabled=true, min_tier unchanged.",
             "GET", "/tenant/entitlements", token=c)
    cap.call("sales-config-lite-override-enable-quotes", "The override unlocks the gated save.",
             "PATCH", "/sales/config", token=c, json_body={"quotation_enabled": True})

    d_id, d_email = register(cap, "d")
    d = login(cap, d_email)
    db_set_tier(d_id, "trial", status="trial_expired")
    cap.call("entitlements-trial-expired",
             "Readable after the trial expires (no 402): the dashboard needs it for the upgrade view.",
             "GET", "/tenant/entitlements", token=d)

    print("done:", _counter["n"], "captures in", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
