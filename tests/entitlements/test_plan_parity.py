"""One definition of the plans: subscription_plans, tenant_capabilities, schemas, seed and migration."""

import importlib.util
import os

from sqlalchemy import inspect

from app.db.models.subscription_plan import (
    PLAN_META_FIELDS,
    PLAN_ONLY_CAPABILITY_FIELDS,
    SubscriptionPlan,
)
from app.db.models.tenant import TENANT_ONLY_CAPABILITY_FIELDS, TenantCapabilities
from app.schemas.pricing import PricingPlanCapabilitiesResponse, PricingPlanCapabilitiesUpdate
from app.schemas.tenant import TenantCapabilitiesResponse, TenantCapabilitiesUpdate
from app.services.entitlements import plan_capability_fields
from app.services.plan_catalog import TIER_ORDER, build_feature_strings, build_plan_rows
from scripts.seed_data import create_subscription_plans
from tests.entitlements.helpers import TIERS

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "ent10_plan_entitlements.py",
)


def _plan_columns():
    return {c.key: c for c in SubscriptionPlan.__table__.columns}


def _caps_columns():
    return {c.key: c for c in TenantCapabilities.__table__.columns}


def test_every_plan_capability_exists_on_tenant_capabilities_and_back():
    plan_caps = set(_plan_columns()) - PLAN_META_FIELDS - PLAN_ONLY_CAPABILITY_FIELDS
    tenant_caps = set(_caps_columns()) - TENANT_ONLY_CAPABILITY_FIELDS
    assert plan_caps - tenant_caps == set(), (
        "plan columns missing on tenant_capabilities (add the column + migration, or list it in "
        f"PLAN_ONLY_CAPABILITY_FIELDS): {sorted(plan_caps - tenant_caps)}"
    )
    assert tenant_caps - plan_caps == set(), (
        "tenant_capabilities columns missing on subscription_plans (add the plan column, or list "
        f"it in TENANT_ONLY_CAPABILITY_FIELDS): {sorted(tenant_caps - plan_caps)}"
    )
    assert set(plan_capability_fields()) == plan_caps


def test_mirrored_columns_have_the_same_type_and_nullability():
    plan, caps = _plan_columns(), _caps_columns()
    for name in plan_capability_fields():
        assert type(plan[name].type) is type(caps[name].type), name
        assert plan[name].nullable == caps[name].nullable, name


def test_new_gate_columns_default_to_off_in_the_database():
    for name in ("can_use_sales_close", "can_use_crm_notifications", "can_export_ratings"):
        for table in (_plan_columns(), _caps_columns()):
            assert str(table[name].server_default.arg).lower() in ("false", "0"), name
    assert str(_caps_columns()["can_use_screen_recording"].server_default.arg).lower() == "false"


def test_the_live_tables_have_the_same_columns_as_the_models(pg_engine):
    columns = {
        t: {c["name"] for c in inspect(pg_engine).get_columns(t)}
        for t in (
            "subscription_plans",
            "tenant_capabilities",
        )
    }
    assert set(plan_capability_fields()) <= columns["subscription_plans"]
    assert set(plan_capability_fields()) <= columns["tenant_capabilities"]


def test_schemas_expose_every_plan_capability():
    names = set(SubscriptionPlan.capability_columns())
    assert set(PricingPlanCapabilitiesResponse.model_fields) == names
    assert set(PricingPlanCapabilitiesUpdate.model_fields) == names
    mirrored = set(plan_capability_fields())
    assert mirrored <= set(TenantCapabilitiesResponse.model_fields)
    assert mirrored <= set(TenantCapabilitiesUpdate.model_fields)


def test_catalog_covers_every_tier_and_every_capability_column():
    rows = build_plan_rows()
    assert [r["tier"] for r in rows] == list(TIER_ORDER) == list(TIERS)
    wanted = set(SubscriptionPlan.capability_columns())
    for row in rows:
        assert wanted <= set(row), row["tier"]


def test_seeded_rows_equal_the_catalog(db_session):
    plans = {p.tier: p for p in db_session.query(SubscriptionPlan).all()}
    for row in build_plan_rows():
        plan = plans[row["tier"]]
        for key, value in row.items():
            assert getattr(plan, key) == value, f"{row['tier']}.{key}"


def test_reseeding_puts_the_approved_values_back(db_session):
    """The seed is the single writer: drifted staging rows are corrected, never reverted."""
    lite = db_session.query(SubscriptionPlan).filter_by(tier="lite").one()
    lite.monthly_price_kobo = 900_000
    lite.can_use_sales_close = True
    lite.max_automation_actions = 5
    db_session.commit()
    create_subscription_plans(db_session)
    db_session.expire_all()
    lite = db_session.query(SubscriptionPlan).filter_by(tier="lite").one()
    assert lite.monthly_price_kobo == 1_500_000 and lite.annual_price_kobo == 15_000_000
    assert lite.can_use_sales_close is False
    assert lite.max_automation_actions == 2


def test_lite_price_is_15000_naira_with_ten_months_annual():
    lite = next(r for r in build_plan_rows() if r["tier"] == "lite")
    assert lite["monthly_price_kobo"] == 1_500_000
    assert lite["annual_price_kobo"] == 15_000_000 == 10 * lite["monthly_price_kobo"]


def test_feature_strings_are_generated_from_the_capabilities(db_session):
    for plan in db_session.query(SubscriptionPlan).all():
        assert plan.features == build_feature_strings(plan.tier, plan.get_capabilities()), plan.tier


def test_feature_strings_only_promise_what_is_enforced(db_session):
    promised = {
        "Meeting booking and quotes": "can_use_sales_close",
        "Slack and CRM notifications": "can_use_crm_notifications",
        "Screen recording": "can_use_screen_recording",
        "Ratings breakdown and CSV export": "can_export_ratings",
        "Lookup chains": "can_use_lookup_chains",
        "Analytics dashboard": "can_access_analytics",
        "URL scraping": "can_use_url_sources",
        "AI chat automation": "can_use_ai_chat_automation",
    }
    for plan in db_session.query(SubscriptionPlan).all():
        for text, column in promised.items():
            assert (text in plan.features) is bool(getattr(plan, column)), (plan.tier, text)
        assert any("webhooks per flow" in f for f in plan.features) is plan.can_use_webhooks


def test_lite_marketing_matches_the_matrix():
    lite = next(r for r in build_plan_rows() if r["tier"] == "lite")["features"]
    assert "2 automation actions" in lite
    assert "Analytics dashboard" in lite
    for gated in (
        "Meeting booking and quotes",
        "Slack and CRM notifications",
        "Screen recording",
        "Ratings breakdown and CSV export",
        "Lookup chains",
    ):
        assert gated not in lite


def test_latest_plan_migration_froze_the_same_numbers_as_the_catalog():
    """If this fails you changed plan values: add a new migration and repoint this test."""
    spec = importlib.util.spec_from_file_location("ent10", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = {r["tier"]: r for r in build_plan_rows()}
    for tier, (sales, crm, recording, ratings, whatsapp) in mod.GATES.items():
        row = rows[tier]
        assert (
            row["can_use_sales_close"],
            row["can_use_crm_notifications"],
            row["can_use_screen_recording"],
            row["can_export_ratings"],
            row["max_whatsapp_numbers"],
        ) == (sales, crm, recording, ratings, whatsapp), tier
        assert mod.NEW_FEATURES[tier] == row["features"], tier
    assert (
        rows["lite"]["monthly_price_kobo"],
        rows["lite"]["annual_price_kobo"],
    ) == mod.LITE_PRICE["new"]
    assert rows["lite"]["max_automation_actions"] == mod.LITE_ACTIONS["new"]
