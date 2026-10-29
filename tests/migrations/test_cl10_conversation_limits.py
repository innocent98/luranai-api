"""cl10 data steps (plan rows, tenant backfill, over-limit warning) and the real up/down/up."""

import importlib.util
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities
from app.services.plan_catalog import build_plan_rows, plan_capabilities
from tests.conversation_counting.conftest import seed_session
from tests.entitlements.helpers import TIERS, make_tenant
from tests.migrations.test_wa10_whatsapp_channel import (  # noqa: F401  (fixtures + helper)
    _cfg,
    keep_logging,
    scratch_db,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION = os.path.join(_ROOT, "alembic", "versions", "cl10_conversation_limit_numbers.py")

# Written out by hand (not read from the migration or the catalog) so a wrong number in either
# cannot hide behind a test that reads the same value.
OLD = {"trial": 200, "lite": 2_000, "professional": 10_000, "growth": 25_000}
NEW = {"trial": 100, "lite": 1_000, "professional": 5_000, "growth": 10_000}
CAPPED = tuple(OLD)


def _load():
    spec = importlib.util.spec_from_file_location("cl10_conversation_limit_numbers", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _conn(db):
    return db.connection()


def _tenant_at(db, tier, value, *, override_flags=None):
    """A tenant whose stored limit is `value` (the pre-migration plan number unless given)."""
    tenant = make_tenant(db, tier)
    row = db.query(TenantCapabilities).filter_by(tenant_id=tenant.id).one()
    row.max_conversations_per_month = value
    row.override_flags = override_flags
    db.commit()
    return tenant


def _limit(db, tenant):
    db.expire_all()
    return (
        db.query(TenantCapabilities)
        .filter_by(tenant_id=tenant.id)
        .one()
        .max_conversations_per_month
    )


def _catalog_features():
    return {row["tier"]: row["features"] for row in build_plan_rows()}


def _old_features(tier):
    """The plan's feature list as it stands before cl10: the catalog's, with the old string."""
    new = f"{NEW[tier]:,} conversations/month"
    return [
        f"{OLD[tier]:,} conversations/month" if item == new else item
        for item in _catalog_features()[tier]
    ]


def _set_features(db, tier, features):
    db.execute(
        sa.text("UPDATE subscription_plans SET features = CAST(:f AS JSON) WHERE tier = :tier"),
        {"f": json.dumps(features), "tier": tier},
    )
    db.expire_all()


def _plan(db, tier):
    db.expire_all()
    return db.query(SubscriptionPlan).filter_by(tier=tier).one()


def _make_old_plan(db, tier):
    """Put the plan row into its pre-cl10 state."""
    db.query(SubscriptionPlan).filter_by(tier=tier).update(
        {"max_conversations_per_month": OLD[tier]}
    )
    _set_features(db, tier, _old_features(tier))
    db.commit()
    db.expire_all()


# --- revision graph and frozen numbers -------------------------------------------------------


def test_single_head_and_cl10_sits_on_the_whatsapp_default_on_head():
    scripts = ScriptDirectory.from_config(_cfg())
    assert len(scripts.get_heads()) == 1
    cl10 = scripts.get_revision("cl10conversationlimits")
    assert cl10.down_revision == "wa30whatsappdefaulton"


def test_the_frozen_numbers_are_the_approved_ones():
    mod = _load()
    assert mod.LIMITS == {tier: (OLD[tier], NEW[tier]) for tier in CAPPED}


def test_the_new_numbers_are_what_the_catalog_generates_today():
    """Migration, catalog and the owner's decision agree; Enterprise stays unlimited."""
    for tier in CAPPED:
        assert plan_capabilities(tier)["max_conversations_per_month"] == NEW[tier], tier
    assert plan_capabilities("enterprise")["max_conversations_per_month"] is None
    for tier, features in _catalog_features().items():
        if tier in NEW:
            assert f"{NEW[tier]:,} conversations/month" in features, tier
    assert "Unlimited conversations/month" in _catalog_features()["enterprise"]


# --- tenant backfill -------------------------------------------------------------------------


@pytest.mark.parametrize("tier", CAPPED)
def test_a_tenant_on_the_old_plan_number_moves_to_the_new_one(db_session, tier):
    mod = _load()
    tenant = _tenant_at(db_session, tier, OLD[tier])
    assert mod.rewrite_tenants(_conn(db_session))[tier] >= 1
    assert _limit(db_session, tenant) == NEW[tier]


def test_enterprise_stays_unlimited(db_session):
    mod = _load()
    tenant = _tenant_at(db_session, "enterprise", None)
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) is None


def test_an_admin_override_survives_in_both_directions(db_session):
    mod = _load()
    flags = {"max_conversations_per_month": True}
    kept_high = _tenant_at(db_session, "growth", 25_000, override_flags=flags)
    kept_other = _tenant_at(db_session, "lite", 4_000, override_flags=flags)
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, kept_high) == 25_000
    assert _limit(db_session, kept_other) == 4_000
    mod.rewrite_tenants(_conn(db_session), reverse=True)
    assert _limit(db_session, kept_high) == 25_000
    assert _limit(db_session, kept_other) == 4_000


def test_an_override_of_the_old_number_itself_is_still_an_override(db_session):
    mod = _load()
    tenant = _tenant_at(
        db_session, "growth", 25_000, override_flags={"max_conversations_per_month": True}
    )
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) == 25_000


def test_other_overrides_do_not_block_the_backfill(db_session):
    mod = _load()
    tenant = _tenant_at(db_session, "growth", 25_000, override_flags={"can_use_voice": True})
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) == 10_000


def test_a_value_that_is_not_the_old_plan_number_is_left_alone(db_session):
    mod = _load()
    odd = _tenant_at(db_session, "professional", 7_777)
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, odd) == 7_777


def test_a_null_limit_on_a_capped_tier_is_left_alone(db_session):
    mod = _load()
    tenant = _tenant_at(db_session, "lite", None)
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) is None


def test_a_tenant_with_null_override_flags_moves(db_session):
    mod = _load()
    tenant = _tenant_at(db_session, "professional", 10_000)
    db_session.execute(
        text("UPDATE tenant_capabilities SET override_flags = NULL WHERE tenant_id = :t"),
        {"t": tenant.id},
    )
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) == 5_000


@pytest.mark.parametrize(
    "raw", ["null", '["max_conversations_per_month"]', '"max_conversations_per_month"', "7"]
)
def test_non_object_override_flags_count_as_no_override_and_never_abort(db_session, raw):
    mod = _load()
    tenant = _tenant_at(db_session, "professional", 10_000)
    db_session.execute(
        text(
            "UPDATE tenant_capabilities SET override_flags = CAST(:f AS JSON) WHERE tenant_id = :t"
        ),
        {"f": raw, "t": tenant.id},
    )
    db_session.expire_all()
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) == 5_000


def test_a_tenant_whose_tier_has_no_plan_row_is_left_alone(db_session):
    mod = _load()
    tenant = _tenant_at(db_session, "lite", 2_000)
    db_session.execute(
        text("UPDATE tenants SET subscription_tier = 'legacy' WHERE id = :t"), {"t": tenant.id}
    )
    mod.rewrite_tenants(_conn(db_session))
    assert _limit(db_session, tenant) == 2_000


def test_the_backfill_is_idempotent_and_counts_only_changed_rows(db_session):
    mod = _load()
    _tenant_at(db_session, "growth", 25_000)
    first = mod.rewrite_tenants(_conn(db_session))
    assert first["growth"] >= 1
    assert sum(mod.rewrite_tenants(_conn(db_session)).values()) == 0


def test_the_downgrade_restores_old_numbers_but_only_for_the_new_value(db_session):
    mod = _load()
    plain = _tenant_at(db_session, "growth", 10_000)
    custom = _tenant_at(db_session, "growth", 12_345)
    mod.rewrite_tenants(_conn(db_session), reverse=True)
    assert _limit(db_session, plain) == 25_000
    assert _limit(db_session, custom) == 12_345


# --- plan rows -------------------------------------------------------------------------------


@pytest.mark.parametrize("tier", CAPPED)
def test_the_plan_row_and_its_string_move_and_nothing_else_changes(db_session, tier):
    mod = _load()
    _make_old_plan(db_session, tier)
    before = list(_plan(db_session, tier).features)
    mod.rewrite_plan_limits(_conn(db_session))
    plan = _plan(db_session, tier)
    assert plan.max_conversations_per_month == NEW[tier]
    index = before.index(f"{OLD[tier]:,} conversations/month")
    assert plan.features[index] == f"{NEW[tier]:,} conversations/month"
    assert plan.features[:index] + plan.features[index + 1 :] == (
        before[:index] + before[index + 1 :]
    )


def test_the_whatsapp_line_wa30_wrote_is_untouched(db_session):
    mod = _load()
    _make_old_plan(db_session, "lite")
    mod.rewrite_plan_limits(_conn(db_session))
    assert any(f.startswith("WhatsApp (beta): ") for f in _plan(db_session, "lite").features)


def test_enterprise_plan_is_not_touched(db_session):
    mod = _load()
    before = list(_plan(db_session, "enterprise").features)
    mod.rewrite_plan_limits(_conn(db_session))
    plan = _plan(db_session, "enterprise")
    assert plan.max_conversations_per_month is None
    assert list(plan.features) == before


def test_plan_rewrite_is_idempotent_and_silent_once_converged(db_session, capsys):
    mod = _load()
    _make_old_plan(db_session, "growth")
    mod.rewrite_plan_limits(_conn(db_session))
    capsys.readouterr()
    converged = list(_plan(db_session, "growth").features)
    mod.rewrite_plan_limits(_conn(db_session))
    assert list(_plan(db_session, "growth").features) == converged
    assert "WARNING" not in capsys.readouterr().out


def test_a_hand_edited_limit_is_left_alone_with_a_warning(db_session, capsys):
    mod = _load()
    _make_old_plan(db_session, "lite")
    db_session.query(SubscriptionPlan).filter_by(tier="lite").update(
        {"max_conversations_per_month": 3_000}
    )
    db_session.commit()
    mod.rewrite_plan_limits(_conn(db_session))
    assert _plan(db_session, "lite").max_conversations_per_month == 3_000
    out = capsys.readouterr().out
    assert "WARNING" in out and "lite" in out


def test_a_hand_edited_list_without_the_exact_string_is_left_alone_with_a_warning(
    db_session, capsys
):
    mod = _load()
    edited = ["Our own marketing line", "2,500 conversations/month"]
    _set_features(db_session, "lite", edited)
    mod.rewrite_plan_limits(_conn(db_session))
    assert list(_plan(db_session, "lite").features) == edited
    out = capsys.readouterr().out
    assert "WARNING" in out and "lite" in out


def test_a_hand_edited_list_that_still_holds_the_exact_string_only_loses_that_string(db_session):
    mod = _load()
    _set_features(
        db_session, "growth", ["Our own marketing line", "25,000 conversations/month", "Another"]
    )
    mod.rewrite_plan_limits(_conn(db_session))
    assert list(_plan(db_session, "growth").features) == [
        "Our own marketing line",
        "10,000 conversations/month",
        "Another",
    ]


@pytest.mark.parametrize("tier", CAPPED)
def test_plan_rewrite_reverses_byte_for_byte(db_session, tier):
    mod = _load()
    _make_old_plan(db_session, tier)
    old_features = list(_plan(db_session, tier).features)
    mod.rewrite_plan_limits(_conn(db_session))
    mod.rewrite_plan_limits(_conn(db_session), reverse=True)
    plan = _plan(db_session, tier)
    assert plan.max_conversations_per_month == OLD[tier]
    assert list(plan.features) == old_features


# --- the over-limit warning ------------------------------------------------------------------


def test_a_tenant_already_over_its_new_limit_is_named_and_nothing_is_deleted(db_session, capsys):
    mod = _load()
    tenant = _tenant_at(db_session, "trial", 100)
    for _ in range(101):
        seed_session(db_session, tenant.id)
    quiet = _tenant_at(db_session, "trial", 100)
    for _ in range(100):  # exactly at the limit is not over it
        seed_session(db_session, quiet.id)

    assert mod.warn_over_limit(_conn(db_session)) >= 1
    out = capsys.readouterr().out
    assert str(tenant.id) in out and "101" in out and "WARNING" in out
    assert str(quiet.id) not in out
    count = db_session.execute(
        text("SELECT count(*) FROM conversation_sessions WHERE tenant_id = :t"), {"t": tenant.id}
    ).scalar()
    assert count == 101  # warned, never cut


def test_an_overridden_tenant_is_not_warned_about(db_session, capsys):
    mod = _load()
    tenant = _tenant_at(
        db_session, "trial", 500, override_flags={"max_conversations_per_month": True}
    )
    for _ in range(150):
        seed_session(db_session, tenant.id)
    mod.warn_over_limit(_conn(db_session))
    assert str(tenant.id) not in capsys.readouterr().out


def test_last_months_conversations_do_not_count_toward_the_warning(db_session, capsys):
    mod = _load()
    tenant = _tenant_at(db_session, "trial", 100)
    for _ in range(150):
        seed_session(db_session, tenant.id, hours_ago=24 * 70, created_hours_ago=24 * 70)
    mod.warn_over_limit(_conn(db_session))
    assert str(tenant.id) not in capsys.readouterr().out


# --- the existing downgrade-validation path uses the new numbers ------------------------------


@pytest.mark.parametrize("tier", ("trial", "lite", "professional", "growth"))
def test_downgrade_validation_warns_against_the_new_limit(db_session, tier):
    """subscription_validation_service._check_conversations reads the plan's capabilities, so it
    warns at the new number with no code change."""
    from app.services.subscription_validation_service import SubscriptionValidationService

    tenant = make_tenant(db_session, "professional")
    service = SubscriptionValidationService(db_session)
    limits = plan_capabilities(tier)
    over = limits["max_conversations_per_month"] + 1
    seeded = db_session.execute(
        text(
            "SELECT count(*) FROM conversation_sessions WHERE tenant_id = :t "
            "AND created_at >= date_trunc('month', now())"
        ),
        {"t": tenant.id},
    ).scalar()
    assert seeded == 0
    for _ in range(over):
        seed_session(db_session, tenant.id)
    warning = service._check_conversations(tenant.id, limits)
    assert warning is not None and f"({limits['max_conversations_per_month']})" in warning


# --- the real upgrade() / downgrade() on a scratch Postgres database --------------------------


def _seed_pre_state(engine):
    """Rows as they stand at wa30: old numbers on the plans, the tenants and their sessions."""
    mod = _load()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM subscription_plans"))
        for order, tier in enumerate(TIERS):
            limit = OLD.get(tier)
            strings = [
                "20 knowledge sources",
                "Unlimited conversations/month" if limit is None else mod._feature(limit),
                "WhatsApp (beta): 1 number",
                "Full support",
            ]
            conn.execute(
                text(
                    "INSERT INTO subscription_plans (id, tier, display_name, description, "
                    "monthly_price_kobo, annual_price_kobo, can_access_analytics, "
                    "can_use_url_sources, max_conversations_per_month, max_response_tokens, "
                    "max_automation_actions, can_use_ai_chat_automation, can_use_lookup_chains, "
                    "can_use_webhooks, can_use_whatsapp, features, is_active, sort_order, "
                    "created_at, updated_at) VALUES (:id, :t, :t, 'd', 0, 0, true, true, :lim, "
                    "800, 2, true, false, false, true, CAST(:f AS JSON), true, :o, now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "t": tier,
                    "lim": limit,
                    "f": json.dumps(strings),
                    "o": order,
                },
            )
        # name, tier, stored limit, override_flags (None = SQL NULL)
        tenants = {}
        for name, tier, stored, flags in (
            ("t-trial", "trial", 200, {}),
            ("t-lite", "lite", 2_000, None),
            ("t-pro", "professional", 10_000, {}),
            ("t-growth", "growth", 25_000, {"can_use_voice": True}),
            ("t-growth-held", "growth", 25_000, {"max_conversations_per_month": True}),
            ("t-pro-custom", "professional", 7_777, {}),
            ("t-enterprise", "enterprise", None, {}),
            ("t-legacy", "legacy", 2_000, {}),
        ):
            tid = str(uuid.uuid4())
            tenants[name] = tid
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
                    "max_automation_actions,max_conversations_per_month,override_flags,"
                    "created_at,updated_at) VALUES "
                    "(:cid,:id,true,true,true,100,false,false,true,1,true,false,false,5,"
                    ":lim,CAST(:f AS JSON),now(),now())"
                ),
                {
                    "cid": str(uuid.uuid4()),
                    "id": tid,
                    "lim": stored,
                    "f": None if flags is None else json.dumps(flags),
                },
            )
        # a trial tenant already past its new limit of 100 this month
        for n in range(101):
            conn.execute(
                text(
                    "INSERT INTO conversation_sessions (id, tenant_id, user_identifier, status, "
                    "created_at, updated_at, last_message_at) VALUES (:id, :t, :u, 'ACTIVE', "
                    "now(), now(), now())"
                ),
                {"id": str(uuid.uuid4()), "t": tenants["t-trial"], "u": f"u-{n}"},
            )


def _state(engine):
    with engine.connect() as conn:
        tenants = {
            r.name: r.lim
            for r in conn.execute(
                text(
                    "SELECT t.name, c.max_conversations_per_month AS lim "
                    "FROM tenants t JOIN tenant_capabilities c ON c.tenant_id = t.id"
                )
            )
        }
        plans = {
            r.tier: (r.lim, r.features)
            for r in conn.execute(
                text(
                    "SELECT tier, max_conversations_per_month AS lim, features "
                    "FROM subscription_plans"
                )
            )
        }
        sessions = conn.execute(text("SELECT count(*) FROM conversation_sessions")).scalar()
    return tenants, plans, sessions


def test_real_upgrade_downgrade_upgrade_round_trip(scratch_db, capfd):
    cfg = _cfg()
    command.upgrade(cfg, "wa30whatsappdefaulton")
    _seed_pre_state(scratch_db)
    before_tenants, before_plans, before_sessions = _state(scratch_db)
    assert before_sessions == 101

    command.upgrade(cfg, "cl10conversationlimits")
    tenants, plans, sessions = _state(scratch_db)
    assert tenants["t-trial"] == 100
    assert tenants["t-lite"] == 1_000
    assert tenants["t-pro"] == 5_000
    assert tenants["t-growth"] == 10_000
    assert tenants["t-growth-held"] == 25_000  # the admin override keeps its number
    assert tenants["t-pro-custom"] == 7_777  # not the old plan number: left alone
    assert tenants["t-enterprise"] is None  # unlimited stays unlimited
    assert tenants["t-legacy"] == 2_000  # no plan row for its tier: left alone
    assert {t: plans[t][0] for t in plans} == {**NEW, "enterprise": None}
    for tier, limit in NEW.items():
        assert f"{limit:,} conversations/month" in plans[tier][1], tier
        assert f"{OLD[tier]:,} conversations/month" not in plans[tier][1], tier
        assert "WhatsApp (beta): 1 number" in plans[tier][1]
    assert sessions == 101  # nothing was deleted
    out = capfd.readouterr().out
    assert "WARNING" in out and "101" in out  # the over-limit trial tenant was named

    command.downgrade(cfg, "wa30whatsappdefaulton")
    after_tenants, after_plans, after_sessions = _state(scratch_db)
    assert after_tenants == before_tenants
    assert after_plans == before_plans  # old numbers and strings back, byte for byte
    assert after_sessions == 101

    command.upgrade(cfg, "head")  # and back up again
    tenants_again, plans_again, _ = _state(scratch_db)
    assert tenants_again == tenants and plans_again == plans
