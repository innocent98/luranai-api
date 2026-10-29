"""wa40: the exclusion table is additive, matches the model, and reverses cleanly (real Postgres)."""

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy import inspect

from app.db.base import Base
from tests.migrations.test_wa10_whatsapp_channel import (  # noqa: F401  (fixtures + helper)
    _cfg,
    keep_logging,
    scratch_db,
)

TABLE = "whatsapp_contact_exclusions"


def test_single_head_and_wa40_sits_on_cl10():
    scripts = ScriptDirectory.from_config(_cfg())
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("wa40contactexclusion").down_revision == "cl10conversationlimits"


def test_upgrade_matches_the_model_and_downgrade_reverses(scratch_db):
    cfg = _cfg()
    command.upgrade(cfg, "head")
    insp = inspect(scratch_db)
    model = Base.metadata.tables[TABLE]
    cols = {c["name"]: c for c in insp.get_columns(TABLE)}
    assert set(cols) == set(model.columns.keys())
    for name, col in model.columns.items():
        assert cols[name]["nullable"] == col.nullable, f"{TABLE}.{name} nullability drifted"
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(TABLE)}
    assert ("connection_id", "phone_hmac") in uniques
    assert ("connection_id", "lid_hmac") in {
        tuple(i["column_names"]) for i in insp.get_indexes(TABLE)
    }
    fks = {
        (fk["referred_table"], fk["options"].get("ondelete")) for fk in insp.get_foreign_keys(TABLE)
    }
    assert fks == {("tenants", "CASCADE"), ("whatsapp_connections", "CASCADE")}

    chat_cols = {c["name"]: c for c in insp.get_columns("whatsapp_chats")}
    assert chat_cols["phone_hmac"]["nullable"] is True
    assert "ix_whatsapp_chats_connection_phone" in {
        i["name"] for i in insp.get_indexes("whatsapp_chats")
    }

    command.downgrade(cfg, "cl10conversationlimits")  # exactly one step back
    assert not inspect(scratch_db).has_table(TABLE)
    assert "phone_hmac" not in {
        c["name"] for c in inspect(scratch_db).get_columns("whatsapp_chats")
    }
    assert inspect(scratch_db).has_table("whatsapp_connections")  # nothing else was touched

    command.upgrade(cfg, "head")  # and it comes back cleanly
    assert inspect(scratch_db).has_table(TABLE)


def test_upgrade_is_idempotent_over_tables_built_from_the_models(scratch_db):
    """Environments and tests that create_all() first, then run alembic from an older revision,
    must not hit DuplicateColumn / DuplicateTable; a second run and a double downgrade are no-ops.
    """
    cfg = _cfg()
    command.upgrade(cfg, "cl10conversationlimits")
    # What a create_all-built database looks like at that revision: the new table and column exist.
    Base.metadata.create_all(bind=scratch_db)
    with scratch_db.begin() as conn:
        conn.execute(text("ALTER TABLE whatsapp_chats ADD COLUMN phone_hmac VARCHAR(64)"))
        conn.execute(
            text(
                "CREATE INDEX ix_whatsapp_chats_connection_phone "
                "ON whatsapp_chats (connection_id, phone_hmac)"
            )
        )
    insp = inspect(scratch_db)
    assert insp.has_table(TABLE)
    assert "phone_hmac" in {c["name"] for c in insp.get_columns("whatsapp_chats")}

    command.upgrade(cfg, "head")  # would raise DuplicateColumn if not idempotent
    command.stamp(cfg, "cl10conversationlimits")
    command.upgrade(cfg, "head")  # and again

    command.downgrade(cfg, "cl10conversationlimits")
    command.stamp(cfg, "wa40contactexclusion")
    command.downgrade(cfg, "cl10conversationlimits")  # nothing left to drop: still fine
    insp = inspect(scratch_db)
    assert not insp.has_table(TABLE)
    assert "phone_hmac" not in {c["name"] for c in insp.get_columns("whatsapp_chats")}
