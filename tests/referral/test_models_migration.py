"""Referral program schema: models, constraints, encryption at rest and the ref10 migration."""

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models.referral import (
    Marketer,
    PayoutBatch,
    PayoutItem,
    PayoutItemStatus,
    ReferralConfig,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.services.referral.config import ReferralRuntimeConfig, get_referral_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://luran_user:luran_password_dev@localhost:5432/luran_db"
)
REFERRAL_TABLES = {
    "marketers",
    "referral_attributions",
    "referral_rewards",
    "payout_batches",
    "payout_items",
    "signup_origins",
    "referral_config",
}
REFERRAL_ENUMS = {
    "marketer_status",
    "attribution_source",
    "reward_status",
    "batch_status",
    "payout_item_status",
}


def _tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Referral Test Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    return tenant


def _marketer(db, code: str = "ada-sales", email: str | None = None) -> Marketer:
    marketer = Marketer(
        name="Ada Marketer",
        email=email or f"ada_{uuid4().hex[:6]}@example.com",
        password_hash="$2b$12$placeholder_hash_not_real",
        code=code,
        bank_code="000",
        account_number="0000000000",
        account_name="Ada Placeholder",
    )
    db.add(marketer)
    db.flush()
    return marketer


def _reward(db, tenant, marketer) -> ReferralReward:
    now = datetime.now(UTC)
    reward = ReferralReward(
        tenant_id=tenant.id,
        marketer_id=marketer.id,
        amount_kobo=500_000,
        converted_at=now,
        payable_after=now + timedelta(days=7),
    )
    db.add(reward)
    db.flush()
    return reward


def test_second_reward_for_same_tenant_rejected(db_session):
    tenant = _tenant(db_session)
    marketer = _marketer(db_session, code=f"m-{uuid4().hex[:8]}")
    first = _reward(db_session, tenant, marketer)
    assert first.status == RewardStatus.PENDING
    assert first.review_reasons == []
    with pytest.raises(IntegrityError):
        _reward(db_session, tenant, marketer)


def test_tenant_delete_keeps_reward_and_cascades_attribution_and_origin(db_session):
    from app.db.models.referral import ReferralAttribution, SignupOrigin

    tenant = _tenant(db_session)
    marketer = _marketer(db_session, code=f"del-{uuid4().hex[:8]}")
    reward = _reward(db_session, tenant, marketer)
    db_session.add(
        ReferralAttribution(tenant_id=tenant.id, marketer_id=marketer.id, source="cookie")
    )
    db_session.add(SignupOrigin(tenant_id=tenant.id, origin_hash="0" * 64))
    db_session.flush()
    reward_id = reward.id

    db_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
    db_session.expire_all()
    kept = db_session.get(ReferralReward, reward_id)
    assert kept is not None and kept.tenant_id is None
    left = db_session.execute(
        text(
            "SELECT (SELECT count(*) FROM referral_attributions WHERE marketer_id = :m), "
            "(SELECT count(*) FROM signup_origins WHERE origin_hash = :h)"
        ),
        {"m": marketer.id, "h": "0" * 64},
    ).one()
    assert tuple(left) == (0, 0)


def test_code_unique_case_insensitive(db_session):
    stem = uuid4().hex[:8]
    _marketer(db_session, code=f"ada-{stem}")
    with pytest.raises(IntegrityError):
        _marketer(db_session, code=f"ADA-{stem}")


def test_code_unique_case_insensitive_on_sqlite(db):
    """The functional index must also work on the in-memory SQLite fixture."""
    _marketer(db, code="sqlite-code")
    with pytest.raises(IntegrityError):
        _marketer(db, code="SQLITE-Code")


def test_email_hash_set_by_listener(db):
    marketer = _marketer(db, code="hash-code", email="hash.me@example.com")
    from app.core.encryption import hash_for_lookup

    assert marketer.email_hash == hash_for_lookup("hash.me@example.com")


def test_bank_fields_encrypted_at_rest(db_session):
    marketer = _marketer(db_session, code=f"enc-{uuid4().hex[:8]}", email="enc@example.com")
    row = db_session.execute(
        text("SELECT email, bank_code, account_number, account_name FROM marketers WHERE id = :id"),
        {"id": marketer.id},
    ).one()
    assert row.account_number != "0000000000"
    assert row.account_name != "Ada Placeholder"
    assert row.bank_code != "000"
    assert row.email != "enc@example.com"
    assert "0000000000" not in row.account_number
    db_session.expire_all()
    assert marketer.account_number == "0000000000"
    assert marketer.account_name == "Ada Placeholder"


def test_payout_item_unique_per_batch_and_marketer(db_session):
    marketer = _marketer(db_session, code=f"pi-{uuid4().hex[:8]}")
    batch = PayoutBatch(period_label="2026-11-01")
    db_session.add(batch)
    db_session.flush()
    item = PayoutItem(batch_id=batch.id, marketer_id=marketer.id, amount_kobo=500_000)
    db_session.add(item)
    db_session.flush()
    assert item.status == PayoutItemStatus.PENDING
    db_session.add(PayoutItem(batch_id=batch.id, marketer_id=marketer.id, amount_kobo=1))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_config_falls_back_to_env_then_db_override_wins(db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "REFERRAL_REWARD_KOBO", 500_000)
    monkeypatch.setattr(settings, "REFERRAL_HOLD_DAYS", 7)
    monkeypatch.setattr(settings, "REFERRAL_COMMISSION_WINDOW_DAYS", 90)
    assert get_referral_config(db) == ReferralRuntimeConfig(
        reward_kobo=500_000, hold_days=7, commission_window_days=90
    )

    db.add(ReferralConfig(key="reward_kobo", value=750_000))
    db.add(ReferralConfig(key="hold_days", value=3))
    db.flush()
    cfg = get_referral_config(db)
    assert cfg.reward_kobo == 750_000
    assert cfg.hold_days == 3
    assert cfg.commission_window_days == 90  # no override row: env value


def test_settings_defaults():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["REFERRAL_REWARD_KOBO"].default == 500_000
    assert fields["REFERRAL_HOLD_DAYS"].default == 7
    assert fields["REFERRAL_COMMISSION_WINDOW_DAYS"].default == 90
    assert fields["REFERRAL_COOKIE_DAYS"].default == 30
    assert fields["REFERRAL_PAYOUT_PROVIDER"].default == "manual"
    assert fields["REFERRAL_LINK_BASE_URL"].default == "https://luranai.com"


# --- migration round trip on an isolated scratch database ------------------------------------


def _scratch_url() -> tuple[str, str, str]:
    parts = urlsplit(BASE_DATABASE_URL)
    base = parts.path.lstrip("/")
    name = f"{base}_referral_migration_test"
    maintenance = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    return maintenance, name, urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))


def _recreate(maintenance_url: str, name: str, create: bool) -> None:
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            if create:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def scratch_db():
    maintenance, name, url = _scratch_url()
    _recreate(maintenance, name, create=True)
    yield url
    _recreate(maintenance, name, create=False)


@pytest.fixture
def alembic_config(scratch_db, monkeypatch) -> Config:
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", scratch_db)
    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", scratch_db)
    return cfg


def _enum_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT typname FROM pg_type WHERE typname = ANY(:names)"),
            {"names": sorted(REFERRAL_ENUMS)},
        ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_creates_tables_and_downgrade_removes_them(alembic_config, scratch_db):
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == ["ref10referral"]
    assert script.get_revision("ref10referral").down_revision == "wa20ratingreply"

    engine = create_engine(scratch_db)
    try:
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        assert REFERRAL_TABLES <= set(inspector.get_table_names())
        assert _enum_names(engine) == REFERRAL_ENUMS
        index_names = {i["name"] for i in inspector.get_indexes("marketers")}
        assert "uq_marketers_code_lower" in index_names
        reward_fks = {
            fk["constrained_columns"][0]: fk["options"].get("ondelete")
            for fk in inspector.get_foreign_keys("referral_rewards")
        }
        assert reward_fks["tenant_id"] == "SET NULL"
        assert reward_fks["marketer_id"] == "RESTRICT"

        command.downgrade(alembic_config, "wa20ratingreply")
        inspector = inspect(engine)
        assert REFERRAL_TABLES.isdisjoint(set(inspector.get_table_names()))
        assert _enum_names(engine) == set()

        command.upgrade(alembic_config, "head")  # forward path re-applies cleanly
        assert REFERRAL_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
