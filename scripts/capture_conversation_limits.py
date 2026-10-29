"""Capture REAL responses for the conversation-limit numbers (cl10conversationlimits).

Writes docs/fe-integration-captures/conversation-limits/. Everything quoted in the FE guide's
"Conversation limits" section and in docs/sop/conversation_limit_numbers.md is copied from these
files; re-run to re-derive any claim.

Local, throwaway infrastructure only (never staging/prod). Needs Postgres migrated to head
(`alembic upgrade head`), Redis, the usual settings in the environment, and the API running against
the same database (CAPTURE_API, default http://127.0.0.1:8583):

    python scripts/capture_conversation_limits.py

The LLM is never called. Authorization headers are replaced by placeholders in the written files;
throwaway passwords are never written.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "fe-integration-captures" / "conversation-limits"
API = os.environ.get("CAPTURE_API", "http://127.0.0.1:8583")
PASSWORD = "Throwaway-Passw0rd!-local"  # local DB only; never written to a capture

OUT.mkdir(parents=True, exist_ok=True)
http = httpx.Client(timeout=30)
_n = {"i": 0}


def record(name, method, path, *, token=None, body=None, note=""):
    _n["i"] += 1
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = http.request(method, API + path, headers=headers, json=body)
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


def stored_limit(tenant_id):
    from sqlalchemy import text

    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        row = s.execute(
            text(
                "SELECT max_conversations_per_month, override_flags "
                "FROM tenant_capabilities WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        ).one()
        return {
            "max_conversations_per_month": row.max_conversations_per_month,
            "override_flags": row.override_flags,
        }
    finally:
        s.close()


def resync_plan(tenant_id, tier, respect_overrides):
    """The tier sync (Paystack itself is not exercised): True is what an upgrade or activation
    runs, False what a downgrade, a promotional grant and its expiry run."""
    import uuid as _uuid

    from app.db.session import SessionLocal
    from app.services.subscription_service import SubscriptionService

    s = SessionLocal()
    try:
        SubscriptionService(s)._update_capabilities_for_tier(
            _uuid.UUID(tenant_id), tier, respect_overrides=respect_overrides
        )
        s.commit()
    finally:
        s.close()


def make_tenant(tag, tier):
    from sqlalchemy import select

    from app.db.models.tenant import Tenant
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
            TenantCreate(name=f"cl_{tag}_{uuid.uuid4().hex[:6]}", display_name=f"Acme {tag} Co")
        )
        user = AuthService(s).create_user(
            UserCreate(
                email=f"owner-{tag}-{uuid.uuid4().hex[:6]}@example.com",
                full_name=f"{tag} Owner",
                role=UserRole.TENANT_ADMIN,
                password=PASSWORD,
                tenant_id=tenant.id,
            )
        )
        user.is_active = True
        if hasattr(user, "is_verified"):
            user.is_verified = True
        tenant_id, email = tenant.id, user.email
        s.commit()
        if tier != "trial":
            # What a confirmed payment does (same sync code path); Paystack is not exercised.
            row = s.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
            SubscriptionService(s)._update_capabilities_for_tier(row.id, tier)
            row.subscription_tier = tier
            row.subscription_status = "active"
            s.commit()
    finally:
        s.close()
    return str(tenant_id), email


def login(email, path="/api/v1/auth/login", password=None):
    r = http.post(API + path, json={"email": email, "password": password or PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def main():
    from scripts.seed_data import create_platform_admin, create_subscription_plans

    from app.db.session import SessionLocal

    s = SessionLocal()
    create_subscription_plans(s)
    create_platform_admin(s)
    s.close()

    platform = login(
        os.environ["FIRST_SUPERUSER_EMAIL"],
        "/api/v1/platform/auth/login",
        os.environ["FIRST_SUPERUSER_PASSWORD"],
    )

    record(
        "public-plans",
        "GET",
        "/api/v1/subscription/plans",
        note="Prices, limits and generated feature strings after cl10 (Enterprise is not listed).",
    )

    growth_id, _ = make_tenant("growth", "growth")
    trial_id, _ = make_tenant("trial", "trial")
    record(
        "platform-tenant-growth-after-sync",
        "GET",
        f"/api/v1/platform/tenants/{growth_id}",
        token=platform,
        note="A Growth tenant synced from the plan: 10,000, no override recorded.",
    )
    record(
        "platform-tenant-trial-after-sync",
        "GET",
        f"/api/v1/platform/tenants/{trial_id}",
        token=platform,
        note="A new Trial tenant: 100.",
    )
    record(
        "platform-hold-growth-tenant-at-25000",
        "PUT",
        f"/api/v1/platform/tenants/{growth_id}/capabilities",
        token=platform,
        body={"max_conversations_per_month": 25000},
        note="How to keep one tenant at its old number: an admin edit, recorded in override_flags.",
    )
    save(
        "db-state-growth-held",
        {"tenant_capabilities": stored_limit(growth_id)},
        "The stored limit and the override record behind the admin edit.",
    )
    resync_plan(growth_id, "growth", respect_overrides=True)
    save(
        "db-state-held-after-upgrade-or-activation-sync",
        {"tenant_capabilities": stored_limit(growth_id)},
        "After the sync an upgrade or activation runs (respect_overrides=True): kept at 25,000.",
    )
    resync_plan(growth_id, "growth", respect_overrides=False)
    save(
        "db-state-held-after-downgrade-or-promo-sync",
        {"tenant_capabilities": stored_limit(growth_id)},
        "After the sync a downgrade, a promotional grant or its expiry runs "
        "(respect_overrides=False): back to the plan's 10,000 and the flag is cleared.",
    )

    for conversations in (10_000, 15_000, 30_000):
        record(
            f"pricing-compare-{conversations}",
            "POST",
            "/api/v1/platform/pricing/enterprise/compare",
            token=platform,
            body={"conversations": conversations},
            note="Growth vs Enterprise sales helper after Growth's limit became 10,000.",
        )


if __name__ == "__main__":
    main()
