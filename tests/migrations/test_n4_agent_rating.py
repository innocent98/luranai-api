"""n4agentrating data steps: source backfill CASE, idempotent comment encryption, key guard."""

import importlib.util
import os
import sqlite3

import pytest
from sqlalchemy import text

from app.core.encryption import decrypt_field
from tests.rating.conftest import make_rating, make_tenant, make_ticket

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "n4_agent_rating.py",
)


def _mod():
    spec = importlib.util.spec_from_file_location("n4_agent_rating", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    mod = _mod()
    assert (mod.revision, mod.down_revision) == ("n4agentrating", "sr10screenrec")


def test_check_constants_match_models():
    from app.db.models.review import RATING_REQUEST_SOURCE_CHECK, SESSION_RATING_SOURCE_CHECK

    mod = _mod()
    assert mod.SESSION_RATING_SOURCE_CHECK == SESSION_RATING_SOURCE_CHECK
    assert mod.RATING_REQUEST_SOURCE_CHECK == RATING_REQUEST_SOURCE_CHECK


def test_backfill_sql_only_touches_null_source():
    sql = " ".join(_mod().BACKFILL_SOURCE_SQL.split())
    assert sql.startswith("UPDATE session_ratings SET source = CASE ")
    assert sql.endswith(" WHERE source IS NULL")


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 38, 0), reason="->> needs SQLite 3.38+")
@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"source": "agent_resolved"}, "resolution_prompt"),
        ({"source": "ticket_closed"}, "resolution_prompt"),
        ({"source": "idle_prompt"}, "idle_prompt"),
        ({"other": 1}, "idle_prompt"),
        (None, "idle_prompt"),
    ],
)
def test_source_case_maps_legacy_meta(db, meta, expected):
    tenant = make_tenant(db)
    t = make_ticket(db, tenant)
    r = make_rating(db, session_id=t.session_id, tenant_id=tenant.id, meta=meta)
    got = db.execute(
        text(f"SELECT {_mod().SOURCE_CASE_SQL} FROM session_ratings WHERE id = :id"),
        {"id": r.id.hex},
    ).scalar_one()
    assert got == expected


def _raw_comment(db, rating_id):
    return db.execute(
        text("SELECT comment FROM session_ratings WHERE id = :id"), {"id": rating_id.hex}
    ).scalar_one()


def test_encrypt_existing_comments_is_idempotent_and_reversible(db):
    mod = _mod()
    tenant = make_tenant(db)
    plain = make_rating(
        db, session_id=make_ticket(db, tenant).session_id, tenant_id=tenant.id, comment="x"
    )
    already = make_rating(
        db,
        session_id=make_ticket(db, tenant).session_id,
        tenant_id=tenant.id,
        comment="already encrypted",
    )
    # Simulate a pre-N4 row: plaintext straight into the column, bypassing the TypeDecorator.
    db.execute(
        text("UPDATE session_ratings SET comment = :c WHERE id = :id"),
        {"c": "legacy plain comment", "id": plain.id.hex},
    )
    db.commit()
    before_already = _raw_comment(db, already.id)

    assert mod.encrypt_existing_comments(db.connection()) == 1
    assert decrypt_field(_raw_comment(db, plain.id)) == "legacy plain comment"
    assert _raw_comment(db, already.id) == before_already

    assert mod.encrypt_existing_comments(db.connection()) == 0  # idempotent

    assert mod.decrypt_existing_comments(db.connection()) == 2
    assert _raw_comment(db, plain.id) == "legacy plain comment"
    assert _raw_comment(db, already.id) == "already encrypted"


def test_migration_refuses_to_run_without_key(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        _mod()._require_encryption_key()


def test_duplicate_guard_sql_counts_sessions_with_several_ratings():
    sql = " ".join(_mod().DUPLICATE_SESSIONS_SQL.split())
    assert "GROUP BY session_id HAVING count(*) > 1" in sql
