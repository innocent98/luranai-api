"""Referral reward maintenance (arq crons).

``referral_promote_pending`` runs daily at 03:00 UTC; ``referral_prepare_batch`` runs on the 1st
and 15th at 04:00 UTC and only PREPARES a draft payout batch (nothing is sent until a super admin
approves it).

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
from sqlalchemy import func, select

from app.db.models.referral import PayoutItem
from app.db.session import SessionLocal
from app.services.referral.payouts import prepare_batch
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


async def referral_prepare_batch(ctx: dict) -> dict[str, Any] | None:
    """Group payable rewards into a draft payout batch (1st and 15th, 04:00 UTC).

    Idempotent: ``prepare_batch`` returns ``None`` when no reward is payable, so a second run
    (or an overlapping manual one) creates nothing. Returns the batch id and totals, or ``None``.
    Only ids and counts are logged.
    """
    db = SessionLocal()
    now = datetime.now(UTC)
    try:
        batch = prepare_batch(db, now)
        if batch is None:
            logger.info("referral payout batch: nothing payable")
            return None
        result = {
            "batch_id": str(batch.id),
            "total_kobo": batch.total_kobo,
            "items": db.scalar(
                select(func.count()).select_from(PayoutItem).where(PayoutItem.batch_id == batch.id)
            ),
        }
        db.commit()
        logger.info(
            f"referral payout batch {result['batch_id']} prepared: "
            f"{result['items']} items, {result['total_kobo']} kobo"
        )
        return result
    except Exception as exc:
        db.rollback()
        logger.error(f"referral payout batch preparation failed: {type(exc).__name__}")
        raise
    finally:
        db.close()
