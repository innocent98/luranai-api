"""ent10 data steps: plan rows, tenant backfill, Growth override tenant, idempotency, reversibility."""

import importlib.util
import json
import os
import uuid

import sqlalchemy as sa
from sqlalchemy import text

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
    assert (
        lite().monthly_price_kobo,
        list(lite().features),
        lite().max_automation_actions,
    ) == original


def test_hand_edited_feature_strings_are_left_alone(db_session, capsys):
    mod = _load()
    plan = db_session.query(SubscriptionPlan).filter_by(tier="professional").one()
    edited = ["Our own marketing line"]
    db_session.execute(
        sa.text(
            "UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = 'professional'"
        ),
        {"f": '["Our own marketing line"]'},
    )
    mod.apply_plan_data(db_session.connection())
    db_session.expire_all()
    assert (
        db_session.query(SubscriptionPlan).filter_by(tier="professional").one().features == edited
    )
    assert "professional" in capsys.readouterr().out
    assert plan.id is not None and uuid.UUID(str(plan.id))


# --- the real upgrade() / downgrade() on a scratch Postgres database (not just the data steps) ---

from alembic import command  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from tests.migrations.test_wa10_whatsapp_channel import (
    _cfg,
    keep_logging,
    scratch_db,
)  # noqa: E402,F401


def _seed_pre_state(engine):
    """Old-state rows at act10onboarding: Lite plan at the old price, five tenants."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM subscription_plans"))
        for tier, price, actions, features in (
            ("trial", 0, 2, ["2 knowledge sources"]),
            ("lite", 900000, 5, ["5 automation actions"]),
            ("professional", 4000000, 15, ["x"]),
            ("growth", 8000000, 40, ["y"]),
        ):
            conn.execute(
                text(
                    "INSERT INTO subscription_plans (id, tier, display_name, description, "
                    "monthly_price_kobo, annual_price_kobo, can_access_analytics, can_use_url_sources, "
                    "max_response_tokens, max_automation_actions, can_use_ai_chat_automation, "
                    "can_use_lookup_chains, can_use_webhooks, features, is_active, sort_order, "
                    "created_at, updated_at) VALUES (:id, :t, :t, 'd', :p, :p, true, true, 800, :a, "
                    "true, false, false, CAST(:f AS JSON), true, 0, now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": tier,
                    "p": price,
                    "a": actions,
                    "f": json.dumps(features),
                },
            )
        for name, tier, flags, wa in (
            ("t-lite", "lite", {}, 0),
            ("t-lite-override", "lite", {"can_use_screen_recording": True}, 0),
            ("t-growth-override", "growth", {"can_use_webhooks": True}, 0),
            ("t-pro-wa", "professional", {}, 3),
        ):
            tid = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO tenants (id,name,display_name,status,api_key_hash,subscription_tier,"
                    "subscription_status,is_publicly_listed,created_at,updated_at) VALUES "
                    "(:id,:n,:n,'ACTIVE','x',:t,'active',false,now(),now())"
                ),
                {"id": tid, "n": name, "t": tier},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_capabilities (id,tenant_id,can_create_tickets,can_access_analytics,"
                    "can_upload_knowledge,max_knowledge_mb,can_use_url_sources,can_use_voice,"
                    "can_use_screen_recording,can_use_whatsapp,max_whatsapp_numbers,"
                    "can_use_ai_chat_automation,can_use_lookup_chains,can_use_webhooks,"
                    "max_automation_actions,override_flags,created_at,updated_at) VALUES "
                    "(:cid,:id,true,true,true,100,false,false,true,:wa_on,:wa,true,false,false,5,"
                    "CAST(:f AS JSON),now(),now())"
                ),
                {
                    "cid": str(uuid.uuid4()),
                    "id": tid,
                    "wa_on": wa > 0,
                    "wa": wa,
                    "f": json.dumps(flags),
                },
            )


def _rows(engine, sales=True):
    sales_col = "c.can_use_sales_close" if sales else "NULL"
    with engine.connect() as conn:
        caps = {
            r.name: r
            for r in conn.execute(
                text(
                    f"SELECT t.name, {sales_col} AS sales, c.can_use_screen_recording AS rec, "
                    "c.max_whatsapp_numbers AS wa, c.max_automation_actions AS act "
                    "FROM tenants t JOIN tenant_capabilities c ON c.tenant_id = t.id"
                )
            )
        }
        plans = {
            r.tier: r
            for r in conn.execute(
                text(
                    "SELECT tier, monthly_price_kobo AS m, max_automation_actions AS act FROM subscription_plans"
                )
            )
        }
    return caps, plans


def test_real_upgrade_downgrade_upgrade_round_trip(scratch_db):
    cfg = _cfg()
    command.upgrade(cfg, "act10onboarding")
    _seed_pre_state(scratch_db)

    command.upgrade(cfg, "ent10entitlements")
    insp = inspect(scratch_db)
    plan_cols = {c["name"]: c for c in insp.get_columns("subscription_plans")}
    caps_cols = {c["name"]: c for c in insp.get_columns("tenant_capabilities")}
    for name in ("can_use_sales_close", "can_use_crm_notifications", "can_export_ratings"):
        assert plan_cols[name]["nullable"] is False and caps_cols[name]["nullable"] is False
    assert "can_use_screen_recording" in plan_cols and "max_whatsapp_numbers" in plan_cols
    assert caps_cols["max_whatsapp_numbers"]["nullable"] is True
    assert caps_cols["max_knowledge_mb"]["nullable"] is True
    assert "false" in str(caps_cols["can_use_screen_recording"]["default"]).lower()
    caps, plans = _rows(scratch_db)
    assert plans["lite"].m == 1_500_000 and plans["lite"].act == 2
    assert (caps["t-lite"].sales, caps["t-lite"].rec, caps["t-lite"].wa, caps["t-lite"].act) == (
        False,
        False,
        1,
        2,
    )
    assert caps["t-lite-override"].rec is True  # override kept
    assert caps["t-lite-override"].act == 2  # its action limit is not overridden
    assert caps["t-growth-override"].sales is True and caps["t-growth-override"].wa == 5
    assert caps["t-pro-wa"].wa == 3  # a hand-granted number is never lowered

    command.downgrade(cfg, "act10onboarding")
    insp = inspect(scratch_db)
    assert "can_use_sales_close" not in {c["name"] for c in insp.get_columns("tenant_capabilities")}
    assert "can_use_screen_recording" not in {
        c["name"] for c in insp.get_columns("subscription_plans")
    }
    caps_cols = {c["name"]: c for c in insp.get_columns("tenant_capabilities")}
    assert caps_cols["max_whatsapp_numbers"]["nullable"] is False
    assert caps_cols["max_knowledge_mb"]["nullable"] is False
    assert "true" in str(caps_cols["can_use_screen_recording"]["default"]).lower()
    caps, plans = _rows(scratch_db, sales=False)
    assert plans["lite"].m == 900_000 and plans["lite"].act == 5
    assert caps["t-lite"].rec is True and caps["t-lite"].act == 5

    command.upgrade(cfg, "head")  # and back up again
    caps, plans = _rows(scratch_db)
    assert plans["lite"].m == 1_500_000
    assert caps["t-lite"].rec is False and caps["t-growth-override"].sales is True
