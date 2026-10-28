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
    ent = get_entitlements(make_tenant(db_session, tier, whatsapp_grant=True), db_session)
    for key in ("sales_close", "crm_notifications", "screen_recording", "ratings_export"):
        assert ent.enabled(key) is SALES_CLOSE[tier], key
    assert ent.enabled("analytics") is ANALYTICS[tier]
    assert ent.enabled("lookup_chains") is LOOKUP_AND_WEBHOOKS[tier]
    assert ent.enabled("webhooks") is LOOKUP_AND_WEBHOOKS[tier]
    assert ent.limit("ai_actions") == AI_ACTIONS[tier]
    assert ent.limit("whatsapp") == WHATSAPP[tier]
    assert ent.enabled("ai_actions") is True
    assert ent.enabled("whatsapp") is True  # grant held, plan allows at least one number
    assert ent.enabled("voice") is False  # platform grant only, never from a plan


def test_whatsapp_needs_the_platform_grant_even_when_the_plan_allows_numbers(db_session):
    ent = get_entitlements(make_tenant(db_session, "growth", whatsapp_grant=False), db_session)
    assert ent.limit("whatsapp") == 5
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
    assert ent.enabled("whatsapp") is False  # no grant row


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


def test_response_shape_matches_the_contract(db_session):
    tenant = make_tenant(db_session, "lite", whatsapp_grant=True)
    body = get_entitlements(tenant, db_session).to_response()
    assert set(body) == {"tier", "subscription_status", "features"}
    assert body["tier"] == "lite" and body["subscription_status"] == "active"
    features = body["features"]
    assert set(features) == set(FEATURES)
    assert features["sales_close"] == {"enabled": False, "min_tier": "professional"}
    assert features["analytics"] == {"enabled": True, "min_tier": "lite"}
    assert features["voice"] == {"enabled": False, "min_tier": None}
    assert features["ai_actions"] == {"enabled": True, "limit": 2, "used": 0, "min_tier": "trial"}
    assert features["whatsapp"] == {"enabled": True, "limit": 1, "used": 0, "min_tier": "trial"}


def test_enterprise_limits_are_null_not_zero(db_session):
    body = get_entitlements(make_tenant(db_session, "enterprise", whatsapp_grant=True), db_session)
    features = body.to_response()["features"]
    assert features["ai_actions"]["limit"] is None and features["ai_actions"]["enabled"] is True
    assert features["whatsapp"]["limit"] is None and features["whatsapp"]["enabled"] is True


def test_no_second_row_per_tenant(db_session):
    tenant = make_tenant(db_session, "lite")
    assert db_session.query(TenantCapabilities).filter_by(tenant_id=tenant.id).count() == 1
