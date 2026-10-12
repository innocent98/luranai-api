"""sr10 backfill: existing rows without expires_at get rollout + 30 d."""

import importlib.util
import os
from datetime import UTC, datetime, timedelta

_MIGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "sr10_screen_recording.py",
)


def _mod():
    spec = importlib.util.spec_from_file_location("sr10_screen_recording", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_sql_only_fills_missing_values():
    sql = " ".join(_mod().BACKFILL_SQL.split())
    assert sql == (
        "UPDATE message_attachments SET expires_at = :expires_at WHERE expires_at IS NULL"
    )


def test_grace_is_30_days_from_now():
    mod = _mod()
    before = datetime.now(UTC)
    got = mod.backfill_expiry()
    assert before + timedelta(days=30) <= got <= datetime.now(UTC) + timedelta(days=30)
