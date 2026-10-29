"""wa30 data steps (plan flag, feature line, tenant backfill) and the real up/down/up on Postgres."""

import importlib.util
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities
from tests.entitlements.helpers import TIERS, make_tenant
from tests.migrations.test_wa10_whatsapp_channel import (  # noqa: F401  (fixtures + helper)
    _cfg,
    keep_logging,
    scratch_db,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION = os.path.join(_ROOT, "alembic", "versions", "wa30_whatsapp_default_on.py")
_ENT10 = os.path.join(_ROOT, "alembic", "versions", "ent10_plan_entitlements.py")


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load():
    return _module(_MIGRATION, "wa30_whatsapp_default_on")


def _ent10_features(tier):
    """The plan's feature list as ent10 left it (the old WhatsApp line)."""
    return list(_module(_ENT10, "ent10_for_wa30").NEW_FEATURES[tier])


def _caps(db, tenant):
    db.expire_all()
    return db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()


def _pre_migration_tenant(db, tier="lite", *, override_flags=None, stored=False):
    """A tenant as it was before wa30: the stored flag is the old grant (false unless given)."""
    tenant = make_tenant(db, tier)
    row = _caps(db, tenant)
    row.can_use_whatsapp = stored
    row.override_flags = override_flags
    db.commit()
    return tenant


def _set_raw_flags(db, tenant, raw_json):
    """Write override_flags as raw JSON text ('null', '["x"]', '"x"') bypassing the ORM."""
    db.execute(
        text(
            "UPDATE tenant_capabilities SET override_flags = CAST(:f AS JSON) "
            "WHERE tenant_id = :t"
        ),
        {"f": raw_json, "t": tenant.id},
    )
    db.expire_all()


def _set_plan(db, tier, **values):
    db.query(SubscriptionPlan).filter_by(tier=tier).update(values)
    db.commit()
    db.expire_all()


def _features(db, tier):
    db.expire_all()
    return db.query(SubscriptionPlan).filter_by(tier=tier).one().features


def _set_features(db, tier, features):
    db.execute(
        sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
        {"f": json.dumps(features), "tier": tier},
    )


# --- revision graph -------------------------------------------------------------------------


def test_single_head_and_wa30_sits_on_the_referral_head():
    scripts = ScriptDirectory.from_config(_cfg())
    assert len(scripts.get_heads()) == 1
    wa30 = scripts.get_revision("wa30whatsappdefaulton")
    assert wa30.down_revision == "ref10referral"


# --- tenant backfill ------------------------------------------------------------------------


def test_backfill_gives_every_tenant_the_plan_value_of_its_tier(db_session):
    mod = _load()
    tenants = {tier: _pre_migration_tenant(db_session, tier) for tier in TIERS}
    assert mod.backfill_tenants(db_session.connection()) >= len(TIERS)
    for tier, tenant in tenants.items():
        assert _caps(db_session, tenant).can_use_whatsapp is True, tier


def test_backfill_copies_a_plan_value_of_false_too(db_session):
    mod = _load()
    _set_plan(db_session, "lite", can_use_whatsapp=False)
    tenant = _pre_migration_tenant(db_session, "lite", stored=True)
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).can_use_whatsapp is False


def test_an_admin_override_of_false_survives_the_backfill(db_session):
    mod = _load()
    tenant = _pre_migration_tenant(
        db_session, "professional", override_flags={"can_use_whatsapp": True}, stored=False
    )
    mod.backfill_tenants(db_session.connection())
    caps = _caps(db_session, tenant)
    assert caps.can_use_whatsapp is False
    assert caps.override_flags == {"can_use_whatsapp": True}


def test_an_admin_override_of_true_survives_a_plan_that_says_no(db_session):
    mod = _load()
    _set_plan(db_session, "lite", can_use_whatsapp=False)
    tenant = _pre_migration_tenant(
        db_session, "lite", override_flags={"can_use_whatsapp": True}, stored=True
    )
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).can_use_whatsapp is True


def test_other_overrides_do_not_block_the_backfill(db_session):
    mod = _load()
    tenant = _pre_migration_tenant(
        db_session, "growth", override_flags={"can_use_sales_close": True}, stored=False
    )
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).can_use_whatsapp is True


def test_a_tenant_with_null_override_flags_gets_the_plan_value(db_session):
    mod = _load()
    tenant = _pre_migration_tenant(db_session, "professional")
    db_session.execute(
        text("UPDATE tenant_capabilities SET override_flags = NULL WHERE tenant_id = :t"),
        {"t": tenant.id},
    )
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).can_use_whatsapp is True


@pytest.mark.parametrize("raw", ["null", '["can_use_whatsapp"]', '"can_use_whatsapp"', "7", "true"])
def test_non_object_override_flags_count_as_no_override_and_never_abort(db_session, raw):
    mod = _load()
    tenant = _pre_migration_tenant(db_session, "professional")
    _set_raw_flags(db_session, tenant, raw)
    assert mod.backfill_tenants(db_session.connection()) >= 1
    assert _caps(db_session, tenant).can_use_whatsapp is True


@pytest.mark.parametrize("raw", [None, "null", '["can_use_whatsapp"]', '"x"', "[]", "{}"])
def test_json_dict_returns_an_empty_dict_for_anything_that_is_not_a_dict(raw):
    mod = _load()
    assert mod._json_dict(raw) == {}
    assert mod._json_dict(json.loads(raw) if raw else raw) == {}


def test_json_dict_keeps_a_real_dict_in_either_form():
    mod = _load()
    assert mod._json_dict({"a": 1}) == {"a": 1}
    assert mod._json_dict('{"a": 1}') == {"a": 1}


def test_a_tenant_whose_tier_has_no_plan_row_is_left_alone(db_session):
    mod = _load()
    tenant = _pre_migration_tenant(db_session, "lite")
    db_session.execute(
        text("UPDATE tenants SET subscription_tier = 'legacy' WHERE id = :t"), {"t": tenant.id}
    )
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).can_use_whatsapp is False


def test_backfill_is_idempotent(db_session):
    mod = _load()
    _pre_migration_tenant(db_session, "lite")
    _pre_migration_tenant(db_session, "growth", override_flags={"can_use_whatsapp": True})
    assert mod.backfill_tenants(db_session.connection()) >= 1
    assert mod.backfill_tenants(db_session.connection()) == 0


def test_backfill_leaves_the_numbers_alone(db_session):
    """max_whatsapp_numbers was backfilled by ent10; wa30 only touches the flag."""
    mod = _load()
    tenant = _pre_migration_tenant(db_session, "professional")
    row = _caps(db_session, tenant)
    row.max_whatsapp_numbers = 7
    db_session.commit()
    mod.backfill_tenants(db_session.connection())
    assert _caps(db_session, tenant).max_whatsapp_numbers == 7


# --- plan rows ------------------------------------------------------------------------------


def test_set_plan_flag_turns_it_on_for_all_five_tiers_and_is_idempotent(db_session):
    mod = _load()
    for tier in TIERS:
        _set_plan(db_session, tier, can_use_whatsapp=False)
    mod.set_plan_flag(db_session.connection())
    mod.set_plan_flag(db_session.connection())
    db_session.expire_all()
    flags = {p.tier: p.can_use_whatsapp for p in db_session.query(SubscriptionPlan).all()}
    assert all(flags[tier] is True for tier in TIERS)


def test_the_whatsapp_line_is_replaced_in_place_and_nothing_else_changes(db_session):
    mod = _load()
    for tier in TIERS:
        old_list = _ent10_features(tier)
        _set_features(db_session, tier, old_list)
        mod.rewrite_plan_features(db_session.connection())
        new_list = _features(db_session, tier)
        old_line, new_line = mod.WHATSAPP_FEATURE[tier]
        index = old_list.index(old_line)
        assert new_list[index] == new_line, tier
        assert new_list[:index] + new_list[index + 1 :] == old_list[:index] + old_list[index + 1 :]
        assert "on request" not in " ".join(new_list)


def test_feature_rewrite_is_idempotent_and_silent_once_converged(db_session, capsys):
    mod = _load()
    _set_features(db_session, "lite", _ent10_features("lite"))
    mod.rewrite_plan_features(db_session.connection())
    converged = _features(db_session, "lite")
    capsys.readouterr()
    mod.rewrite_plan_features(db_session.connection())
    assert _features(db_session, "lite") == converged
    assert "lite" not in capsys.readouterr().out


def test_a_hand_edited_list_without_the_exact_line_is_left_alone_with_a_warning(db_session, capsys):
    mod = _load()
    edited = ["Our own marketing line", "WhatsApp (beta, on request): 1 number!"]
    _set_features(db_session, "professional", edited)
    mod.rewrite_plan_features(db_session.connection())
    assert _features(db_session, "professional") == edited
    out = capsys.readouterr().out
    assert "WARNING" in out and "professional" in out


def test_a_hand_edited_list_that_still_holds_the_exact_line_only_loses_that_line(db_session):
    mod = _load()
    old_line, new_line = mod.WHATSAPP_FEATURE["growth"]
    _set_features(db_session, "growth", ["Our own marketing line", old_line, "Another line"])
    mod.rewrite_plan_features(db_session.connection())
    assert _features(db_session, "growth") == ["Our own marketing line", new_line, "Another line"]


def test_feature_rewrite_reverses(db_session):
    mod = _load()
    for tier in TIERS:
        old_list = _ent10_features(tier)
        _set_features(db_session, tier, old_list)
        mod.rewrite_plan_features(db_session.connection())
        mod.rewrite_plan_features(db_session.connection(), reverse=True)
        assert _features(db_session, tier) == old_list, tier


def test_feature_rewrite_is_the_catalog_string(db_session):
    """wa30's frozen new lines are what the catalog generates today."""
    mod = _load()
    from app.services.plan_catalog import build_plan_rows

    for row in build_plan_rows():
        assert mod.WHATSAPP_FEATURE[row["tier"]][1] in row["features"]


# --- downgrade data step --------------------------------------------------------------------


def test_unwind_turns_the_flag_off_except_for_hand_set_tenants(db_session):
    mod = _load()
    plain = _pre_migration_tenant(db_session, "lite", stored=True)
    null_flags = _pre_migration_tenant(db_session, "lite", stored=True)
    db_session.execute(
        text("UPDATE tenant_capabilities SET override_flags = NULL WHERE tenant_id = :t"),
        {"t": null_flags.id},
    )
    granted = _pre_migration_tenant(
        db_session, "lite", stored=True, override_flags={"can_use_whatsapp": True}
    )
    revoked = _pre_migration_tenant(
        db_session, "lite", stored=False, override_flags={"can_use_whatsapp": True}
    )
    assert mod.unwind_tenants(db_session.connection()) >= 2
    assert _caps(db_session, plain).can_use_whatsapp is False
    assert _caps(db_session, null_flags).can_use_whatsapp is False
    assert _caps(db_session, granted).can_use_whatsapp is True
    assert _caps(db_session, revoked).can_use_whatsapp is False
    assert mod.unwind_tenants(db_session.connection()) == 0


@pytest.mark.parametrize("raw", ["null", '["can_use_whatsapp"]', '"can_use_whatsapp"'])
def test_unwind_treats_non_object_override_flags_as_no_override(db_session, raw):
    mod = _load()
    tenant = _pre_migration_tenant(db_session, "lite", stored=True)
    _set_raw_flags(db_session, tenant, raw)
    assert mod.unwind_tenants(db_session.connection()) >= 1
    assert _caps(db_session, tenant).can_use_whatsapp is False


def test_backfill_and_unwind_count_only_the_rows_they_changed(db_session):
    mod = _load()
    _pre_migration_tenant(db_session, "lite")
    _pre_migration_tenant(db_session, "lite", override_flags={"can_use_whatsapp": True})
    conn = db_session.connection()
    changed = mod.backfill_tenants(conn)
    assert changed >= 1
    assert mod.backfill_tenants(conn) == 0
    assert mod.unwind_tenants(conn) >= changed
    assert mod.unwind_tenants(conn) == 0


# --- the real upgrade() / downgrade() on a scratch Postgres database ------------------------


def _seed_pre_state(engine):
    """Rows as they stand at ref10referral: plans carry ent10's feature lines, no flag column."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM subscription_plans"))
        for order, tier in enumerate(TIERS):
            conn.execute(
                text(
                    "INSERT INTO subscription_plans (id, tier, display_name, description, "
                    "monthly_price_kobo, annual_price_kobo, can_access_analytics, "
                    "can_use_url_sources, max_response_tokens, max_automation_actions, "
                    "can_use_ai_chat_automation, can_use_lookup_chains, can_use_webhooks, "
                    "features, is_active, sort_order, created_at, updated_at) VALUES (:id, :t, "
                    ":t, 'd', 0, 0, true, true, 800, 2, true, false, false, CAST(:f AS JSON), "
                    "true, :o, now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": tier,
                    "f": json.dumps(_ent10_features(tier)),
                    "o": order,
                },
            )
        # name, tier, override_flags (None = SQL NULL), stored flag
        for name, tier, flags, stored in (
            ("t-lite", "lite", {}, False),
            ("t-pro-null", "professional", None, False),
            ("t-lite-off", "lite", {"can_use_whatsapp": True}, False),
            ("t-growth-on", "growth", {"can_use_whatsapp": True}, True),
            ("t-legacy", "legacy", {}, False),
            ("t-list-flags", "lite", ["can_use_whatsapp"], False),
            ("t-string-flags", "professional", "can_use_whatsapp", False),
            ("t-json-null-flags", "growth", "JSON-NULL", False),
        ):
            tid = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO tenants (id,name,display_name,status,api_key_hash,"
                    "subscription_tier,subscription_status,is_publicly_listed,created_at,"
                    "updated_at) VALUES (:id,:n,:n,'ACTIVE','x',:t,'active',false,now(),now())"
                ),
                {"id": tid, "n": name, "t": tier},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_capabilities (id,tenant_id,can_create_tickets,"
                    "can_access_analytics,can_upload_knowledge,max_knowledge_mb,"
                    "can_use_url_sources,can_use_voice,can_use_whatsapp,max_whatsapp_numbers,"
                    "can_use_ai_chat_automation,can_use_lookup_chains,can_use_webhooks,"
                    "max_automation_actions,override_flags,created_at,updated_at) VALUES "
                    "(:cid,:id,true,true,true,100,false,false,:on,1,true,false,false,5,"
                    "CAST(:f AS JSON),now(),now())"
                ),
                {
                    "cid": str(uuid.uuid4()),
                    "id": tid,
                    "on": stored,
                    "f": (
                        None
                        if flags is None
                        else "null" if flags == "JSON-NULL" else json.dumps(flags)
                    ),
                },
            )


def _state(engine):
    with engine.connect() as conn:
        tenants = {
            r.name: r
            for r in conn.execute(
                text(
                    "SELECT t.name, c.can_use_whatsapp AS wa, c.max_whatsapp_numbers AS n "
                    "FROM tenants t JOIN tenant_capabilities c ON c.tenant_id = t.id"
                )
            )
        }
        features = {
            r.tier: r.features
            for r in conn.execute(text("SELECT tier, features FROM subscription_plans"))
        }
    return tenants, features


def test_real_upgrade_downgrade_upgrade_round_trip(scratch_db):
    cfg = _cfg()
    command.upgrade(cfg, "ref10referral")
    assert "can_use_whatsapp" not in {
        c["name"] for c in inspect(scratch_db).get_columns("subscription_plans")
    }
    _seed_pre_state(scratch_db)
    _, before_features = _state(scratch_db)

    command.upgrade(cfg, "wa30whatsappdefaulton")
    column = {c["name"]: c for c in inspect(scratch_db).get_columns("subscription_plans")}[
        "can_use_whatsapp"
    ]
    assert column["nullable"] is False and "false" in str(column["default"]).lower()
    with scratch_db.connect() as conn:
        flags = {
            r.tier: r.can_use_whatsapp
            for r in conn.execute(text("SELECT tier, can_use_whatsapp FROM subscription_plans"))
        }
    assert flags == dict.fromkeys(TIERS, True)
    tenants, features = _state(scratch_db)
    assert tenants["t-lite"].wa is True
    assert tenants["t-pro-null"].wa is True  # NULL override_flags = no override
    assert tenants["t-lite-off"].wa is False  # admin said no: kept
    assert tenants["t-growth-on"].wa is True
    assert tenants["t-legacy"].wa is False  # no plan row for its tier: left alone
    # override_flags that is not an object (list, string, JSON null) is no override
    assert tenants["t-list-flags"].wa is True
    assert tenants["t-string-flags"].wa is True
    assert tenants["t-json-null-flags"].wa is True
    assert all(t.n == 1 for t in tenants.values())  # numbers untouched
    for tier in TIERS:
        assert not any("on request" in line for line in features[tier]), tier
        assert any(line.startswith("WhatsApp (beta): ") for line in features[tier]), tier

    command.downgrade(cfg, "ref10referral")
    assert "can_use_whatsapp" not in {
        c["name"] for c in inspect(scratch_db).get_columns("subscription_plans")
    }
    assert "can_use_whatsapp" in {
        c["name"] for c in inspect(scratch_db).get_columns("tenant_capabilities")
    }
    tenants, features = _state(scratch_db)
    assert features == before_features  # the old feature lines are back, byte for byte
    assert tenants["t-lite"].wa is False and tenants["t-pro-null"].wa is False
    assert tenants["t-list-flags"].wa is False and tenants["t-string-flags"].wa is False
    assert tenants["t-json-null-flags"].wa is False
    assert tenants["t-lite-off"].wa is False  # hand-set tenants keep their value
    assert tenants["t-growth-on"].wa is True

    command.upgrade(cfg, "head")  # and back up again
    tenants, features = _state(scratch_db)
    assert tenants["t-lite"].wa is True and tenants["t-pro-null"].wa is True
    assert tenants["t-lite-off"].wa is False and tenants["t-growth-on"].wa is True
    assert all(
        any(line.startswith("WhatsApp (beta): ") for line in features[tier]) for tier in TIERS
    )

    # A second upgrade pass changes nothing.
    mod = _load()
    with scratch_db.begin() as conn:
        assert mod.backfill_tenants(conn) == 0
