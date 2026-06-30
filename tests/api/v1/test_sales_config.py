"""Tests for GET/PATCH /api/v1/sales/config."""

import uuid

from app.core.security import get_password_hash


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_tenant_user(db_session, label: str = ""):
    """Create a fresh Tenant + TENANT_ADMIN User; return (user, token)."""
    from app.core.security import create_access_token
    from app.db.models.tenant import Tenant
    from app.db.models.user import User, UserRole

    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()

    user = User(
        email=f"user_{prefix}@example.com",
        hashed_password=get_password_hash("testpassword123"),
        full_name=f"User {prefix}",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)

    token = create_access_token(
        subject=str(user.id),
        additional_claims={
            "role": user.role.value,
            "tenant_id": str(user.tenant_id),
        },
    )
    return user, token


# ---------------------------------------------------------------------------
# GET /api/v1/sales/config
# ---------------------------------------------------------------------------


def test_get_returns_default_when_no_row(pg_client, db_session):
    """GET returns default config (NGN) without persisting a row."""
    from app.db.models.sales_agent_config import TenantSalesAgentConfig
    from sqlalchemy import select

    user, token = _make_tenant_user(db_session)

    r = pg_client.get("/api/v1/sales/config", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["currency"] == "NGN"
    assert data["agent_enabled"] is False
    assert data["lead_capture_enabled"] is True

    # Critical: GET must NOT persist a row
    row = db_session.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    assert row is None, "GET /sales/config must not insert a row"


def test_get_unauthenticated_returns_401(pg_client, db_session):
    """Unauthenticated GET returns 401."""
    r = pg_client.get("/api/v1/sales/config")
    assert r.status_code == 401


def test_get_returns_persisted_row_after_patch(pg_client, db_session):
    """GET after PATCH returns the persisted (non-default) values."""
    user, token = _make_tenant_user(db_session)

    pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True, "currency": "USD"},
        headers=_auth(token),
    )

    r = pg_client.get("/api/v1/sales/config", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agent_enabled"] is True
    assert data["currency"] == "USD"


# ---------------------------------------------------------------------------
# PATCH /api/v1/sales/config
# ---------------------------------------------------------------------------


def test_patch_creates_row_when_none_exists(pg_client, db_session):
    """PATCH on a tenant with no config creates a row and returns persisted values."""
    from app.db.models.sales_agent_config import TenantSalesAgentConfig
    from sqlalchemy import select

    user, token = _make_tenant_user(db_session)

    r = pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agent_enabled"] is True
    assert data["currency"] == "NGN"  # default preserved

    row = db_session.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    assert row is not None, "PATCH must persist a row"
    assert row.agent_enabled is True


def test_patch_updates_existing_row(pg_client, db_session):
    """Second PATCH updates the existing row (upsert, not duplicate)."""
    from app.db.models.sales_agent_config import TenantSalesAgentConfig
    from sqlalchemy import select

    user, token = _make_tenant_user(db_session)

    pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True},
        headers=_auth(token),
    )
    pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": False, "crm_provider": "hubspot"},
        headers=_auth(token),
    )

    rows = db_session.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == user.tenant_id
        )
    ).scalars().all()
    assert len(rows) == 1, "Upsert must not create duplicate rows"
    assert rows[0].agent_enabled is False
    assert rows[0].crm_provider == "hubspot"


def test_patch_pricing_tiers_and_roundtrip(pg_client, db_session):
    """PATCH pricing_tiers persists and GET returns them."""
    user, token = _make_tenant_user(db_session)

    tiers = [{"name": "Growth", "price": 45000, "unit": "mo", "range": "11-50 users"}]
    p = pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True, "pricing_tiers": tiers},
        headers=_auth(token),
    )
    assert p.status_code == 200
    assert p.json()["data"]["agent_enabled"] is True

    g = pg_client.get("/api/v1/sales/config", headers=_auth(token))
    assert g.status_code == 200
    assert g.json()["data"]["pricing_tiers"][0]["price"] == 45000


def test_patch_webhook_availability_discount_roundtrip(pg_client, db_session):
    """Regression: slack_webhook_url, crm_webhook_url, availability, and
    annual_discount_pct must round-trip through PATCH→DB→GET.

    These four model columns (Slices 3/4/5d/5e) were silently dropped by the
    SalesConfig/SalesConfigPatch schemas — the dashboard sent them but they
    never persisted. This locks the schema↔model parity for them.
    """
    from app.db.models.sales_agent_config import TenantSalesAgentConfig
    from sqlalchemy import select

    user, token = _make_tenant_user(db_session)

    avail = {
        "weekdays": [1, 2, 3],
        "start": "10:00",
        "end": "16:00",
        "slot_minutes": 30,
        "buffer_minutes": 15,
        "tenant_timezone": "Africa/Lagos",
    }
    p = pg_client.patch(
        "/api/v1/sales/config",
        json={
            "slack_webhook_url": "https://hooks.slack.com/services/T/B/x",
            "crm_webhook_url": "https://hooks.zapier.com/hooks/catch/1/x",
            "annual_discount_pct": 15,
            "availability": avail,
        },
        headers=_auth(token),
    )
    assert p.status_code == 200

    # Persisted to the DB row.
    db_session.expire_all()
    row = db_session.execute(
        select(TenantSalesAgentConfig).where(
            TenantSalesAgentConfig.tenant_id == user.tenant_id
        )
    ).scalar_one()
    assert row.slack_webhook_url == "https://hooks.slack.com/services/T/B/x"
    assert row.crm_webhook_url == "https://hooks.zapier.com/hooks/catch/1/x"
    assert row.annual_discount_pct == 15
    assert row.availability["start"] == "10:00"

    # Returned by GET (read schema includes them; crm decrypts transparently).
    g = pg_client.get("/api/v1/sales/config", headers=_auth(token))
    data = g.json()["data"]
    assert data["slack_webhook_url"] == "https://hooks.slack.com/services/T/B/x"
    assert data["crm_webhook_url"] == "https://hooks.zapier.com/hooks/catch/1/x"
    assert data["annual_discount_pct"] == 15
    assert data["availability"]["start"] == "10:00"


def test_patch_partial_update_only_touches_provided_fields(pg_client, db_session):
    """PATCH only mutates fields explicitly provided; other defaults remain."""
    user, token = _make_tenant_user(db_session)

    # First PATCH: set two fields
    pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True, "currency": "USD"},
        headers=_auth(token),
    )
    # Second PATCH: only touch one field
    r = pg_client.patch(
        "/api/v1/sales/config",
        json={"quotation_enabled": True},
        headers=_auth(token),
    )
    data = r.json()["data"]
    assert data["agent_enabled"] is True   # preserved from first PATCH
    assert data["currency"] == "USD"       # preserved
    assert data["quotation_enabled"] is True


def test_patch_unauthenticated_returns_401(pg_client, db_session):
    """Unauthenticated PATCH returns 401."""
    r = pg_client.patch("/api/v1/sales/config", json={"agent_enabled": True})
    assert r.status_code == 401


def test_patch_cross_tenant_isolation(pg_client, db_session):
    """Each tenant's config is isolated — PATCH from one does not affect another."""
    user_a, token_a = _make_tenant_user(db_session, "a")
    user_b, token_b = _make_tenant_user(db_session, "b")

    pg_client.patch(
        "/api/v1/sales/config",
        json={"agent_enabled": True, "currency": "USD"},
        headers=_auth(token_a),
    )

    # Tenant B should see their own default (unaffected)
    r_b = pg_client.get("/api/v1/sales/config", headers=_auth(token_b))
    assert r_b.status_code == 200
    data_b = r_b.json()["data"]
    assert data_b["agent_enabled"] is False
    assert data_b["currency"] == "NGN"
