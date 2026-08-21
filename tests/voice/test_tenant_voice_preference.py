from sqlalchemy import insert

from app.db.models.tenant import TenantAIConfig
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_preference_orm_default_is_enabled_with_no_voice_chosen(db_session):
    """ORM default: granting a tenant voice must make it work immediately.

    Verifies the Python-side `default=True` on the model. SQLAlchemy resolves
    this default at flush time and writes the value explicitly into the INSERT,
    so the database server_default does NOT fire in this path — it is exercised
    only when a column is omitted from an INSERT entirely (see the Core test
    below).

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


def test_server_default_backfills_enabled_when_column_is_omitted(db_session):
    """Database server_default: existing rows must backfill to enabled.

    Core INSERT omitting voice_enabled — the only path where the DDL
    server_default is actually applied. Migration backfills of pre-existing
    rows use this shape (omitted column, so the DB default supplies the value).
    The ORM test above cannot detect a missing or wrong server_default, because
    the model's Python-side default satisfies its assertions. This test can.

    Would fail if `server_default` was missing, wrong, or if `voice_id`
    carried a non-null default (a fake id reaching the mint).
    """
    tenant = _tenant(db_session)
    db_session.execute(
        insert(TenantAIConfig.__table__).values(tenant_id=tenant.id)
    )
    db_session.flush()
    row = (
        db_session.query(TenantAIConfig)
        .filter(TenantAIConfig.tenant_id == tenant.id)
        .one()
    )

    assert row.voice_enabled is True
    assert row.voice_id is None
