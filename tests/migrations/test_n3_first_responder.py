"""N3 data step: tenants with zero ACTIVE knowledge sources -> 'human'."""

import importlib.util
import os

from sqlalchemy import text

from app.db.models.knowledge import SourceStatus
from app.db.models.tenant import TenantAIConfig
from tests.api.v1 import test_chat_image_send as img
from tests.services.test_first_responder import _source

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "n3_first_responder.py",
)


def _load_sql() -> str:
    spec = importlib.util.spec_from_file_location("n3_first_responder", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.N3_DATA_STEP_SQL


def _value(db, tenant_id):
    db.expire_all()
    return (
        db.query(TenantAIConfig.first_responder)
        .filter(TenantAIConfig.tenant_id == tenant_id)
        .scalar()
    )


def test_data_step_moves_only_zero_active_tenants_and_is_idempotent(db):
    _, no_kb = img._session(db)
    _, with_kb = img._session(db)
    _source(db, no_kb, SourceStatus.PROCESSING)
    _source(db, with_kb, SourceStatus.ACTIVE)
    db.query(TenantAIConfig).filter(TenantAIConfig.tenant_id.in_([no_kb, with_kb])).update(
        {"first_responder": "ai"}, synchronize_session=False
    )
    db.commit()

    sql = text(_load_sql())
    db.execute(sql)
    db.commit()
    assert _value(db, no_kb) == "human"
    assert _value(db, with_kb) == "ai"

    again = db.execute(sql)
    db.commit()
    assert again.rowcount == 0
    assert _value(db, no_kb) == "human"
    assert _value(db, with_kb) == "ai"
