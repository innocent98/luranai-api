"""Migration round-trip test for the account-deletion schema (Task 1).

Runs the REAL alembic upgrade/downgrade path — not `Base.metadata.create_all`
— against an isolated, throwaway Postgres database (never the shared
`PG_DATABASE_URL` database other tests use), because this test performs
destructive DDL (upgrade -> downgrade -> upgrade) that must never touch a
database other tests, CI jobs, or developers depend on.

Verifies:
  * A fresh DB brought to `head` creates the `deletion_status` /
    `deletion_scope` / `deletion_requested_via` enum types, the
    `deletion_requests` table, and the two new `users` columns.
  * `downgrade -1` removes all of the above cleanly.
  * `upgrade head` re-applies cleanly (idempotent forward path).
  * The migration graph has a single head throughout.
  * The Postgres enum labels are byte-for-byte the same strings
    `values_callable=lambda x: [e.value for e in x]` emits for each Python
    enum — this is the exact casing invariant that broke inserts before
    (see migration 3a83f8e6d080 / fix d1f2humanagentname) — and a real
    ORM insert + read-back round-trips every member of every new enum.
"""

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://luran_user:luran_password_dev@localhost:5432/luran_db",
)


def _split_db_url(url: str) -> tuple[str, str]:
    """Return (url_without_dbname_pointed_at_postgres_maintenance_db, dbname)."""
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    maintenance = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    return maintenance, dbname


def _migration_test_url() -> tuple[str, str, str]:
    """Build an isolated test-db URL sibling to BASE_DATABASE_URL.

    Returns (maintenance_url, test_db_name, test_db_url).
    """
    maintenance_url, base_dbname = _split_db_url(BASE_DATABASE_URL)
    test_dbname = f"{base_dbname}_deletion_migration_test"
    parts = urlsplit(BASE_DATABASE_URL)
    test_url = urlunsplit((parts.scheme, parts.netloc, f"/{test_dbname}", "", ""))
    return maintenance_url, test_dbname, test_url


@pytest.fixture(scope="module")
def migration_test_db():
    """Create a fresh, empty database for this test module; drop it afterwards."""
    maintenance_url, test_dbname, test_url = _migration_test_url()
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": test_dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_dbname}"'))
            conn.execute(text(f'CREATE DATABASE "{test_dbname}"'))
    finally:
        maintenance_engine.dispose()

    yield test_url

    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": test_dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_dbname}"'))
    finally:
        maintenance_engine.dispose()


@pytest.fixture
def alembic_config(migration_test_db, monkeypatch) -> Config:
    # alembic/env.py sets `sqlalchemy.url` from `app.core.config.settings.DATABASE_URL`
    # unconditionally (overriding whatever Config we pass in), so the app's
    # settings singleton must point at the isolated test DB while this
    # fixture is in effect — never at the real DATABASE_URL other tests use.
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", migration_test_db)

    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", migration_test_db)
    return cfg


def _enum_labels(engine, type_name: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = :type_name "
                "ORDER BY e.enumsortorder"
            ),
            {"type_name": type_name},
        ).fetchall()
    return [r[0] for r in rows]


def test_migration_round_trip(alembic_config: Config, migration_test_db: str):
    script = ScriptDirectory.from_config(alembic_config)

    # --- single head, before touching anything -----------------------------
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single alembic head, found {heads}"
    assert heads[0] == "ad40accountdeletion"

    engine = create_engine(migration_test_db)

    # --- fresh DB -> head ----------------------------------------------------
    command.upgrade(alembic_config, "head")

    inspector = inspect(engine)
    assert "deletion_requests" in inspector.get_table_names()

    users_columns = {c["name"] for c in inspector.get_columns("users")}
    assert "deletion_status" in users_columns
    assert "deletion_requested_at" in users_columns

    deletion_request_columns = {c["name"]: c for c in inspector.get_columns("deletion_requests")}
    for col in (
        "id",
        "user_id",
        "tenant_id",
        "scope",
        "status",
        "verification_token_hash",
        "cancel_token_hash",
        "requested_via",
        "scheduled_purge_at",
        "created_at",
        "updated_at",
    ):
        assert col in deletion_request_columns, f"missing column {col}"

    fk_tables = {fk["referred_table"] for fk in inspector.get_foreign_keys("deletion_requests")}
    assert fk_tables == {"users", "tenants"}

    # --- enum casing: DB labels must equal what values_callable emits -------
    from app.db.models.deletion_request import DeletionRequestedVia, DeletionScope
    from app.db.models.user import DeletionStatus

    assert _enum_labels(engine, "deletion_status") == [m.value for m in DeletionStatus]
    assert _enum_labels(engine, "deletion_scope") == [m.value for m in DeletionScope]
    assert _enum_labels(engine, "deletion_requested_via") == [m.value for m in DeletionRequestedVia]

    # --- ORM round-trip: insert + read back every enum member ---------------
    _assert_orm_round_trip(engine)

    # --- downgrade -1 removes everything this migration added ---------------
    command.downgrade(alembic_config, "-1")

    inspector = inspect(engine)
    assert "deletion_requests" not in inspector.get_table_names()
    users_columns_after_downgrade = {c["name"] for c in inspector.get_columns("users")}
    assert "deletion_status" not in users_columns_after_downgrade
    assert "deletion_requested_at" not in users_columns_after_downgrade

    with engine.connect() as conn:
        remaining_types = conn.execute(
            text(
                "SELECT typname FROM pg_type WHERE typname IN "
                "('deletion_status', 'deletion_scope', 'deletion_requested_via')"
            )
        ).fetchall()
    assert remaining_types == []

    # --- upgrade head again succeeds (idempotent forward path) --------------
    command.upgrade(alembic_config, "head")
    inspector = inspect(engine)
    assert "deletion_requests" in inspector.get_table_names()

    engine.dispose()


def _assert_orm_round_trip(engine) -> None:
    """Insert a Tenant/User/DeletionRequest through the real ORM models and
    read every enum member back, proving the mapped `values_callable` Enum
    columns agree with the Postgres enum labels this migration created.
    """
    from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
    from app.db.models.tenant import Tenant
    from app.db.models.user import DeletionStatus, User, UserRole

    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        tenant = Tenant(
            name="deletion-migration-test",
            display_name="Deletion Migration Test",
            api_key_hash="not-a-real-hash",
        )
        session.add(tenant)
        session.flush()

        user = User(
            tenant_id=tenant.id,
            email="deletion-migration-test@example.com",
            hashed_password="not-a-real-hash",
            full_name="Deletion Migration Test",
            role=UserRole.TENANT_ADMIN,
        )
        session.add(user)
        session.flush()

        assert user.deletion_status == DeletionStatus.ACTIVE  # column default

        for status in DeletionStatus:
            user.deletion_status = status
            session.flush()

        for scope in DeletionScope:
            for requested_via in DeletionRequestedVia:
                for status in DeletionStatus:
                    session.add(
                        DeletionRequest(
                            user_id=user.id,
                            tenant_id=tenant.id,
                            scope=scope,
                            status=status,
                            requested_via=requested_via,
                            verification_token_hash="a" * 64,
                            cancel_token_hash="b" * 64,
                        )
                    )
        session.commit()
        user_id = user.id

    # Fresh session — force a real read from Postgres, not the identity map.
    with session_factory() as session:
        reloaded_user = session.get(User, user_id)
        assert reloaded_user.deletion_status == DeletionStatus.PURGED  # last value set above

        rows = session.query(DeletionRequest).filter_by(user_id=user_id).all()
        assert len(rows) == len(list(DeletionScope)) * len(list(DeletionRequestedVia)) * len(
            list(DeletionStatus)
        )
        seen_statuses = {row.status for row in rows}
        seen_scopes = {row.scope for row in rows}
        seen_requested_via = {row.requested_via for row in rows}
        assert seen_statuses == set(DeletionStatus)
        assert seen_scopes == set(DeletionScope)
        assert seen_requested_via == set(DeletionRequestedVia)
        for row in rows:
            assert row.verification_token_hash == "a" * 64
            assert row.cancel_token_hash == "b" * 64
