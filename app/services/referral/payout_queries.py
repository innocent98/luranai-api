"""Read models for the platform-admin payout screens.

Pure reads (no commit, no locks). Rows are loaded with ``populate_existing`` so a session that
already holds an older copy of a batch (the payout functions commit step by step) never serves
stale state. Bank details never leave this module: ``bank_last4`` is the only trace of them.
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.referral import (
    Marketer,
    PayoutBatch,
    PayoutItem,
    PayoutItemStatus,
    ReferralReward,
    RewardStatus,
)
from app.schemas.referral import (
    PayoutBatchDetail,
    PayoutBatchListResponse,
    PayoutBatchSummary,
    PayoutItemCounts,
    PayoutItemResponse,
)

_FRESH = {"populate_existing": True}


def _counts(db: Session, batch_ids: list[UUID]) -> dict[UUID, PayoutItemCounts]:
    counts: dict[UUID, dict[str, int]] = {batch_id: {} for batch_id in batch_ids}
    if batch_ids:
        rows = db.execute(
            select(PayoutItem.batch_id, PayoutItem.status, func.count())
            .where(PayoutItem.batch_id.in_(batch_ids))
            .group_by(PayoutItem.batch_id, PayoutItem.status)
        ).all()
        for batch_id, status, number in rows:
            counts[batch_id][status.value] = number
    return {batch_id: PayoutItemCounts(**by_status) for batch_id, by_status in counts.items()}


def _summary(batch: PayoutBatch, counts: PayoutItemCounts) -> dict:
    return {
        "id": batch.id,
        "period_label": batch.period_label,
        "status": batch.status,
        "total_kobo": batch.total_kobo,
        "item_counts": counts,
        "approved_at": batch.approved_at,
        "approved_by_admin_id": batch.approved_by_admin_id,
        "created_at": batch.created_at,
    }


def list_batches(db: Session, limit: int, offset: int) -> PayoutBatchListResponse:
    """Batches newest first (id breaks ties so pages are stable)."""
    total = db.scalar(select(func.count()).select_from(PayoutBatch)) or 0
    batches = list(
        db.scalars(
            select(PayoutBatch)
            .order_by(PayoutBatch.created_at.desc(), PayoutBatch.id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(**_FRESH)
        )
    )
    counts = _counts(db, [b.id for b in batches])
    return PayoutBatchListResponse(
        items=[PayoutBatchSummary(**_summary(b, counts[b.id])) for b in batches],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_batch_detail(db: Session, batch_id: UUID) -> PayoutBatchDetail | None:
    batch = db.scalar(
        select(PayoutBatch).where(PayoutBatch.id == batch_id).execution_options(**_FRESH)
    )
    if batch is None:
        return None
    rows = db.execute(
        select(PayoutItem, Marketer)
        .join(Marketer, Marketer.id == PayoutItem.marketer_id)
        .where(PayoutItem.batch_id == batch_id)
        .order_by(Marketer.name, PayoutItem.id)
        .execution_options(**_FRESH)
    ).all()

    marketer_ids = [item.marketer_id for item, _ in rows]
    held: dict[UUID, int] = {}
    if marketer_ids:
        held = dict(
            db.execute(
                select(ReferralReward.marketer_id, func.count())
                .where(
                    ReferralReward.marketer_id.in_(marketer_ids),
                    ReferralReward.status == RewardStatus.HELD,
                    or_(
                        ReferralReward.payout_batch_id.is_(None),
                        ReferralReward.payout_batch_id != batch_id,
                    ),
                )
                .group_by(ReferralReward.marketer_id)
            ).all()
        )

    items = [
        PayoutItemResponse(
            id=item.id,
            marketer_id=marketer.id,
            marketer_name=marketer.name,
            marketer_code=marketer.code,
            amount_kobo=item.amount_kobo,
            status=item.status,
            failure_reason=item.failure_reason,
            provider_status=item.provider_status,
            attempt=item.attempt,
            transfer_reference=item.transfer_reference,
            reward_count=len(item.reward_ids or []),
            reward_ids=[UUID(str(r)) for r in (item.reward_ids or [])],
            bank_last4=marketer.account_number[-4:] if marketer.account_number else None,
            held_rewards_excluded=held.get(marketer.id, 0),
        )
        for item, marketer in rows
    ]
    counts = PayoutItemCounts(**_status_counts([item.status for item, _ in rows]))
    return PayoutBatchDetail(**_summary(batch, counts), items=items)


def _status_counts(statuses: list[PayoutItemStatus]) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in statuses:
        out[status.value] = out.get(status.value, 0) + 1
    return out
