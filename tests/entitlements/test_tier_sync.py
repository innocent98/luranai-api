"""Every writer of tenant_capabilities carries the new gates (one apply_plan_to_capabilities)."""

from uuid import uuid4

import pytest

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities
from app.schemas.tenant import TenantCapabilitiesUpdate
from app.services.plan_service import PlanService
from app.services.platform_subscription_service import PlatformSubscriptionService
from app.services.promotional_grant_service import PromotionalGrantService
from app.services.subscription_service import SubscriptionService
from app.services.tenant_service import TenantService
from tests.entitlements.helpers import caps_of, make_tenant

GATES = (
    "can_use_sales_close",
    "can_use_crm_notifications",
    "can_use_screen_recording",
    "can_export_ratings",
)


def _gates(db, tenant):
    caps = caps_of(db, tenant)
    return tuple(getattr(caps, g) for g in GATES), caps.max_whatsapp_numbers


def test_new_tenants_start_from_the_trial_plan(db_session):
    from app.schemas.tenant import TenantCreate

    created = TenantService(db_session).create_tenant(
        TenantCreate(name=f"trialco_{uuid4().hex[:8]}", display_name="Trial Co")
    )
    caps = db_session.query(TenantCapabilities).filter_by(tenant_id=created.id).one()
    assert tuple(getattr(caps, g) for g in GATES) == (True, True, True, True)
    assert caps.max_whatsapp_numbers == 1
    assert caps.can_use_whatsapp is False  # a grant, never from the plan


def test_upgrade_and_downgrade_move_the_gates(db_session):
    tenant = make_tenant(db_session, "lite")
    assert _gates(db_session, tenant) == ((False, False, False, False), 1)
    SubscriptionService(db_session).upgrade_subscription(tenant.id, "professional")
    assert _gates(db_session, tenant) == ((True, True, True, True), 2)
    SubscriptionService(db_session)._update_capabilities_for_tier(tenant.id, "lite")
    assert _gates(db_session, tenant) == ((False, False, False, False), 1)


def test_upgrade_to_enterprise_stores_unlimited_as_null(db_session):
    tenant = make_tenant(db_session, "growth")
    SubscriptionService(db_session).upgrade_subscription(tenant.id, "enterprise")
    caps = caps_of(db_session, tenant)
    assert caps.max_whatsapp_numbers is None and caps.max_knowledge_mb is None
    assert caps.max_automation_actions is None


def test_upgrade_keeps_a_manual_override_and_downgrade_clears_it(db_session):
    tenant = make_tenant(db_session, "lite")
    TenantService(db_session).update_capabilities(
        tenant.id, TenantCapabilitiesUpdate(can_use_sales_close=True)
    )
    assert caps_of(db_session, tenant).override_flags == {"can_use_sales_close": True}
    SubscriptionService(db_session).upgrade_subscription(tenant.id, "professional")
    assert caps_of(db_session, tenant).can_use_sales_close is True
    SubscriptionService(db_session)._update_capabilities_for_tier(tenant.id, "lite")
    caps = caps_of(db_session, tenant)
    assert caps.can_use_sales_close is False and "can_use_sales_close" not in (
        caps.override_flags or {}
    )


def test_platform_override_and_promo_grant_apply_the_gates(db_session):
    tenant = make_tenant(db_session, "lite")
    PlatformSubscriptionService(db_session)._update_capabilities_with_overrides(
        tenant.id, "growth", respect_overrides=False
    )
    assert _gates(db_session, tenant) == ((True, True, True, True), 5)
    PlatformSubscriptionService(db_session)._update_capabilities_with_overrides(
        tenant.id, "lite", respect_overrides=False
    )
    grant = PromotionalGrantService(db_session).create_grant(
        tenant_id=tenant.id,
        campaign_code="ENT-SYNC",
        campaign_name="t",
        granted_tier="professional",
        duration_days=30,
        source="admin_single",
        granted_by_admin_id=None,
        granted_by_admin_name="test",
        reason="tier sync carries the gates",
    )
    assert _gates(db_session, tenant) == ((True, True, True, True), 2)
    PromotionalGrantService(db_session).revoke_grant(
        grant_id=grant.id, reason="back to the paid tier", admin_id=None, admin_name="test"
    )
    assert _gates(db_session, tenant) == ((False, False, False, False), 1)


def test_platform_admin_capability_edit_is_an_override(db_session):
    tenant = make_tenant(db_session, "lite")
    TenantService(db_session).update_capabilities(
        tenant.id, TenantCapabilitiesUpdate(can_export_ratings=True, max_whatsapp_numbers=None)
    )
    caps = caps_of(db_session, tenant)
    assert caps.can_export_ratings is True and caps.max_whatsapp_numbers is None
    assert set(caps.override_flags) == {"can_export_ratings", "max_whatsapp_numbers"}


def test_plan_editor_accepts_the_new_capabilities_and_rejects_junk(db_session):
    service = PlanService(db_session)
    plan = service.update_plan(
        "lite", capabilities={"can_use_sales_close": True, "max_whatsapp_numbers": 3}
    )
    assert plan.can_use_sales_close is True and plan.max_whatsapp_numbers == 3
    service.update_plan("lite", capabilities={"max_whatsapp_numbers": None})
    assert (
        db_session.query(SubscriptionPlan).filter_by(tier="lite").one().max_whatsapp_numbers is None
    )
    with pytest.raises(ValueError, match="Unknown plan capability"):
        service.update_plan("lite", capabilities={"can_do_anything": True})
    with pytest.raises(ValueError, match="true or false"):
        service.update_plan("lite", capabilities={"can_export_ratings": "yes"})
    with pytest.raises(ValueError, match="cannot be negative"):
        service.update_plan("lite", capabilities={"max_audits_per_month": -1})
    with pytest.raises(ValueError, match="must be positive"):
        service.update_plan("lite", capabilities={"max_response_tokens": 0})


def test_a_plan_edit_reaches_tenants_through_the_resolver(db_session):
    from app.services.entitlements import get_entitlements

    tenant = make_tenant(db_session, "lite")
    assert get_entitlements(tenant, db_session).enabled("ratings_export") is False
    PlanService(db_session).update_plan("lite", capabilities={"can_export_ratings": True})
    assert get_entitlements(tenant, db_session).enabled("ratings_export") is True
