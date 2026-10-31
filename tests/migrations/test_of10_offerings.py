"""of10offerings: tables match the models, plan columns/values/backfill converge, round trip (Postgres)."""

import importlib.util
import os

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.base import Base
from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities
from app.services.plan_catalog import build_plan_rows
from tests.entitlements.helpers import TIERS, make_tenant
from tests.migrations.test_wa10_whatsapp_channel import (  # noqa: F401  (fixtures + helper)
    _cfg,
    keep_logging,
    scratch_db,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION = os.path.join(_ROOT, "alembic", "versions", "of10_offerings.py")

TABLES = (
    "offerings",
    "offering_images",
    "offering_activity_days",
    "tenant_offerings_settings",
    "lead_offering_interests",
    "offering_misses",
)
PLAN_COLUMNS = ("max_offerings", "max_offering_images")


def _load():
    spec = importlib.util.spec_from_file_location("of10_offerings", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _caps(db, tenant):
    db.expire_all()
    return db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()


def _plan(db, tier):
    db.expire_all()
    return db.query(SubscriptionPlan).filter_by(tier=tier).one()


def _snapshot(engine):
    insp = inspect(engine)
    out = {}
    for table in TABLES:
        out[table] = {
            "columns": {
                c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns(table)
            },
            "pk": tuple(insp.get_pk_constraint(table)["constrained_columns"]),
            "indexes": {
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(table)
            },
            "uniques": {
                (u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(table)
            },
            "fks": {
                (tuple(f["constrained_columns"]), f["referred_table"], f["options"].get("ondelete"))
                for f in insp.get_foreign_keys(table)
            },
            "checks": {c["name"] for c in insp.get_check_constraints(table)},
        }
    return out


# --- revision graph -------------------------------------------------------------------------


def test_single_head_and_of10_sits_on_wa40():
    scripts = ScriptDirectory.from_config(_cfg())
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("of10offerings").down_revision == "qv10quotevisitoremail"
    assert scripts.get_heads() == ["of10offerings"]


# --- schema ---------------------------------------------------------------------------------


def test_migrated_tables_are_identical_to_the_models_and_downgrade_reverses(scratch_db):
    cfg = _cfg()
    command.upgrade(cfg, "head")
    migrated = _snapshot(scratch_db)
    insp = inspect(scratch_db)
    for col in PLAN_COLUMNS:
        for table in ("subscription_plans", "tenant_capabilities"):
            assert col in {c["name"] for c in insp.get_columns(table)}, (table, col)

    command.downgrade(cfg, "qv10quotevisitoremail")  # exactly one step back
    insp = inspect(scratch_db)
    for table in TABLES:
        assert not insp.has_table(table), table
    for col in PLAN_COLUMNS:
        for table in ("subscription_plans", "tenant_capabilities"):
            assert col not in {c["name"] for c in insp.get_columns(table)}
    assert insp.has_table("whatsapp_contact_exclusions")  # nothing else was touched

    # Build the same six tables from the models and compare: any drift fails here, not at deploy.
    Base.metadata.create_all(bind=scratch_db, tables=[Base.metadata.tables[t] for t in TABLES])
    from_models = _snapshot(scratch_db)
    for table in TABLES:
        assert migrated[table] == from_models[table], table


def test_round_trip_up_down_up(scratch_db):
    cfg = _cfg()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "qv10quotevisitoremail")
    command.upgrade(cfg, "head")
    insp = inspect(scratch_db)
    for table in TABLES:
        assert insp.has_table(table)
    # the notification enum value survives a downgrade (Postgres cannot drop an enum value)
    with scratch_db.connect() as conn:
        labels = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'notif_event'"
                )
            )
        }
    assert "offering_request" in labels


def test_upgrade_is_idempotent_over_tables_built_from_the_models(scratch_db):
    """create_all-built databases run alembic from an older revision: nothing may raise."""
    cfg = _cfg()
    command.upgrade(cfg, "qv10quotevisitoremail")
    Base.metadata.create_all(bind=scratch_db)  # the six tables already exist
    with scratch_db.begin() as conn:  # create_all does not alter old tables: add the columns too
        for table in ("subscription_plans", "tenant_capabilities"):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN max_offerings INTEGER"))
            conn.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN max_offering_images INTEGER NOT NULL DEFAULT 4"
                )
            )
    assert inspect(scratch_db).has_table("offerings")
    command.upgrade(cfg, "head")  # would raise DuplicateTable / DuplicateColumn if not idempotent
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "qv10quotevisitoremail")
    command.downgrade(cfg, "qv10quotevisitoremail")  # a second downgrade is a no-op
    assert not inspect(scratch_db).has_table("offerings")


# --- data steps -----------------------------------------------------------------------------


def test_frozen_numbers_match_the_catalog():
    mod = _load()
    rows = {r["tier"]: r for r in build_plan_rows()}
    assert set(mod.LIMITS) == set(rows) == set(TIERS)
    for tier, (items, photos) in mod.LIMITS.items():
        assert (rows[tier]["max_offerings"], rows[tier]["max_offering_images"]) == (items, photos)
    assert mod.LIMITS == {
        "trial": (25, 4),
        "lite": (100, 4),
        "professional": (500, 8),
        "growth": (2000, 8),
        "enterprise": (None, 8),
    }
    for tier, line in mod.OFFERINGS_FEATURE.items():
        assert line in rows[tier]["features"], tier


def _blank_plans(db):
    for tier in TIERS:
        plan = _plan(db, tier)
        plan.max_offerings = None
        plan.max_offering_images = 4
        plan.features = [f for f in plan.features if not f.startswith("Offerings")]
    db.commit()


def test_plan_values_are_written_per_tier_and_are_idempotent(db_session):
    mod = _load()
    _blank_plans(db_session)
    bind = db_session.connection()
    mod.set_plan_values(bind)
    mod.set_plan_values(bind)  # second run: same result
    for tier, (items, photos) in mod.LIMITS.items():
        plan = _plan(db_session, tier)
        assert (plan.max_offerings, plan.max_offering_images) == (items, photos), tier


def test_feature_line_is_inserted_after_the_whatsapp_line_once_and_removed_on_reverse(db_session):
    mod = _load()
    _blank_plans(db_session)
    bind = db_session.connection()
    mod.rewrite_plan_features(bind)
    mod.rewrite_plan_features(bind)  # converged: no duplicate
    for tier in TIERS:
        features = _plan(db_session, tier).features
        line = mod.OFFERINGS_FEATURE[tier]
        assert features.count(line) == 1, tier
        wa = next(i for i, f in enumerate(features) if f.startswith("WhatsApp (beta"))
        assert features[wa + 1] == line, tier
    # the generated catalog strings equal what the migration leaves behind
    rows = {r["tier"]: r for r in build_plan_rows()}
    for tier in TIERS:
        assert _plan(db_session, tier).features == rows[tier]["features"], tier
    mod.rewrite_plan_features(bind, reverse=True)
    for tier in TIERS:
        assert not any(f.startswith("Offerings") for f in _plan(db_session, tier).features), tier


def test_a_hand_edited_feature_list_without_whatsapp_gets_the_line_appended(db_session):
    mod = _load()
    _blank_plans(db_session)
    plan = _plan(db_session, "lite")
    plan.features = ["Custom thing"]
    db_session.commit()
    mod.rewrite_plan_features(db_session.connection())
    assert _plan(db_session, "lite").features == ["Custom thing", mod.OFFERINGS_FEATURE["lite"]]


def _blank_tenant(db, tier, override_flags=None):
    tenant = make_tenant(db, tier)
    caps = _caps(db, tenant)
    caps.max_offerings = 7
    caps.max_offering_images = 1
    caps.override_flags = override_flags
    db.commit()
    return tenant


def test_backfill_copies_the_plan_numbers_onto_every_tier(db_session):
    mod = _load()
    tenants = {tier: _blank_tenant(db_session, tier) for tier in TIERS}
    mod.backfill_tenants(db_session.connection())
    for tier, tenant in tenants.items():
        caps = _caps(db_session, tenant)
        assert (caps.max_offerings, caps.max_offering_images) == mod.LIMITS[tier], tier


def test_backfill_skips_a_field_the_admin_overrode_and_only_that_field(db_session):
    mod = _load()
    tenant = _blank_tenant(db_session, "lite", override_flags={"max_offerings": True})
    mod.backfill_tenants(db_session.connection())
    caps = _caps(db_session, tenant)
    assert caps.max_offerings == 7  # kept: hand-set
    assert caps.max_offering_images == 4  # not overridden: follows the plan


def test_backfill_is_idempotent(db_session):
    mod = _load()
    _blank_tenant(db_session, "growth")
    first = mod.backfill_tenants(db_session.connection())
    second = mod.backfill_tenants(db_session.connection())
    assert first >= 1 and second == 0


def test_backfill_leaves_a_tenant_without_a_plan_row_alone(db_session):
    mod = _load()
    tenant = _blank_tenant(db_session, "lite")
    db_session.execute(
        text("UPDATE tenants SET subscription_tier = 'ghost' WHERE id = :i"), {"i": tenant.id}
    )
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).max_offerings == 7
