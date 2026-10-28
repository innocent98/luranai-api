"""ent10 data steps: plan rows, tenant backfill, Growth override tenant, idempotency, reversibility."""

import importlib.util
import os
import uuid

import sqlalchemy as sa

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities
from tests.entitlements.helpers import make_tenant

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "ent10_plan_entitlements.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("ent10_plan_entitlements", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _caps(db, tenant):
    db.expire_all()
    return db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()


def _pre_migration_state(db, tenant, **caps):
    """Rows as they were before the migration: gates absent (off), recording on for everyone."""
    row = _caps(db, tenant)
    row.can_use_sales_close = False
    row.can_use_crm_notifications = False
    row.can_export_ratings = False
    row.can_use_screen_recording = True
    row.max_whatsapp_numbers = 0
    for key, value in caps.items():
        setattr(row, key, value)
    db.commit()


def test_backfill_gives_each_tenant_its_tier_values(db_session):
    mod = _load()
    tenants = {t: make_tenant(db_session, t) for t in ("trial", "lite", "professional", "growth")}
    for tenant in tenants.values():
        _pre_migration_state(db_session, tenant)
    mod.backfill_tenants(db_session.connection())
    lite, pro = _caps(db_session, tenants["lite"]), _caps(db_session, tenants["professional"])
    assert (lite.can_use_sales_close, lite.can_use_screen_recording) == (False, False)
    assert (lite.can_use_crm_notifications, lite.can_export_ratings) == (False, False)
    assert lite.max_whatsapp_numbers == 1
    assert (pro.can_use_sales_close, pro.can_use_screen_recording, pro.can_export_ratings) == (
        True,
        True,
        True,
    )
    assert pro.max_whatsapp_numbers == 2
    assert _caps(db_session, tenants["trial"]).can_use_sales_close is True
    assert _caps(db_session, tenants["growth"]).max_whatsapp_numbers == 5


def test_growth_override_tenant_ends_up_with_everything_on(db_session):
    """The single Growth override tenant (manual admin override) keeps every capability."""
    mod = _load()
    tenant = make_tenant(db_session, "growth")
    _pre_migration_state(
        db_session, tenant, can_use_webhooks=True, override_flags={"can_use_webhooks": True}
    )
    mod.backfill_tenants(db_session.connection())
    caps = _caps(db_session, tenant)
    assert caps.can_use_sales_close and caps.can_use_crm_notifications
    assert caps.can_use_screen_recording and caps.can_export_ratings
    assert caps.can_use_webhooks and caps.can_use_lookup_chains and caps.can_access_analytics
    assert caps.max_whatsapp_numbers == 5


def test_backfill_respects_overrides_and_never_lowers_a_granted_number(db_session):
    mod = _load()
    kept = make_tenant(db_session, "lite")
    _pre_migration_state(
        db_session,
        kept,
        can_use_screen_recording=True,
        override_flags={"can_use_screen_recording": True},
    )
    granted = make_tenant(db_session, "professional")
    _pre_migration_state(db_session, granted, max_whatsapp_numbers=4, can_use_whatsapp=True)
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, kept).can_use_screen_recording is True
    assert _caps(db_session, granted).max_whatsapp_numbers == 4


def test_lite_ai_action_limit_drops_to_two_only_for_lite(db_session):
    mod = _load()
    lite, pro = make_tenant(db_session, "lite"), make_tenant(db_session, "professional")
    _pre_migration_state(db_session, lite, max_automation_actions=5)
    _pre_migration_state(db_session, pro, max_automation_actions=99)
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, lite).max_automation_actions == 2
    assert _caps(db_session, pro).max_automation_actions == 99


def test_backfill_is_idempotent(db_session):
    mod = _load()
    _pre_migration_state(db_session, make_tenant(db_session, "lite"))
    first = mod.backfill_tenants(db_session.connection())
    assert first >= 1
    assert mod.backfill_tenants(db_session.connection()) == 0


def test_plan_data_step_is_idempotent_and_reversible(db_session):
    mod = _load()
    conn = db_session.connection()

    def lite():
        db_session.expire_all()
        return db_session.query(SubscriptionPlan).filter_by(tier="lite").one()

    original = (lite().monthly_price_kobo, list(lite().features), lite().max_automation_actions)
    mod.apply_plan_data(conn, reverse=True)
    assert (lite().monthly_price_kobo, lite().annual_price_kobo) == (900_000, 9_000_000)
    assert lite().max_automation_actions == 5
    assert "5 automation actions" in lite().features
    mod.apply_plan_data(conn)
    mod.apply_plan_data(conn)  # twice: converges
    assert (lite().monthly_price_kobo, list(lite().features), lite().max_automation_actions) == original


def test_hand_edited_feature_strings_are_left_alone(db_session, capsys):
    mod = _load()
    plan = db_session.query(SubscriptionPlan).filter_by(tier="professional").one()
    edited = ["Our own marketing line"]
    db_session.execute(
        sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = 'professional'"),
        {"f": '["Our own marketing line"]'},
    )
    mod.apply_plan_data(db_session.connection())
    db_session.expire_all()
    assert db_session.query(SubscriptionPlan).filter_by(tier="professional").one().features == edited
    assert "professional" in capsys.readouterr().out
    assert plan.id is not None and uuid.UUID(str(plan.id))
