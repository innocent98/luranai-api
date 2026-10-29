"""GET /api/v1/tenant/entitlements: contract body, any tenant user, own tenant only."""

from app.core.config import settings
from app.db.models.user import UserRole
from app.services.promotional_grant_service import PromotionalGrantService
from tests.entitlements.helpers import caps_of, make_tenant, make_user

URL = "/api/v1/tenant/entitlements"


def test_requires_authentication(pg_client):
    assert pg_client.get(URL).status_code in (401, 403)


def test_lite_admin_gets_the_contract_body(pg_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    r = pg_client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tier": "lite",
        "subscription_status": "active",
        "features": {
            "sales_close": {"enabled": False, "min_tier": "professional"},
            "crm_notifications": {"enabled": False, "min_tier": "professional"},
            "screen_recording": {"enabled": False, "min_tier": "professional"},
            "ratings_export": {"enabled": False, "min_tier": "professional"},
            "lookup_chains": {"enabled": False, "min_tier": "professional"},
            "webhooks": {"enabled": False, "min_tier": "professional"},
            "analytics": {"enabled": True, "min_tier": "lite"},
            "ai_actions": {"enabled": True, "limit": 2, "used": 0, "min_tier": "trial"},
            "whatsapp": {
                "enabled": True,
                "limit": 1,
                "used": 0,
                "min_tier": "trial",
                "platform_available": False,
            },
            "voice": {"enabled": False, "min_tier": None},
        },
    }


def test_whatsapp_entry_reports_the_kill_switch_without_changing_enabled(
    pg_client, db_session, monkeypatch
):
    tenant = make_tenant(db_session, "professional")
    _, headers = make_user(db_session, tenant)
    for switch in (True, False):
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", switch)
        features = pg_client.get(URL, headers=headers).json()["features"]
        assert features["whatsapp"] == {
            "enabled": True,
            "limit": 2,
            "used": 0,
            "min_tier": "trial",
            "platform_available": switch,
        }
        assert all(
            "platform_available" not in entry
            for name, entry in features.items()
            if name != "whatsapp"
        )


def test_whatsapp_entry_for_an_admin_override_off(pg_client, db_session):
    tenant = make_tenant(db_session, "professional", whatsapp_override=False)
    _, headers = make_user(db_session, tenant)
    entry = pg_client.get(URL, headers=headers).json()["features"]["whatsapp"]
    assert entry["enabled"] is False and entry["limit"] == 2 and entry["min_tier"] == "trial"


def test_any_tenant_role_can_read_it(pg_client, db_session):
    tenant = make_tenant(db_session, "professional")
    for role in (UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT):
        _, headers = make_user(db_session, tenant, role)
        r = pg_client.get(URL, headers=headers)
        assert r.status_code == 200, (role, r.text)
        assert r.json()["features"]["sales_close"]["enabled"] is True


def test_each_tenant_sees_only_its_own_plan(pg_client, db_session):
    lite, enterprise = make_tenant(db_session, "lite"), make_tenant(db_session, "enterprise")
    _, lite_headers = make_user(db_session, lite)
    _, ent_headers = make_user(db_session, enterprise)
    assert pg_client.get(URL, headers=lite_headers).json()["tier"] == "lite"
    assert pg_client.get(URL, headers=ent_headers).json()["tier"] == "enterprise"
    # There is no way to ask for another tenant's entitlements.
    spoof = pg_client.get(URL, headers=lite_headers, params={"tenant_id": str(enterprise.id)})
    assert spoof.json()["tier"] == "lite"
    assert spoof.json()["features"]["ai_actions"]["limit"] == 2


def test_override_is_reflected(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.can_use_sales_close = True
    caps.override_flags = {"can_use_sales_close": True}
    db_session.commit()
    _, headers = make_user(db_session, tenant)
    features = pg_client.get(URL, headers=headers).json()["features"]
    assert features["sales_close"] == {"enabled": True, "min_tier": "professional"}
    assert features["crm_notifications"]["enabled"] is False


def test_promotional_grant_is_reflected(pg_client, db_session):
    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    PromotionalGrantService(db_session).create_grant(
        tenant_id=tenant.id,
        campaign_code="ENT-EP",
        campaign_name="t",
        granted_tier="growth",
        duration_days=30,
        source="admin_single",
        granted_by_admin_id=None,
        granted_by_admin_name="test",
        reason="entitlements endpoint test",
    )
    body = pg_client.get(URL, headers=headers).json()
    assert body["tier"] == "growth"
    assert body["features"]["ai_actions"]["limit"] == 40
    assert body["features"]["sales_close"]["enabled"] is True


def test_usage_counts_reflect_the_tenants_rows(pg_client, db_session):
    from uuid import uuid4

    from app.db.models.automation import TenantAction

    tenant = make_tenant(db_session, "lite")
    _, headers = make_user(db_session, tenant)
    db_session.add(
        TenantAction(
            tenant_id=tenant.id,
            name="A",
            slug=f"a_{uuid4().hex[:6]}",
            description="d",
            method="GET",
            url_template="https://api.example.com/x",
            input_schema={},
        )
    )
    db_session.commit()
    assert pg_client.get(URL, headers=headers).json()["features"]["ai_actions"]["used"] == 1


def test_still_readable_when_the_trial_has_expired(pg_client, db_session):
    """The dashboard needs it to render the upgrade state: no 402 here."""
    tenant = make_tenant(db_session, "trial")
    tenant.subscription_status = "trial_expired"
    db_session.commit()
    _, headers = make_user(db_session, tenant)
    r = pg_client.get(URL, headers=headers)
    assert r.status_code == 200 and r.json()["subscription_status"] == "trial_expired"
    # A lapsed account keeps its tier but none of the features (no free use of routes that
    # have no 402 check of their own).
    assert not any(f["enabled"] for f in r.json()["features"].values())
