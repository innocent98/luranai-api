"""Daily referral reward maintenance (arq cron, 03:00 UTC).

Two idempotent steps, each in its own transaction so one failing never blocks the other:

* ``promote_pending``: ``pending`` rewards past their hold date, for active marketers, become
  ``payable``.
* ``reconcile_missing_rewards``: creates the reward for any attributed tenant that paid but has
  none. ``charge.success`` creates the reward behind a savepoint and never lets a failure reach the
  payment, and a replayed webhook is a no-op, so this job is what repairs a swallowed failure.
"""

from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.db.session import SessionLocal
from app.services.referral.rewards import promote_pending, reconcile_missing_rewards


async def referral_promote_pending(ctx: dict) -> dict[str, Any]:
    """Promote due rewards and repair missing ones; returns the counts (and failed step count)."""
    db = SessionLocal()
    now = datetime.now(UTC)
    result: dict[str, Any] = {"promoted": 0, "reconciled": 0, "errors": 0}
    try:
        for key, step in (("promoted", promote_pending), ("reconciled", reconcile_missing_rewards)):
            try:
                result[key] = step(db, now)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - keep the other step running
                db.rollback()
                result["errors"] += 1
                logger.error(f"referral job step {key} failed: {type(exc).__name__}")
        logger.info(f"referral daily job: {result}")
        return result
    finally:
        db.close()
