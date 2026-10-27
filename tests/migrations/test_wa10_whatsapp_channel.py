"""wa10whatsapp: single alembic head, upgrade/downgrade on a scratch Postgres DB, model parity.

CI never runs alembic and the test suite builds schemas with `create_all`, so a model that
drifts from its migration would otherwise go unnoticed until deploy.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import Base
from app.db.models import whatsapp  # noqa: F401  (registers the tables)

ROOT = Path(__file__).resolve().parents[2]
TABLES = ("whatsapp_connections", "whatsapp_chats", "whatsapp_inbound_messages")


def _cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_single_alembic_head_and_wa10_sits_on_the_previous_head():
    scripts = ScriptDirectory.from_config(_cfg())
    assert len(scripts.get_heads()) == 1
    wa10 = scripts.get_revision("wa10whatsapp")
    assert wa10.down_revision == "pf10planfeatures"
    assert "wa10whatsapp" in [r.revision for r in scripts.walk_revisions()]


@pytest.fixture
def keep_logging():
    """alembic/env.py calls logging.config.fileConfig, which would disable other loggers."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    disabled = {
        n: lg.disabled
        for n, lg in logging.Logger.manager.loggerDict.items()
        if hasattr(lg, "disabled")
    }
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, flag in disabled.items():
        logging.Logger.manager.loggerDict[name].disabled = flag


@pytest.fixture
def scratch_db(monkeypatch, keep_logging):
    base = make_url(settings.DATABASE_URL)
    if not base.drivername.startswith("postgresql"):
        pytest.skip("needs Postgres")
    name = f"wa_mig_{uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:  # noqa: BLE001
        pytest.skip("cannot create a scratch database")
    url = base.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setattr(settings, "DATABASE_URL", url)  # alembic/env.py reads this
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_matches_models_and_downgrade_reverses(scratch_db):
    assert os.environ.get("ENCRYPTION_KEY")  # n4 migration needs it (set by tests/conftest.py)
    cfg = _cfg()
    command.upgrade(cfg, "head")
    insp = inspect(scratch_db)

    for table in TABLES:
        model = Base.metadata.tables[table]
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert set(cols) == set(model.columns.keys()), table
        for name, col in model.columns.items():
            assert cols[name]["nullable"] == col.nullable, f"{table}.{name} nullability drifted"

    uniques = {
        tuple(u["column_names"])
        for t in TABLES
        for u in inspect(scratch_db).get_unique_constraints(t)
    }
    assert ("phone_hmac",) in uniques and ("session_key",) in uniques
    assert ("connection_id", "contact_hmac") in uniques
    assert ("connection_id", "provider_message_id") in uniques  # the dedupe key

    caps = {c["name"]: c for c in insp.get_columns("tenant_capabilities")}
    assert caps["can_use_whatsapp"]["nullable"] is False
    assert caps["max_whatsapp_numbers"]["nullable"] is False
    with scratch_db.connect() as conn:
        labels = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'visitor_source'"
                )
            )
        }
    assert "whatsapp" in labels

    # Back to wa10's own parent (not "-1": later migrations may sit on top of wa10).
    command.downgrade(cfg, "pf10planfeatures")
    insp = inspect(scratch_db)
    assert not [t for t in TABLES if insp.has_table(t)]
    assert "can_use_whatsapp" not in {c["name"] for c in insp.get_columns("tenant_capabilities")}

    command.upgrade(cfg, "head")  # and it comes back cleanly
    assert inspect(scratch_db).has_table("whatsapp_connections")
