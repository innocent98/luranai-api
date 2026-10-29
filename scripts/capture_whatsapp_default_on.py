"""Capture REAL responses for the WhatsApp default-on section of the FE guides.

Writes docs/fe-integration-captures/whatsapp-default-on/. Everything in the guides' new sections is
copied from these files; re-run to re-derive any claim.

Local, throwaway infrastructure only (never staging/prod). Needs, all started by the operator:
  * Postgres migrated to head (`alembic upgrade head`) and Redis; DATABASE_URL/REDIS_URL in the env
  * the fake sidecar:  python -m tests.fakes.fake_sidecar --token T --secret S --max-sessions 3
  * the API with WHATSAPP_ENABLED=true (CAPTURE_API, default :8100) and a second API with
    WHATSAPP_ENABLED=false (CAPTURE_API_OFF, default :8101), both with WHATSAPP_MAX_CONNECTIONS=2,
    PLATFORM_ALERT_EMAILS set, SMTP_* pointing at the local sink (CAPTURE_MAIL_DIR collects mails)

    python scripts/capture_whatsapp_default_on.py

The LLM is never called. Authorization headers and the sidecar session key are replaced by
placeholders in the written files; throwaway passwords are never written.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "whatsapp-default-on"
API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8100")
API_OFF = os.environ.get("CAPTURE_API_OFF", "http://127.0.0.1:8101")
MAIL_DIR = Path(os.environ.get("CAPTURE_MAIL_DIR", "/tmp/wdo-mail"))
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture
PHONES = {"a": "2348000000011", "b": "2348000000012", "c": "2348000000013", "d": "2348000000014"}

OUT.mkdir(parents=True, exist_ok=True)
http = httpx.Client(timeout=30)
_n = {"i": 0}


def record(name, method, base, path, *, token=None, body=None, note=""):
    _n["i"] += 1
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http.request(method, base + path, headers=headers, json=body)
    try:
        resp_body = r.json() if r.content else None
    except ValueError:
        resp_body = r.text
    entry = {
        "scenario": name,
        "note": note,
        "request": {
            "method": method,
            "path": path,
            "headers": {"Authorization": "Bearer <jwt-placeholder>"} if token else {},
            "body": body,
        },
        "response": {"status": r.status_code, "body": resp_body},
    }
    (OUT / f"{_n['i']:02d}-{name}.json").write_text(
        json.dumps(entry, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"{_n['i']:02d} {name}: {r.status_code}")
    return r


def save(name, payload, note=""):
    _n["i"] += 1
    (OUT / f"{_n['i']:02d}-{name}.json").write_text(
        json.dumps({"scenario": name, "note": note, **payload}, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"{_n['i']:02d} {name}")


def db(sql, **params):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        rows = s.execute(text(sql), params)
        if rows.returns_rows:
            return [dict(r._mapping) for r in rows]
        s.commit()
        return []
    finally:
        s.close()


def make_tenant(tag, tier):
    from app.db.models.user import UserRole
    from app.db.session import SessionLocal
    from app.schemas.tenant import TenantCreate
    from app.schemas.user import UserCreate
    from app.services.auth_service import AuthService
    from app.services.subscription_service import SubscriptionService
    from app.services.tenant_service import TenantService

    s = SessionLocal()
    try:
        tenant = TenantService(s).create_tenant(
            TenantCreate(name=f"wdo_{tag}_{uuid.uuid4().hex[:6]}", display_name=f"Acme {tag} Co")
        )
        user = AuthService(s).create_user(
            UserCreate(
                email=f"owner-{tag}@example.com",
                full_name=f"{tag} Owner",
                role=UserRole.TENANT_ADMIN,
                password=PASSWORD,
                tenant_id=tenant.id,
            )
        )
        user.is_active = True
        if hasattr(user, "is_verified"):
            user.is_verified = True
        tenant_id = tenant.id
        s.commit()
    finally:
        s.close()
    if tier != "trial":
        # What a confirmed payment does (same sync code path); Paystack itself is not exercised.
        from sqlalchemy import select

        from app.db.models.tenant import Tenant

        s = SessionLocal()
        try:
            row = s.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
            SubscriptionService(s)._update_capabilities_for_tier(row.id, tier)
            row.subscription_tier = tier
            row.subscription_status = "active"
            s.commit()
        finally:
            s.close()
    return str(tenant_id)


def login(email, path="/api/v1/auth/login", password=None):
    r = http.post(API + path, json={"email": email, "password": password or PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def mail_summary(path):
    import email
    import re
    from email import policy

    msg = email.message_from_string(path.read_text(), policy=policy.default)
    html = msg.get_body(preferencelist=("html",)).get_content()
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return {"to": msg["To"], "subject": msg["Subject"], "body_text": text}


def mails():
    return sorted(MAIL_DIR.glob("*.eml")) if MAIL_DIR.exists() else []


def main():
    from scripts.seed_data import create_platform_admin, create_subscription_plans

    from app.db.session import SessionLocal

    s = SessionLocal()
    create_subscription_plans(s)
    create_platform_admin(s)
    s.close()
    ids = {tag: make_tenant(tag, tier) for tag, tier in (("a", "trial"), ("b", "lite"), ("c", "professional"), ("d", "growth"))}
    ta, tb, tc, td = (login(f"owner-{t}@example.com") for t in "abcd")
    platform = login(
        os.environ["FIRST_SUPERUSER_EMAIL"],
        "/api/v1/platform/auth/login",
        os.environ["FIRST_SUPERUSER_PASSWORD"],
    )
    consent = http.get(
        API + "/api/v1/whatsapp/consent", headers={"Authorization": f"Bearer {ta}"}
    ).json()

    def create_body(phone, label="Main line"):
        return {
            "phone_number": phone,
            "label": label,
            "consent": {"version": consent["version"], "accepted": True},
        }

    # --- 1. plan-driven: nobody was granted anything -----------------------------------------
    stored = db(
        "SELECT t.name, c.can_use_whatsapp AS stored_flag, c.override_flags "
        "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id ORDER BY t.name"
    )
    save("db-state-before-any-admin-action", {"tenant_capabilities": stored},
         "Four new tenants (trial, lite, professional, growth). No admin grant exists; override_flags is empty.")
    record("entitlements-trial-kill-switch-on", "GET", API, "/api/v1/tenant/entitlements", token=ta)
    record("entitlements-lite", "GET", API, "/api/v1/tenant/entitlements", token=tb)
    record("entitlements-professional", "GET", API, "/api/v1/tenant/entitlements", token=tc)
    record("entitlements-growth", "GET", API, "/api/v1/tenant/entitlements", token=td)

    # --- 2. plan-only tenant can create (no grant), plan limits apply ------------------------
    record("create-trial-plan-only-201", "POST", API, "/api/v1/whatsapp/connections", token=ta,
           body=create_body(PHONES["a"]), note="Trial tenant, never granted: allowed by plan.")
    record("create-trial-over-plan-limit-409", "POST", API, "/api/v1/whatsapp/connections", token=ta,
           body=create_body(PHONES["b"]), note="Trial allows 1 number.")
    record("entitlements-trial-after-create", "GET", API, "/api/v1/tenant/entitlements", token=ta)

    # --- 3. capacity: backend limit is 2 live connections; the 2nd create reaches 100% -------
    before = len(mails())
    record("create-lite-201", "POST", API, "/api/v1/whatsapp/connections", token=tb,
           body=create_body(PHONES["c"]))
    for _ in range(40):  # the post-create check is fire-and-forget
        if len(mails()) > before:
            break
        time.sleep(0.25)
    save("operator-alert-mail-after-second-connection",
         {"mails_received_by_local_sink_since_previous_step": len(mails()) - before,
          "last_mail": mail_summary(mails()[-1]) if mails() else None},
         "Local SMTP sink; recipient comes from PLATFORM_ALERT_EMAILS (a placeholder address).")
    record("create-capacity-exceeded-503", "POST", API, "/api/v1/whatsapp/connections", token=tc,
           body=create_body(PHONES["d"]), note="WHATSAPP_MAX_CONNECTIONS=2 and both slots are live.")

    # --- 4. platform admin override: revoke, then restore ------------------------------------
    record("platform-revoke-professional", "PUT", API,
           f"/api/v1/platform/tenants/{ids['c']}/capabilities", token=platform,
           body={"can_use_whatsapp": False},
           note="The dashboard's 'WhatsApp grant' control sending false.")
    record("entitlements-professional-revoked", "GET", API, "/api/v1/tenant/entitlements", token=tc)
    record("create-revoked-403", "POST", API, "/api/v1/whatsapp/connections", token=tc,
           body=create_body(PHONES["d"]))
    save("db-state-after-revoke", {"tenant_capabilities": db(
        "SELECT t.name, c.can_use_whatsapp AS stored_flag, c.override_flags "
        "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id "
        "WHERE t.id = :i", i=ids["c"])})
    record("platform-restore-professional", "PUT", API,
           f"/api/v1/platform/tenants/{ids['c']}/capabilities", token=platform,
           body={"can_use_whatsapp": True})
    record("entitlements-professional-restored", "GET", API, "/api/v1/tenant/entitlements", token=tc)

    # --- 5. kill switch off (second API process, WHATSAPP_ENABLED=false) ---------------------
    record("entitlements-kill-switch-off", "GET", API_OFF, "/api/v1/tenant/entitlements", token=td,
           note="Growth tenant on the API whose WHATSAPP_ENABLED is false: plan says yes, platform says no.")
    record("create-kill-switch-off-403", "POST", API_OFF, "/api/v1/whatsapp/connections", token=td,
           body=create_body(PHONES["d"]))
    record("list-kill-switch-off", "GET", API_OFF, "/api/v1/whatsapp/connections", token=td)


def ops():
    """Cron + dedupe and the migration round trip, against the same local database.

    Run in a process started with WHATSAPP_ENABLED=true and WHATSAPP_CAPACITY_ALERT_PERCENT=25 so
    the fake sidecar's /health (rss 120 of 400 MB = 30%) is above the threshold.
    """
    import asyncio
    import subprocess

    from app.workers.whatsapp import whatsapp_capacity_check

    _n["i"] = 20  # continues after the HTTP captures written by main()

    redis_before = db("SELECT 1 AS ok")
    first = asyncio.run(whatsapp_capacity_check({}))
    second = asyncio.run(whatsapp_capacity_check({}))
    import redis

    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    ttls = {k.decode(): r.ttl(k) for k in r.scan_iter("wa:capacity_alert:*")}
    save("cron-run-twice", {
        "first_run": first, "second_run_inside_6h": second, "redis_key_ttl_seconds": ttls,
        "mails_in_local_sink": len(mails()),
        "last_mail": mail_summary(mails()[-1]) if mails() else None,
    }, "Cron function called twice. The 'connections' condition was already alerted when the 2nd "
       "connection was created, so only sidecar_memory is new on the first run; the second run sends nothing.")
    assert redis_before

    # Migration round trip on real data: old grant-model state -> upgrade backfill.
    state = db("SELECT t.name, t.subscription_tier AS tier FROM tenants t ORDER BY t.name")
    names = {row["name"].split("_")[1]: row["name"] for row in state}
    def set_caps(tag, stored, flags):
        db("UPDATE tenant_capabilities SET can_use_whatsapp = :v, override_flags = CAST(:f AS JSON) "
           "WHERE tenant_id = (SELECT id FROM tenants WHERE name = :n)",
           v=stored, f=json.dumps(flags), n=names[tag])
    set_caps("a", False, {})                           # never granted (old default)
    set_caps("b", True, {"can_use_whatsapp": True, "max_whatsapp_numbers": True})  # hand-granted
    set_caps("c", False, {"can_use_whatsapp": True})   # admin revoked
    db("UPDATE tenant_capabilities SET override_flags = NULL "
       "WHERE tenant_id = (SELECT id FROM tenants WHERE name = :n)", n=names["d"])  # NULL flags
    db("UPDATE tenant_capabilities SET can_use_whatsapp = FALSE "
       "WHERE tenant_id = (SELECT id FROM tenants WHERE name = :n)", n=names["d"])

    q = ("SELECT t.name, t.subscription_tier AS tier, c.can_use_whatsapp AS stored_flag, c.override_flags "
         "FROM tenant_capabilities c JOIN tenants t ON t.id = c.tenant_id ORDER BY t.name")
    steps = {"0_seeded_old_state": db(q)}
    env = {**os.environ}
    def alembic(*args):
        out = subprocess.run(["alembic", *args], capture_output=True, text=True, env=env, cwd=ROOT)
        return [l for l in (out.stdout + out.stderr).splitlines() if "Running" in l or "WARNING" in l or "Error" in l]
    steps["1_alembic_downgrade_-1"] = alembic("downgrade", "-1")
    steps["1_after_downgrade"] = db(q)
    steps["2_alembic_upgrade_head"] = alembic("upgrade", "head")
    steps["2_after_upgrade"] = db(q)
    steps["3_alembic_upgrade_head_again"] = alembic("upgrade", "head")
    steps["3_after_second_upgrade"] = db(q)
    steps["plan_flags"] = db("SELECT tier, can_use_whatsapp, features FROM subscription_plans ORDER BY sort_order")
    save("migration-round-trip", steps,
         "a: never granted, b: hand-granted by an admin, c: admin revoked, d: NULL override_flags. "
         "Tenant names carry the tag (wdo_<tag>_...).")


if __name__ == "__main__":
    ops() if "ops" in sys.argv[1:] else main()
