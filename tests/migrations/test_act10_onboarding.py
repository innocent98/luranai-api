"""act10onboarding: one linear head, columns declared nullable (no table rewrite)."""

import os

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _script() -> ScriptDirectory:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_head_and_chain():
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1, heads
    rev = script.get_revision("act10onboarding")
    assert rev.down_revision == "wa10whatsapp"


def test_models_match_the_migration_columns():
    from app.db.models.knowledge import KnowledgeSource
    from app.db.models.tenant import Tenant

    for col in ("widget_first_seen_at", "widget_last_seen_at", "widget_last_seen_host"):
        assert Tenant.__table__.c[col].nullable
        assert Tenant.__table__.c[col].server_default is None
    assert KnowledgeSource.__table__.c["failure_code"].nullable
    assert KnowledgeSource.__table__.c["failure_code"].type.length == 40
