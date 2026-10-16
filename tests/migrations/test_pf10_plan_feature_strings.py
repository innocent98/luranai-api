"""pf10 data step + a guard that plan marketing strings match the enforced limits.

GET /subscription/plans returns both the capability columns and a `features`
list of display strings. b7d21c9f3a04 moved the columns without the strings,
so the public plans response said "15 knowledge sources" while enforcing 20.
The consistency test below fails the next time the two drift apart.
"""

import importlib.util
import os
import re

from app.db.models.subscription_plan import SubscriptionPlan

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "pf10_plan_feature_strings.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("pf10_plan_feature_strings", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rewrite_features_replaces_only_an_exact_match():
    mod = _load()
    assert mod.rewrite_features(
        ["15 knowledge sources", "50MB storage"], "15 knowledge sources", "20 knowledge sources"
    ) == ["20 knowledge sources", "50MB storage"]
    # Hand-edited copy is not ours to overwrite.
    assert (
        mod.rewrite_features(
            ["Up to 15 knowledge sources"], "15 knowledge sources", "20 knowledge sources"
        )
        is None
    )
    # Already migrated: nothing to do.
    assert (
        mod.rewrite_features(
            ["20 knowledge sources"], "15 knowledge sources", "20 knowledge sources"
        )
        is None
    )


def _features(db, tier):
    db.expire_all()
    return db.query(SubscriptionPlan.features).filter(SubscriptionPlan.tier == tier).scalar()


def test_data_step_upgrades_downgrades_and_is_idempotent(db_session):
    db = db_session  # Postgres, seeded from scripts/seed_data.py
    mod = _load()
    original = {tier: list(_features(db, tier)) for tier in ("lite", "growth")}
    stale = {
        "lite": [
            "15 knowledge sources" if f == "20 knowledge sources" else f for f in original["lite"]
        ],
        "growth": [
            "50 knowledge sources" if f == "100 knowledge sources" else f
            for f in original["growth"]
        ],
    }
    try:
        for tier, features in stale.items():
            db.query(SubscriptionPlan).filter(SubscriptionPlan.tier == tier).update(
                {"features": features}, synchronize_session=False
            )
        db.commit()

        assert mod.apply_changes(db.connection()) == 2
        db.commit()
        assert "20 knowledge sources" in _features(db, "lite")
        assert "15 knowledge sources" not in _features(db, "lite")
        assert "100 knowledge sources" in _features(db, "growth")
        assert "50 knowledge sources" not in _features(db, "growth")

        assert mod.apply_changes(db.connection()) == 0
        db.commit()

        assert mod.apply_changes(db.connection(), reverse=True) == 2
        db.commit()
        assert _features(db, "lite") == stale["lite"]
        assert _features(db, "growth") == stale["growth"]
    finally:
        for tier, features in original.items():
            db.query(SubscriptionPlan).filter(SubscriptionPlan.tier == tier).update(
                {"features": features}, synchronize_session=False
            )
        db.commit()


# "<number> <label>" feature strings and the capability column each one describes.
_NUMERIC_FEATURES = {
    "knowledge sources": "max_knowledge_sources",
    "team members": "max_team_members",
    "team member": "max_team_members",
    "flows": "max_flows",
    "flow": "max_flows",
    "automation actions": "max_automation_actions",
    "automation executions/month": "max_automation_executions_per_month",
    "conversations/month": "max_conversations_per_month",
    "MB storage": "max_knowledge_mb",
}
_PATTERN = re.compile(r"^([\d,]+)\s?(.+)$")


def test_seeded_feature_strings_match_capability_columns(db_session):
    db = db_session  # Postgres, seeded from scripts/seed_data.py
    """Every "<n> <thing>" string on every plan equals the column it names."""
    plans = db.query(SubscriptionPlan).all()
    assert plans, "conftest seeds subscription plans from scripts/seed_data.py"
    checked = 0
    for plan in plans:
        for feature in plan.features:
            match = _PATTERN.match(feature)
            if not match or match.group(2) not in _NUMERIC_FEATURES:
                continue
            column = _NUMERIC_FEATURES[match.group(2)]
            stated = int(match.group(1).replace(",", ""))
            assert stated == getattr(plan, column), (
                f"{plan.tier}: feature {feature!r} disagrees with {column}="
                f"{getattr(plan, column)}"
            )
            checked += 1
    assert checked >= 20  # 4 priced tiers x several numeric strings each
