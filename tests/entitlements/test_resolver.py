"""get_entitlements: plan defaults, then override_flags, then an active promotional grant."""

import pytest

from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.entitlements import (
    FEATURES,
    FeatureNotInPlan,
    get_entitlements,
    min_tier_for,
    require_feature,
    tenant_has,
)
from app.services.promotional_grant_service import PromotionalGrantService
from tests.entitlements.helpers import (
    AI_ACTIONS,
    ANALYTICS,
    LOOKUP_AND_WEBHOOKS,
    SALES_CLOSE,
    TIERS,
    WHATSAPP,
    caps_of,
    make_tenant,
)


@pytest.mark.parametrize("tier", TIERS)
def test_plan_defaults_match_the_contract_matrix(db_session, tier):
    ent = get_entitlements(make_tenant(db_session, tier), db_session)
    for key in ("sales_close", "crm_notifications", "screen_recording", "ratings_export"):
        assert ent.enabled(key) is SALES_CLOSE[tier], key
    assert ent.enabled("analytics") is ANALYTICS[tier]
    assert ent.enabled("lookup_chains") is LOOKUP_AND_WEBHOOKS[tier]
    assert ent.enabled("webhooks") is LOOKUP_AND_WEBHOOKS[tier]
    assert ent.limit("ai_actions") == AI_ACTIONS[tier]
    assert ent.limit("whatsapp") == WHATSAPP[tier]
    assert ent.enabled("ai_actions") is True
    assert ent.enabled("whatsapp") is True  # on by plan, no grant needed
    assert ent.value("can_use_whatsapp") is True
    assert ent.enabled("voice") is False  # platform grant only, never from a plan


@pytest.mark.parametrize("tier", TIERS)
def test_whatsapp_is_on_by_plan_for_a_tenant_nobody_granted(db_session, tier):
    tenant = make_tenant(db_session, tier)
    assert not (caps_of(db_session, tenant).override_flags or {})
    ent = get_entitlements(tenant, db_session)
    assert ent.enabled("whatsapp") is True
    assert ent.limit("whatsapp") == WHATSAPP[tier]


def test_an_admin_override_off_closes_whatsapp_on_a_plan_that_includes_it(db_session):
    tenant = make_tenant(db_session, "growth", whatsapp_override=False)
    ent = get_entitlements(tenant, db_session)
    assert ent.limit("whatsapp") == 5
    assert ent.value("can_use_whatsapp") is False
    assert ent.enabled("whatsapp") is False


def test_an_admin_override_on_opens_whatsapp_when_the_plan_row_says_no(db_session):
    from app.db.models.subscription_plan import SubscriptionPlan

    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"can_use_whatsapp": False})
    plain = make_tenant(db_session, "lite")
    granted = make_tenant(db_session, "lite", whatsapp_override=True)
    assert get_entitlements(plain, db_session).enabled("whatsapp") is False
    assert get_entitlements(granted, db_session).enabled("whatsapp") is True


def test_a_plan_edit_switches_whatsapp_for_tenants_without_an_override(db_session):
    from app.db.models.subscription_plan import SubscriptionPlan

    tenant = make_tenant(db_session, "lite")
    assert tenant_has(db_session, tenant.id, "whatsapp") is True
    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"can_use_whatsapp": False})
    db_session.expire_all()
    assert tenant_has(db_session, tenant.id, "whatsapp") is False
    assert min_tier_for(db_session, "whatsapp") == "professional"


def test_a_lapsed_account_has_whatsapp_disabled_but_keeps_the_plan_value(db_session):
    """enabled() is the generic lapsed-aware answer; value() is what the services read."""
    tenant = make_tenant(db_session, "professional")
    tenant.subscription_status = "past_due"
    db_session.commit()
    ent = get_entitlements(tenant, db_session)
    assert ent.enabled("whatsapp") is False
    assert ent.value("can_use_whatsapp") is True


def test_whatsapp_is_off_when_the_plan_allows_zero_numbers(db_session):
    from app.db.models.subscription_plan import SubscriptionPlan

    db_session.query(SubscriptionPlan).filter_by(tier="lite").update({"max_whatsapp_numbers": 0})
    tenant = make_tenant(db_session, "lite")
    ent = get_entitlements(tenant, db_session)
    assert ent.value("can_use_whatsapp") is True
    assert ent.enabled("whatsapp") is False


def test_voice_follows_the_grant_only(db_session):
    tenant = make_tenant(db_session, "lite")
    caps_of(db_session, tenant).can_use_voice = True
    db_session.commit()
    assert get_entitlements(tenant, db_session).enabled("voice") is True


def test_stale_copy_without_override_cannot_widen_the_plan(db_session):
    """Drift guard: a hand-edited stored flag with no override entry does not beat the plan."""
    tenant = make_tenant(db_session, "lite")
    caps_of(db_session, tenant).can_use_sales_close = True
    db_session.commit()
    assert get_entitlements(tenant, db_session).enabled("sales_close") is False


def test_override_flag_keeps_the_stored_value(db_session):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.can_use_sales_close = True
    caps.max_whatsapp_numbers = 4
    caps.can_use_whatsapp = True
    caps.override_flags = {"can_use_sales_close": True, "max_whatsapp_numbers": True}
    db_session.commit()
    ent = get_entitlements(tenant, db_session)
    assert ent.enabled("sales_close") is True
    assert ent.limit("whatsapp") == 4
    assert ent.enabled("crm_notifications") is False  # untouched fields still follow the plan


def test_override_can_remove_a_feature_the_plan_has(db_session):
    tenant = make_tenant(db_session, "professional")
    caps = caps_of(db_session, tenant)
    caps.can_use_sales_close = False
    caps.override_flags = {"can_use_sales_close": True}
    db_session.commit()
    assert get_entitlements(tenant, db_session).enabled("sales_close") is False


def test_active_promotional_grant_lifts_the_tier_and_revoke_restores_it(db_session):
    tenant = make_tenant(db_session, "lite")
    assert tenant_has(db_session, tenant.id, "sales_close") is False
    grant = PromotionalGrantService(db_session).create_grant(
        tenant_id=tenant.id,
        campaign_code="ENT-TEST",
        campaign_name="t",
        granted_tier="professional",
        duration_days=30,
        source="admin_single",
        granted_by_admin_id=None,
        granted_by_admin_name="test",
        reason="entitlements resolution test",
    )
    ent = get_entitlements(db_session.get(Tenant, tenant.id), db_session)
    assert ent.tier == "professional"
    assert ent.enabled("sales_close") is True
    assert ent.limit("ai_actions") == 15
    PromotionalGrantService(db_session).revoke_grant(
        grant_id=grant.id, reason="end of test run", admin_id=None, admin_name="test"
    )
    ent = get_entitlements(db_session.get(Tenant, tenant.id), db_session)
    assert ent.tier == "lite"
    assert ent.enabled("sales_close") is False


def test_tenant_without_a_capabilities_row_follows_the_plan(db_session):
    tenant = make_tenant(db_session, "professional", with_caps=False)
    ent = get_entitlements(tenant, db_session)
    assert ent.enabled("sales_close") is True
    assert ent.enabled("whatsapp") is True  # the plan row is enough, no capabilities row needed


def test_unknown_tier_falls_back_to_stored_capabilities(db_session):
    tenant = make_tenant(db_session, "lite")
    db_session.query(Tenant).filter_by(id=tenant.id).update({"subscription_tier": "legacy"})
    caps_of(db_session, tenant).can_use_sales_close = True
    db_session.commit()
    ent = get_entitlements(db_session.get(Tenant, tenant.id), db_session)
    assert ent.enabled("sales_close") is True
    assert ent.enabled("crm_notifications") is False


def test_unknown_tier_and_no_capabilities_means_everything_off(db_session):
    tenant = make_tenant(db_session, "lite", with_caps=False)
    db_session.query(Tenant).filter_by(id=tenant.id).update({"subscription_tier": "legacy"})
    db_session.commit()
    ent = get_entitlements(db_session.get(Tenant, tenant.id), db_session)
    assert not any(ent.enabled(key) for key in FEATURES)


def test_min_tier_is_the_lowest_tier_from_which_the_feature_stays_included(db_session):
    expected = {
        "sales_close": "professional",
        "crm_notifications": "professional",
        "screen_recording": "professional",
        "ratings_export": "professional",
        "lookup_chains": "professional",
        "webhooks": "professional",
        "analytics": "lite",
        "ai_actions": "trial",
        "whatsapp": "trial",
        "voice": None,
    }
    assert {key: min_tier_for(db_session, key) for key in FEATURES} == expected


def test_require_raises_the_contract_body(db_session):
    tenant = make_tenant(db_session, "lite")
    with pytest.raises(FeatureNotInPlan) as caught:
        require_feature(db_session, tenant.id, "sales_close")
    body = caught.value.body()
    assert body["code"] == "feature_not_in_plan"
    assert body["feature"] == "sales_close"
    assert body["min_tier"] == "professional"
    assert isinstance(body["message"], str) and "Professional" in body["message"]
    require_feature(db_session, make_tenant(db_session, "professional").id, "sales_close")


def test_response_shape_matches_the_contract(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_ENABLED", False)
    tenant = make_tenant(db_session, "lite")
    body = get_entitlements(tenant, db_session).to_response()
    assert set(body) == {"tier", "subscription_status", "features"}
    assert body["tier"] == "lite" and body["subscription_status"] == "active"
    features = body["features"]
    assert set(features) == set(FEATURES)
    assert features["sales_close"] == {"enabled": False, "min_tier": "professional"}
    assert features["analytics"] == {"enabled": True, "min_tier": "lite"}
    assert features["voice"] == {"enabled": False, "min_tier": None}
    assert features["ai_actions"] == {"enabled": True, "limit": 2, "used": 0, "min_tier": "trial"}
    assert features["whatsapp"] == {
        "enabled": True,
        "limit": 1,
        "used": 0,
        "min_tier": "trial",
        "platform_available": False,  # the global kill switch, not folded into `enabled`
    }
    assert all("platform_available" not in v for k, v in features.items() if k != "whatsapp")


def test_platform_available_is_the_kill_switch_and_never_changes_enabled(db_session, monkeypatch):
    from app.core.config import settings

    ent = get_entitlements(make_tenant(db_session, "lite"), db_session)
    for switch in (True, False):
        monkeypatch.setattr(settings, "WHATSAPP_ENABLED", switch)
        entry = ent.to_response()["features"]["whatsapp"]
        assert entry["platform_available"] is switch
        assert entry["enabled"] is True


def test_enterprise_limits_are_null_not_zero(db_session):
    body = get_entitlements(make_tenant(db_session, "enterprise"), db_session)
    features = body.to_response()["features"]
    assert features["ai_actions"]["limit"] is None and features["ai_actions"]["enabled"] is True
    assert features["whatsapp"]["limit"] is None and features["whatsapp"]["enabled"] is True


def test_no_second_row_per_tenant(db_session):
    tenant = make_tenant(db_session, "lite")
    assert db_session.query(TenantCapabilities).filter_by(tenant_id=tenant.id).count() == 1


@pytest.mark.parametrize("status", ["trial_expired", "canceled", "past_due"])
def test_lapsed_accounts_have_no_features(db_session, status):
    tenant = make_tenant(db_session, "professional")
    tenant.subscription_status = status
    db_session.commit()
    ent = get_entitlements(tenant, db_session)
    assert ent.tier == "professional" and ent.subscription_status == status
    assert not any(ent.enabled(key) for key in FEATURES)
    assert tenant_has(db_session, tenant.id, "ratings_export") is False


def test_lapsed_trial_cannot_use_the_staff_ratings_routes_or_public_booking(pg_client, db_session):
    from tests.entitlements.helpers import make_user

    tenant = make_tenant(db_session, "trial")
    tenant.subscription_status = "trial_expired"
    db_session.commit()
    _, headers = make_user(db_session, tenant)
    assert pg_client.get("/api/v1/reviews/session", headers=headers).status_code == 403
    assert pg_client.get("/api/v1/reviews/session/export", headers=headers).status_code == 403
    r = pg_client.patch("/api/v1/sales/config", headers=headers, json={"quotation_enabled": True})
    assert r.status_code == 403 and r.json()["code"] == "feature_not_in_plan"


def test_an_ended_grant_stops_counting_before_the_expiry_worker_runs(db_session):
    from datetime import UTC, datetime, timedelta

    from app.db.models.promotional_grant import PromotionalGrant

    tenant = make_tenant(db_session, "lite")
    PromotionalGrantService(db_session).create_grant(
        tenant_id=tenant.id,
        campaign_code="ENT-END",
        campaign_name="t",
        granted_tier="professional",
        duration_days=30,
        source="admin_single",
        granted_by_admin_id=None,
        granted_by_admin_name="test",
        reason="grant window test",
    )
    assert get_entitlements(db_session.get(Tenant, tenant.id), db_session).enabled("sales_close")
    db_session.query(PromotionalGrant).filter_by(tenant_id=tenant.id).update(
        {"grant_ends_at": datetime.now(UTC) - timedelta(hours=1)}
    )
    tenant_row = db_session.get(Tenant, tenant.id)
    tenant_row.subscription_ends_at = datetime.now(UTC) - timedelta(
        hours=1
    )  # as the grant wrote it
    db_session.commit()
    # The tenant row still says professional (the worker has not reverted it).
    ent = get_entitlements(db_session.get(Tenant, tenant.id), db_session)
    assert ent.tier == "lite"
    # The grant wrote its end date onto the tenant, so until the worker reverts it the account
    # reads as lapsed too: nothing stays on from the grant.
    assert ent.enabled("sales_close") is False
    assert not any(ent.enabled(key) for key in FEATURES)


def test_a_trial_past_its_end_date_is_lapsed_before_the_status_flips(db_session):
    from datetime import UTC, datetime, timedelta

    tenant = make_tenant(db_session, "trial")
    tenant.trial_ends_at = datetime.now(UTC) - timedelta(days=1)  # status still "trialing"
    db_session.commit()
    ent = get_entitlements(tenant, db_session)
    assert ent.subscription_status == "trialing"
    assert not any(ent.enabled(key) for key in FEATURES)


def test_an_active_subscription_past_its_end_date_is_lapsed(db_session):
    from datetime import UTC, datetime, timedelta

    tenant = make_tenant(db_session, "professional")
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    assert not get_entitlements(tenant, db_session).enabled("sales_close")
    tenant.subscription_ends_at = datetime.now(UTC) + timedelta(days=5)
    db_session.commit()
    assert get_entitlements(tenant, db_session).enabled("sales_close")
