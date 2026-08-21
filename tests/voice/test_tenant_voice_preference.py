from sqlalchemy import text

from app.db.models.tenant import TenantAIConfig
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_preference_orm_default_is_enabled_with_no_voice_chosen(db_session):
    """ORM default: a newly-constructed TenantAIConfig gets voice_enabled=True.

    Verifies the Python-side `default=True` on the model — what an ORM-created
    object receives through SQLAlchemy. SQLAlchemy resolves this default at
    flush time and writes it explicitly into the INSERT, so the database
    server_default never fires in this path. See the DDL tests below.

    Would fail if `voice_enabled` defaulted to False (voice granted but silently
    dead) or if `voice_id` defaulted to a string (a fake id reaching the mint).
    """
    tenant = _tenant(db_session)
    config = TenantAIConfig(tenant_id=tenant.id)
    db_session.add(config)
    db_session.flush()
    db_session.refresh(config)

    assert config.voice_enabled is True
    assert config.voice_id is None


def test_ddl_default_backfills_existing_rows_to_enabled(db_session):
    """Assert the ACTUAL column default in the database, whatever built it.

    This is the only check that covers the property the spec cares about --
    that a migration backfills pre-existing rows to enabled rather than NULL.
    It works in both environments: locally the table comes from the migration,
    in CI it comes from `create_all` off the model. Either way this reads what
    Postgres really has.

    The ORM test above cannot do this: SQLAlchemy resolves the Python-side
    `default=True` at flush and writes it into the INSERT, so `server_default`
    never fires. Nor can a Core insert — `pg_engine`'s own docstring notes
    `create_all` is a no-op against the migration-managed local DB, so a
    changed model default never reaches the existing table.
    """
    row = db_session.execute(
        text(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'tenant_ai_configs' AND column_name = 'voice_enabled'"
        )
    ).first()

    assert row is not None, "voice_enabled column is missing from tenant_ai_configs"
    assert row[0] == "true", f"expected DDL default 'true', got {row[0]!r}"
    assert row[1] == "NO", "voice_enabled must be NOT NULL"


def test_voice_id_ddl_is_nullable_with_no_default(db_session):
    """`voice_id` NULL means 'platform default' -- a DDL default would silently
    pin every tenant to one voice.
    """
    row = db_session.execute(
        text(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'tenant_ai_configs' AND column_name = 'voice_id'"
        )
    ).first()

    assert row is not None
    assert row[0] is None, f"voice_id must have no DDL default, got {row[0]!r}"
    assert row[1] == "YES", "voice_id must be nullable"
