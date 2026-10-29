"""Referral payout batches: prepare, approve, pay item by item, retry safely, record manual payments.

MONEY-MOVEMENT CODE. The one rule: a transfer is never sent for an item unless the item has been
CLAIMED first, and a claim is only ever made when it is provably safe. Everything below follows
from that.

PayoutItem state machine (the only writers are the functions named on each edge)::

    pending ──claim──────────────► processing        _claim (approve_batch / retry_batch)
    pending ──suspended/no rewards► skipped          approve_batch (R8), _claim
    processing ──result success──► paid              _record_result, apply_transfer_event,
                                                      _apply_verified_success
    processing ──result pending──► processing        (stays; webhook or verify finalises it)
    processing ──result failed/otp► failed           _record_result (otp -> reason otp_required)
    processing ──result manual───► awaiting_manual   _record_result (ManualPayoutProvider)
    processing ──transfer.failed─► failed            apply_transfer_event
    failed ──transfer.success────► paid              apply_transfer_event / verify says success
    paid ──transfer.reversed─────► failed (reversed) apply_transfer_event; rewards paid -> batched
    awaiting_manual ──mark_paid──► paid              mark_paid (bank reference recorded)
    failed ──mark_paid───────────► paid              mark_paid, only if the transfer is verified
                                                      not successful
    failed|processing ──retry────► processing        retry_batch, only after verify_transfer says
                                                      not_found (same reference) or failed/reversed
                                                      (attempt + 1, new reference ``-r{attempt}``)

How a double or wrong payment is prevented:

* approve_batch locks the batch row and only acts on ``draft``; a second/concurrent call sees a
  non-draft batch and returns it without touching any provider.
* The claim (conditional UPDATE pending -> processing, rows-affected checked, committed) happens
  BEFORE the provider call, so two workers can never both call the provider for one item. The
  provider call runs with no row locks held; the result is recorded in a second short transaction.
* The reference is deterministic per (batch, marketer, attempt) and Paystack deduplicates on it.
* A crash between claim and result leaves ``processing``; only the verify-first retry resolves it.
* A retry NEVER sends before ``provider.verify`` has answered: success -> mark paid, pending ->
  leave alone, failed/reversed -> new attempt reference, not_found -> same reference (and only for
  a ``processing`` claim that has gone stale, so a live worker is never raced).
* The amount is computed at claim time from the rewards currently ``batched`` in this batch for
  the marketer (locked), and exactly those rewards are the ones marked ``paid``.
* A reward captured by an item that is not resolved (``processing``, ``awaiting_manual``,
  ``failed``) cannot silently leave it (R38, R41, R49): a dispute hold on such an item keeps the
  reward's batch link (so ``release``/``void`` refuse it); only a ``pending`` item (nothing can
  have been sent) clears the link and is refreshed. A re-claim recomputes the item from the
  rewards still ``batched`` and detaches held ones; ``mark_paid`` on a ``failed`` item does the
  same, while on an ``awaiting_manual`` item it accepts EITHER the claimed amount (the admin may
  already have paid it at the bank: the claimed rewards are paid, held ones as ``paid_in_flight``)
  OR the current batched sum (held ones are detached). ``_pay_rewards`` refuses a reward that is
  already paid, linked to another batch or in a disallowed status. One reward can therefore
  never be paid by two items.
* Lock order everywhere: batch -> marketer -> item -> rewards. The batch row is taken first
  (approve_batch, hold_for_dispute) or ALONE after everything else was committed
  (``_finalize_batch``, retry_batch); nothing ever waits for the batch while holding a marketer,
  item or reward lock, so no lock cycle can form.

Functions do their own commits (a payout must be durable step by step); callers just call them.
The approval audit is written through the ``after_approval`` hook, before any provider call.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.referral import (
    BatchStatus,
    Marketer,
    MarketerStatus,
    PayoutBatch,
    PayoutItem,
    PayoutItemStatus,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
)
from app.services.referral.notifications import (
    PayoutNotice,
    queue_payout_notice,
    send_payout_notices,
)
from app.services.referral.providers import (
    PayoutProvider,
    PayoutProviderError,
    TransferResult,
    TransferVerification,
    get_payout_provider,
    get_verifier,
)

MARKETER_SUSPENDED = "marketer_suspended"
NO_BATCHED_REWARDS = "no_batched_rewards"
NON_POSITIVE_AMOUNT = "non_positive_amount"
OTP_REQUIRED = "otp_required"
PAID_IN_FLIGHT = "paid_in_flight"
REVERSED = "reversed"
REVERSED_AFTER_HOLD = "payment_reversed_after_hold"
PAYMENT_DISPUTE = "payment_dispute"

# A `processing` item whose claim is younger than this may belong to a worker that is in the
# middle of its provider call, so a retry will not re-send it even when verify says not_found.
STALE_PROCESSING_AFTER = timedelta(minutes=15)

_PS = PayoutItemStatus
_UNRESOLVED = (_PS.PENDING, _PS.PROCESSING, _PS.AWAITING_MANUAL)


class PayoutError(Exception):
    """Base class: ``code`` is the stable machine-readable error string."""

    code = "payout_error"


class BatchNotFoundError(PayoutError):
    code = "batch_not_found"


class PayoutItemNotFoundError(PayoutError):
    code = "payout_item_not_found"


class PayoutStateError(PayoutError):
    """The batch or item is not in a state the requested action allows. Nothing was changed."""

    code = "payout_state_invalid"


class BatchStateError(PayoutStateError):
    """The batch is in a state that does not allow the action (e.g. retrying a draft)."""

    code = "batch_state_invalid"


class ItemNotMarkableError(PayoutStateError):
    """``mark_paid`` refused an item because of its state; carries the state for the caller."""

    code = "item_not_markable"

    def __init__(self, message: str, marketer_id: UUID, item_status: PayoutItemStatus):
        super().__init__(message)
        self.marketer_id = marketer_id
        self.item_status = item_status


class TransferUnverifiedError(PayoutStateError):
    """A failed item cannot be marked paid: its transfer was not confirmed dead at the provider."""

    code = "transfer_unverified"


class AmountMismatchError(PayoutStateError):
    """The amount the admin says they paid differs from what the item is worth right now."""

    code = "amount_mismatch"

    def __init__(
        self,
        message: str,
        marketer_id: UUID,
        current_amount_kobo: int,
        claimed_amount_kobo: int | None = None,
    ):
        super().__init__(message)
        self.marketer_id = marketer_id
        self.current_amount_kobo = current_amount_kobo
        self.claimed_amount_kobo = claimed_amount_kobo


class BankReferenceInUseError(PayoutStateError):
    """The bank reference is already recorded against another payment."""

    code = "bank_reference_in_use"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return _aware(a) == _aware(b)


def item_reference(batch_id: UUID, marketer_id: UUID, attempt: int = 1) -> str:
    """``ref-{batch}-{marketer}`` for the first attempt, ``...-r{attempt}`` for later ones."""
    base = f"ref-{batch_id}-{marketer_id}"
    return base if attempt <= 1 else f"{base}-r{attempt}"


# --------------------------------------------------------------------------- locking helpers


def _lock_batch(db: Session, batch_id: UUID) -> PayoutBatch | None:
    return db.scalar(
        select(PayoutBatch)
        .where(PayoutBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_marketer(db: Session, marketer_id: UUID) -> Marketer | None:
    return db.scalar(
        select(Marketer)
        .where(Marketer.id == marketer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_item(db: Session, item_id: UUID) -> PayoutItem | None:
    return db.scalar(
        select(PayoutItem)
        .where(PayoutItem.id == item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_batched_rewards(db: Session, batch_id: UUID, marketer_id: UUID) -> list[ReferralReward]:
    return list(
        db.scalars(
            select(ReferralReward)
            .where(
                ReferralReward.payout_batch_id == batch_id,
                ReferralReward.marketer_id == marketer_id,
                ReferralReward.status == RewardStatus.BATCHED,
            )
            .order_by(ReferralReward.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _lock_rewards_by_id(db: Session, item: PayoutItem) -> list[ReferralReward]:
    ids = [UUID(str(i)) for i in (item.reward_ids or [])]
    if not ids:
        return []
    return list(
        db.scalars(
            select(ReferralReward)
            .where(ReferralReward.id.in_(ids), ReferralReward.marketer_id == item.marketer_id)
            .order_by(ReferralReward.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _lock_claim_rewards(db: Session, item: PayoutItem) -> list[ReferralReward]:
    """Lock, in one id-ordered pass, the marketer's rewards currently ``batched`` in the item's
    batch plus the ones the item captured earlier (which may have been held since)."""
    ids = [UUID(str(i)) for i in (item.reward_ids or [])]
    in_batch = and_(
        ReferralReward.payout_batch_id == item.batch_id,
        ReferralReward.status == RewardStatus.BATCHED,
    )
    return list(
        db.scalars(
            select(ReferralReward)
            .where(
                ReferralReward.marketer_id == item.marketer_id,
                or_(in_batch, ReferralReward.id.in_(ids)) if ids else in_batch,
            )
            .order_by(ReferralReward.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _split_claim_rewards(
    item: PayoutItem, locked: list[ReferralReward]
) -> tuple[list[ReferralReward], list[ReferralReward]]:
    """(rewards to pay now, previously captured rewards that were held since)."""
    batched = [
        r for r in locked if r.status == RewardStatus.BATCHED and r.payout_batch_id == item.batch_id
    ]
    held = [
        r for r in locked if r.status == RewardStatus.HELD and r.payout_batch_id == item.batch_id
    ]
    return batched, held


def _detach(rewards: list[ReferralReward]) -> None:
    for reward in rewards:
        reward.payout_batch_id = None


def _hold_rewards(rewards: list[ReferralReward], reason: str) -> None:
    for reward in rewards:
        reasons = list(reward.review_reasons or [])
        if reason not in reasons:
            reasons.append(reason)
        reward.review_reasons = reasons  # new list: JSON columns do not track in-place edits
        reward.status = RewardStatus.HELD
        reward.payout_batch_id = None


def _refresh_total(db: Session, batch_id: UUID) -> None:
    """``total_kobo`` = sum of the batch's non-skipped items, as one statement."""
    total = (
        select(func.coalesce(func.sum(PayoutItem.amount_kobo), 0))
        .where(PayoutItem.batch_id == batch_id, PayoutItem.status != _PS.SKIPPED)
        .scalar_subquery()
    )
    db.execute(
        update(PayoutBatch)
        .where(PayoutBatch.id == batch_id)
        .values(total_kobo=total)
        .execution_options(synchronize_session=False)
    )


def _skip_item(
    db: Session, item: PayoutItem, rewards: list[ReferralReward], reason: str, *, hold: bool
) -> None:
    item.status = _PS.SKIPPED
    item.failure_reason = reason
    if hold:
        _hold_rewards(rewards, reason)
    db.flush()


# --------------------------------------------------------------------------- batch status


def recompute_batch_status(db: Session, batch_id: UUID) -> BatchStatus:
    """Derive the batch status from its items; locks the batch row, does NOT commit.

    * every non-skipped item ``paid`` (and at least one) -> ``paid``
    * else any item pending / processing / awaiting_manual -> ``paying``
    * else (something failed, or every item was skipped) -> ``failed``: an empty or all-skipped
      batch ends in a terminal non-paying state, and ``paid`` always means money really moved.

    Also refreshes ``total_kobo`` from the non-skipped items. A ``draft`` batch keeps its status
    (nothing has been approved yet). Call it only when the caller holds no marketer/item/reward
    locks (see the module docstring); ``_finalize_batch`` commits first to guarantee that.
    """
    batch = _lock_batch(db, batch_id)
    if batch is None:
        raise BatchNotFoundError(f"batch {batch_id} not found")
    _refresh_total(db, batch_id)
    if batch.status == BatchStatus.DRAFT:
        return batch.status
    statuses = list(db.scalars(select(PayoutItem.status).where(PayoutItem.batch_id == batch_id)))
    live = [s for s in statuses if s != _PS.SKIPPED]
    if live and all(s == _PS.PAID for s in live):
        new = BatchStatus.PAID
    elif any(s in _UNRESOLVED for s in live):
        new = BatchStatus.PAYING
    else:
        new = BatchStatus.FAILED
    if batch.status != new:
        logger.info(f"payout batch {batch_id}: {batch.status.value} -> {new.value}")
        batch.status = new
        db.flush()
    return new


def _finalize_batch(db: Session, batch_id: UUID) -> None:
    db.commit()  # drop any marketer/item/reward locks first: the batch lock is taken alone
    recompute_batch_status(db, batch_id)
    db.commit()


# --------------------------------------------------------------------------- dispute holds


def lock_payout_context(db: Session, marketer_id: UUID, batch_id: UUID | None) -> PayoutItem | None:
    """Lock batch -> marketer -> item -> the marketer's batched rewards; return the item.

    Used by ``hold_for_dispute`` before it touches a reward that carries a batch link. ``None``
    when there is no batch link or no item.
    """
    if batch_id is None:
        return None
    _lock_batch(db, batch_id)
    _lock_marketer(db, marketer_id)
    item = db.scalar(
        select(PayoutItem)
        .where(PayoutItem.batch_id == batch_id, PayoutItem.marketer_id == marketer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    _lock_batched_rewards(db, batch_id, marketer_id)
    return item


def release_held_reward_from_item(
    db: Session, reward: ReferralReward, item: PayoutItem | None
) -> None:
    """A batched reward was just held (R38). Decide what the batch link and the item become.

    ``processing``, ``failed`` (R41) and ``awaiting_manual`` (R49): money may already have gone
    out (a live or OTP-pending transfer, or an admin who paid at the bank before recording it),
    so the reward KEEPS its batch link and the item is NOT refreshed; ``release`` and ``void``
    refuse it until ``mark_paid`` (an ``awaiting_manual`` or ``failed`` item), or the verify-first
    retry (a ``failed``/``processing`` item), has resolved the item. ``pending``: nothing can have
    been sent (the admin is never shown a pending item as payable), so the link is cleared and the
    item is refreshed from the rewards still batched. The caller holds the batch lock
    (``lock_payout_context``), so the total and the batch status are refreshed here.
    """
    if item is not None and item.status in (_PS.PROCESSING, _PS.FAILED, _PS.AWAITING_MANUAL):
        logger.warning(f"reward {reward.id} held while item {item.id} is {item.status.value}; kept")
        return
    reward.payout_batch_id = None
    if item is None or item.status != _PS.PENDING:
        return
    _refresh_item(db, item)
    recompute_batch_status(db, item.batch_id)


def _refresh_item(db: Session, item: PayoutItem) -> None:
    """Recompute ``reward_ids`` and ``amount_kobo`` from the rewards currently batched (R32).

    An item left with none is ``skipped``. Only used for ``pending`` items (nothing can have been
    sent for them, R49). Does not touch the batch row (callers refresh the total once they hold
    the batch lock or after committing).
    """
    rewards = _lock_batched_rewards(db, item.batch_id, item.marketer_id)
    if rewards:
        item.reward_ids = [str(r.id) for r in rewards]
        item.amount_kobo = sum(r.amount_kobo for r in rewards)
    else:
        item.reward_ids = []
        item.amount_kobo = 0
        _skip_item(db, item, [], NO_BATCHED_REWARDS, hold=False)
    db.flush()


# --------------------------------------------------------------------------- prepare


def prepare_batch(db: Session, now: datetime) -> PayoutBatch | None:
    """Group every ``payable`` reward of an ``active`` marketer into a new ``draft`` batch.

    One ``PayoutItem(pending)`` per marketer; the rewards become ``batched`` with the batch id.
    Returns ``None`` (creating nothing) when no reward is payable, which is also why calling it
    twice in a row yields no second batch. Rewards are row-locked and re-checked, so a reward
    held/voided concurrently is not picked up. Commits.
    """
    now = _aware(now)
    active = select(Marketer.id).where(Marketer.status == MarketerStatus.ACTIVE)
    rewards = list(
        db.scalars(
            select(ReferralReward)
            .where(
                ReferralReward.status == RewardStatus.PAYABLE,
                ReferralReward.marketer_id.in_(active),
            )
            .order_by(ReferralReward.marketer_id, ReferralReward.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not rewards:
        db.commit()
        return None

    by_marketer: dict[UUID, list[ReferralReward]] = {}
    for reward in rewards:
        by_marketer.setdefault(reward.marketer_id, []).append(reward)

    batch = PayoutBatch(
        period_label=now.strftime("%Y-%m-%d"),
        status=BatchStatus.DRAFT,
        total_kobo=sum(r.amount_kobo for r in rewards),
    )
    db.add(batch)
    db.flush()
    for marketer_id, group in by_marketer.items():
        db.add(
            PayoutItem(
                batch_id=batch.id,
                marketer_id=marketer_id,
                amount_kobo=sum(r.amount_kobo for r in group),
                status=_PS.PENDING,
                reward_ids=[str(r.id) for r in group],
            )
        )
        for reward in group:
            reward.status = RewardStatus.BATCHED
            reward.payout_batch_id = batch.id
    db.commit()
    logger.info(f"payout batch {batch.id} prepared: {len(by_marketer)} marketers")
    return batch


# --------------------------------------------------------------------------- claim


@dataclass(frozen=True)
class _Claim:
    item_id: UUID
    marketer_id: UUID
    batch_id: UUID
    reference: str
    attempt: int
    amount_kobo: int
    reason: str


def _claim(
    db: Session,
    item_id: UUID,
    *,
    expect_status: PayoutItemStatus,
    expect_reference: str | None,
    expect_attempt: int,
    expect_claimed_at: datetime | None,
    new_reference: str,
    new_attempt: int,
) -> _Claim | None:
    """Atomically take ownership of an item for ONE provider call; ``None`` if not claimable.

    Locks marketer -> item -> rewards, re-checks that the item is still exactly what the caller
    saw, skips it (holding its rewards) when the marketer is no longer active or nothing is left
    to pay, computes the amount from the rewards currently ``batched`` for this marketer in this
    batch (R32), and moves the item to ``processing`` with a conditional UPDATE whose row count is
    checked. Commits, which releases every lock before the caller talks to the provider.
    """
    seen = db.get(PayoutItem, item_id, populate_existing=True)
    if seen is None:
        db.commit()
        return None
    marketer = _lock_marketer(db, seen.marketer_id)
    item = _lock_item(db, item_id)
    if (
        item is None
        or item.status != expect_status
        or item.transfer_reference != expect_reference
        or item.attempt != expect_attempt
        or not _same_instant(item.claimed_at, expect_claimed_at)
    ):
        db.commit()
        return None
    batch = db.get(PayoutBatch, item.batch_id)
    rewards, stale_held = _split_claim_rewards(item, _lock_claim_rewards(db, item))
    # Captured earlier but held since: this claim only happens once the previous transfer is
    # known dead (or never existed), so they leave the batch and become releasable (R38).
    _detach(stale_held)

    if marketer is None or marketer.status != MarketerStatus.ACTIVE:
        _skip_item(db, item, rewards, MARKETER_SUSPENDED, hold=True)
        _finalize_batch(db, item.batch_id)
        return None
    if not rewards:
        _skip_item(db, item, rewards, NO_BATCHED_REWARDS, hold=False)
        _finalize_batch(db, item.batch_id)
        return None

    amount = sum(r.amount_kobo for r in rewards)
    if amount <= 0:  # never ask a provider to send nothing (or a negative sum)
        _skip_item(db, item, rewards, NON_POSITIVE_AMOUNT, hold=True)
        _finalize_batch(db, item.batch_id)
        return None
    claimed_cond = (
        PayoutItem.claimed_at.is_(None)
        if item.claimed_at is None
        else PayoutItem.claimed_at == item.claimed_at
    )
    reference_cond = (
        PayoutItem.transfer_reference.is_(None)
        if expect_reference is None
        else PayoutItem.transfer_reference == expect_reference
    )
    claimed = db.execute(
        update(PayoutItem)
        .where(
            PayoutItem.id == item_id,
            PayoutItem.status == expect_status,
            PayoutItem.attempt == expect_attempt,
            reference_cond,
            claimed_cond,
        )
        .values(
            status=_PS.PROCESSING,
            transfer_reference=new_reference,
            attempt=new_attempt,
            amount_kobo=amount,
            reward_ids=[str(r.id) for r in rewards],
            claimed_at=datetime.now(UTC),
            failure_reason=None,
            provider_status=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.commit()
        return None
    period = batch.period_label if batch is not None else ""
    claim = _Claim(
        item_id=item_id,
        marketer_id=item.marketer_id,
        batch_id=item.batch_id,
        reference=new_reference,
        attempt=new_attempt,
        amount_kobo=amount,
        reason=f"Luran referral payout {period}".strip(),
    )
    _finalize_batch(db, item.batch_id)  # commits (releasing every lock), then refreshes the total
    return claim


# --------------------------------------------------------------------------- recording results


def _pay_rewards(db: Session, item: PayoutItem, reference: str, now: datetime) -> None:
    """Mark EXACTLY the item's captured rewards paid, refusing anything that does not fit.

    A captured reward held by a dispute after the claim (``processing`` item: it keeps its batch
    link) is still paid: the money for it has already gone out, and leaving it payable would let
    it be paid a second time. It gets the ``paid_in_flight`` review reason.

    Defensive guards (a violation is an anomaly: it is logged with ids only and that reward is
    NOT touched): the reward must still be linked to THIS item's batch, be ``batched`` or
    ``held``, and carry no transfer reference. A reward that is already ``paid`` is never paid a
    second time. So one reward id can never end up paid by two items.
    """
    for reward in _lock_rewards_by_id(db, item):
        if reward.status == RewardStatus.PAID:
            if reward.transfer_reference != reference:
                logger.error(
                    f"payout anomaly: reward {reward.id} already paid, item {item.id} not applied"
                )
            continue
        if (
            reward.payout_batch_id != item.batch_id
            or reward.status not in (RewardStatus.BATCHED, RewardStatus.HELD)
            or reward.transfer_reference is not None
        ):
            logger.error(
                f"payout anomaly: reward {reward.id} ({reward.status.value}) not payable by "
                f"item {item.id}; left untouched"
            )
            continue
        if reward.status == RewardStatus.HELD:
            reasons = list(reward.review_reasons or [])
            if PAID_IN_FLIGHT not in reasons:
                reasons.append(PAID_IN_FLIGHT)
            reward.review_reasons = reasons
            logger.warning(f"reward {reward.id} was held when its transfer paid")
        reward.status = RewardStatus.PAID
        reward.paid_at = now
        reward.transfer_reference = reference


def _mark_item_paid(
    db: Session, item: PayoutItem, reference: str, now: datetime, provider_status: str | None
) -> None:
    item.status = _PS.PAID
    item.paid_at = now
    item.failure_reason = None
    item.provider_status = provider_status
    _pay_rewards(db, item, reference, now)
    db.flush()
    # Reached only on a real transition to paid; the email goes out after the owning commit.
    queue_payout_notice(
        db,
        PayoutNotice(
            item_id=item.id,
            marketer_id=item.marketer_id,
            batch_id=item.batch_id,
            amount_kobo=item.amount_kobo,
            reference=reference,
        ),
    )


def _record_result(db: Session, claim: _Claim, result: TransferResult) -> None:
    """Second short transaction: write the provider's answer onto the claimed item."""
    marketer = _lock_marketer(db, claim.marketer_id)
    item = _lock_item(db, claim.item_id)
    if (
        marketer is None
        or item is None
        or item.status != _PS.PROCESSING
        or item.transfer_reference != claim.reference
        or item.attempt != claim.attempt
    ):
        # Resolved meanwhile (a webhook already paid or failed it): do not overwrite that.
        db.commit()
        return
    item.provider_status = result.provider_status
    if result.status == "success":
        _mark_item_paid(db, item, claim.reference, datetime.now(UTC), result.provider_status)
    elif result.status == "failed":
        item.status = _PS.FAILED
        item.failure_reason = (result.message or "transfer_failed")[:500]
    elif result.status == "otp":
        item.status = _PS.FAILED
        item.failure_reason = OTP_REQUIRED
    elif result.status == "manual":
        item.status = _PS.AWAITING_MANUAL
    # "pending": stays processing; the webhook or a verify finalises it.
    db.flush()
    db.commit()
    send_payout_notices(db)
    _finalize_batch(db, claim.batch_id)


def _run_provider(db: Session, claim: _Claim, provider: PayoutProvider) -> None:
    """Call the provider for a claimed item (no locks held) and record the answer."""
    marketer = db.get(Marketer, claim.marketer_id, populate_existing=True)
    try:
        result = provider.pay(marketer, claim.amount_kobo, claim.reference, reason=claim.reason)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - the item stays processing; verify-first retry resolves
        if not db.is_active:  # only a failed transaction needs clearing
            db.rollback()
        db.commit()
        logger.error(
            f"payout provider raised for item {claim.item_id} ({type(exc).__name__}); "
            "item left processing"
        )
        return
    _record_result(db, claim, result)


# --------------------------------------------------------------------------- approve


def approve_batch(
    db: Session,
    batch_id: UUID,
    admin_id: UUID,
    provider: PayoutProvider | None = None,
    after_approval: Callable[[PayoutBatch], None] | None = None,
) -> PayoutBatch:
    """Approve a ``draft`` batch and pay its items; idempotent.

    A batch that is not ``draft`` is returned unchanged with no provider call (a second or
    concurrent approval). Otherwise, in one transaction under the batch row lock: draft ->
    approved -> paying, the approver is recorded, and every item whose marketer is not active is
    skipped with its still-batched rewards returned to ``held`` (R8). That commits; only then are
    the remaining items claimed and paid one by one (see ``_claim``).

    ``after_approval(batch)`` runs right after that commit and before the first provider call, and
    only when this call actually performed the approval, so the audit entry exists before any money
    can move. It can never block or abort the payout: a failure is logged by exception class only.
    """
    batch = _lock_batch(db, batch_id)
    if batch is None:
        db.commit()
        raise BatchNotFoundError(f"batch {batch_id} not found")
    if batch.status != BatchStatus.DRAFT:
        db.commit()  # release the lock; nothing changed
        return batch

    batch.status = BatchStatus.APPROVED
    batch.approved_by_admin_id = admin_id
    batch.approved_at = datetime.now(UTC)
    db.flush()
    items = list(
        db.scalars(
            select(PayoutItem)
            .where(PayoutItem.batch_id == batch_id, PayoutItem.status == _PS.PENDING)
            .order_by(PayoutItem.marketer_id)
        )
    )
    for item in items:
        marketer = _lock_marketer(db, item.marketer_id)
        if marketer is None or marketer.status != MarketerStatus.ACTIVE:
            locked = _lock_item(db, item.id)
            rewards = _lock_batched_rewards(db, batch_id, item.marketer_id)
            if locked is not None and locked.status == _PS.PENDING:
                _skip_item(db, locked, rewards, MARKETER_SUSPENDED, hold=True)
    batch.status = BatchStatus.PAYING
    db.flush()
    _refresh_total(db, batch_id)  # we hold the batch lock
    db.commit()

    if after_approval is not None:
        try:
            after_approval(batch)
        except Exception as exc:  # noqa: BLE001 - the audit must never stop the money flow
            logger.error(f"approval hook failed for batch {batch_id} ({type(exc).__name__})")
            if not db.is_active:
                db.rollback()
    provider = provider or get_payout_provider(db)
    _process_pending_items(db, batch_id, provider)
    _finalize_batch(db, batch_id)
    db.refresh(batch)
    return batch


def _process_pending_items(db: Session, batch_id: UUID, provider: PayoutProvider) -> None:
    rows = db.execute(
        select(PayoutItem.id, PayoutItem.marketer_id)
        .where(PayoutItem.batch_id == batch_id, PayoutItem.status == _PS.PENDING)
        .order_by(PayoutItem.marketer_id)
    ).all()
    db.commit()
    for item_id, marketer_id in rows:
        claim = _claim(
            db,
            item_id,
            expect_status=_PS.PENDING,
            expect_reference=None,
            expect_attempt=1,
            expect_claimed_at=None,
            new_reference=item_reference(batch_id, marketer_id, 1),
            new_attempt=1,
        )
        if claim is not None:
            _run_provider(db, claim, provider)


# --------------------------------------------------------------------------- retry


def retry_batch(
    db: Session,
    batch_id: UUID,
    admin_id: UUID,
    provider: PayoutProvider | None = None,
) -> PayoutBatch:
    """Re-drive a batch's unfinished items, VERIFY FIRST (R33).

    * ``pending`` items (an approval that crashed before reaching them) are claimed and paid.
    * ``failed`` and ``processing`` items are looked up with ``provider.verify(reference)`` and
      only then, depending on the answer: ``success`` -> marked paid, never sent again;
      ``pending`` -> left alone; ``failed``/``reversed`` -> attempt + 1 with a new reference
      ``ref-{batch}-{marketer}-r{attempt}``; ``not_found`` -> the SAME reference is reused (no
      transfer exists), for a ``processing`` item only once its claim is stale.
    * If verify cannot answer, the item is left exactly as it is.

    ``awaiting_manual`` items are never retried (``mark_paid`` resolves them). A ``paid`` batch is
    returned unchanged; a ``draft`` batch must be approved, not retried.
    """
    batch = _lock_batch(db, batch_id)
    if batch is None:
        db.commit()
        raise BatchNotFoundError(f"batch {batch_id} not found")
    if batch.status == BatchStatus.DRAFT:
        db.commit()
        raise BatchStateError(f"batch {batch_id} is draft: approve it instead of retrying")
    if batch.status == BatchStatus.PAID:
        db.commit()
        return batch
    db.commit()

    provider = provider or get_payout_provider(db)
    item_ids = list(
        db.scalars(
            select(PayoutItem.id)
            .where(
                PayoutItem.batch_id == batch_id,
                PayoutItem.status.in_((_PS.PENDING, _PS.PROCESSING, _PS.FAILED)),
            )
            .order_by(PayoutItem.marketer_id)
        )
    )
    db.commit()
    logger.info(f"payout batch {batch_id} retry by admin {admin_id}: {len(item_ids)} items")
    for item_id in item_ids:
        _retry_item(db, item_id, provider)
    _finalize_batch(db, batch_id)
    db.refresh(batch)
    return batch


def _retry_item(db: Session, item_id: UUID, provider: PayoutProvider) -> None:
    item = db.get(PayoutItem, item_id, populate_existing=True)
    if item is None or item.status not in (_PS.PENDING, _PS.PROCESSING, _PS.FAILED):
        db.commit()
        return
    status, reference, attempt = item.status, item.transfer_reference, item.attempt
    provider_status = item.provider_status
    claimed_at = item.claimed_at
    batch_id, marketer_id = item.batch_id, item.marketer_id
    db.commit()  # no transaction is open while we talk to the provider

    if reference is None:
        # Never sent anything (a pending item, or a failure before the first claim stuck): safe.
        claim = _claim(
            db,
            item_id,
            expect_status=status,
            expect_reference=None,
            expect_attempt=attempt,
            expect_claimed_at=claimed_at,
            new_reference=item_reference(batch_id, marketer_id, 1),
            new_attempt=1,
        )
        if claim is not None:
            _run_provider(db, claim, provider)
        return

    try:
        # A Paystack-era item is verified at Paystack even if the setting is manual again (M5).
        verification = get_verifier(db, reference, provider_status, provider).verify(reference)
    except PayoutProviderError as exc:
        logger.warning(f"item {item_id}: transfer state unknown, left as is ({type(exc).__name__})")
        return

    if verification.status == "success":
        _apply_verified_success(db, item_id, reference, verification)
        return
    if verification.status == "pending":
        _note_provider_status(db, item_id, reference, verification.provider_status)
        return

    if verification.status == "not_found":
        if status == _PS.PROCESSING and not _is_stale(claimed_at):
            return  # a worker may be in the middle of its call: do not race it
        new_attempt, new_reference = attempt, reference
    else:  # failed / reversed: Paystack confirms the previous transfer will not pay
        new_attempt = attempt + 1
        new_reference = item_reference(batch_id, marketer_id, new_attempt)

    claim = _claim(
        db,
        item_id,
        expect_status=status,
        expect_reference=reference,
        expect_attempt=attempt,
        expect_claimed_at=claimed_at,
        new_reference=new_reference,
        new_attempt=new_attempt,
    )
    if claim is not None:
        _run_provider(db, claim, provider)


def _is_stale(claimed_at: datetime | None) -> bool:
    return claimed_at is None or datetime.now(UTC) - _aware(claimed_at) >= STALE_PROCESSING_AFTER


def _note_provider_status(
    db: Session, item_id: UUID, reference: str, provider_status: str | None
) -> None:
    item = _lock_item(db, item_id)
    if item is not None and item.status == _PS.PROCESSING and item.transfer_reference == reference:
        item.provider_status = provider_status
        db.flush()
    db.commit()


def _apply_verified_success(
    db: Session, item_id: UUID, reference: str, verification: TransferVerification
) -> None:
    """Paystack says the transfer succeeded: mark the item and its rewards paid, never re-send."""
    seen = db.get(PayoutItem, item_id, populate_existing=True)
    if seen is None:
        db.commit()
        return
    batch_id = seen.batch_id
    _lock_marketer(db, seen.marketer_id)
    item = _lock_item(db, item_id)
    if (
        item is None
        or item.transfer_reference != reference
        or item.status not in (_PS.PROCESSING, _PS.FAILED)
    ):
        db.commit()
        return
    if item.failure_reason == REVERSED or item.provider_status == REVERSED:
        logger.error(f"transfer for item {item_id} was reversed; a success is not honoured")
        db.commit()
        return
    if verification.amount_kobo is not None and verification.amount_kobo != item.amount_kobo:
        logger.error(
            f"transfer {reference}: verified amount differs from the item; not marking paid"
        )
        db.commit()
        return
    _mark_item_paid(db, item, reference, datetime.now(UTC), verification.provider_status)
    db.commit()
    send_payout_notices(db)
    _finalize_batch(db, batch_id)


# --------------------------------------------------------------------------- manual payment


def _claimed_payable_set(
    item: PayoutItem, locked: list[ReferralReward]
) -> list[ReferralReward] | None:
    """The rewards the item claimed that can still be paid by it (``batched``, or ``held`` since
    the claim, both still linked to this batch and unpaid), if they add up to the claimed amount;
    otherwise ``None`` (the claim no longer matches what is there, so it is not accepted)."""
    ids = {str(i) for i in (item.reward_ids or [])}
    rewards = [
        r
        for r in locked
        if str(r.id) in ids
        and r.payout_batch_id == item.batch_id
        and r.status in (RewardStatus.BATCHED, RewardStatus.HELD)
        and r.transfer_reference is None
    ]
    if rewards and sum(r.amount_kobo for r in rewards) == item.amount_kobo:
        return rewards
    return None


def mark_paid(
    db: Session,
    batch_id: UUID,
    entries: list[tuple[UUID, str, int]],
    admin_id: UUID,
    provider: PayoutProvider | None = None,
) -> PayoutBatch:
    """Record that an admin paid items outside the system; ``entries`` = (marketer_id, bank ref,
    amount_kobo the admin paid).

    Allowed ONLY from ``awaiting_manual`` or ``failed``; ``pending``, ``processing`` and ``paid``
    are refused (a live Paystack transfer could still succeed and pay twice). A ``failed`` item
    additionally needs its last transfer verified as ``failed``, ``reversed`` or ``not_found``
    through the provider; if that cannot be confirmed (verify errors, says pending/success, or the
    provider cannot verify) the whole call is refused. All entries are validated first and applied
    together or not at all. The bank reference becomes the item's ``transfer_reference`` and its
    captured rewards become ``paid``.

    The item is first recomputed from the rewards batched right now (R38). The amount the admin
    says they paid must be one of the two figures the batch detail shows (R43, R49), otherwise the
    call is refused with ``AmountMismatchError`` and nothing changes:

    * the freshly recomputed batched sum (``current_amount_kobo``): that set is paid and rewards
      held since the claim are detached (releasable afterwards), as for a ``failed`` item;
    * for an ``awaiting_manual`` item only, the item's claimed ``amount_kobo``: the admin may
      have paid it at the bank before a dispute held one of its rewards, so the whole claimed set
      is paid (a reward held since is marked paid with the ``paid_in_flight`` reason) and nothing
      can be paid a second time.
    """
    cleaned: dict[UUID, str] = {}
    paid_amounts: dict[UUID, int] = {}
    for marketer_id, ref, amount in entries:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise PayoutStateError("a positive amount in kobo is required for every entry")
        paid_amounts[marketer_id] = amount
        ref = (ref or "").strip()
        if not ref or len(ref) > 100:
            raise PayoutStateError("a bank reference of 1-100 characters is required")
        if marketer_id in cleaned:
            raise PayoutStateError(f"marketer {marketer_id} appears twice")
        cleaned[marketer_id] = ref
    if not cleaned:
        raise PayoutStateError("no entries to record")
    if len(set(cleaned.values())) != len(cleaned):
        raise PayoutStateError("each payment needs its own bank reference")

    batch = db.get(PayoutBatch, batch_id, populate_existing=True)
    if batch is None:
        raise BatchNotFoundError(f"batch {batch_id} not found")
    if batch.status not in (BatchStatus.PAYING, BatchStatus.FAILED):
        status = batch.status.value
        db.commit()
        raise BatchStateError(f"batch {batch_id} is {status}: nothing to mark paid")

    # Failed items: confirm with the provider BEFORE taking any lock.
    to_verify: dict[UUID, str] = {}
    verify_status: dict[UUID, str | None] = {}
    for marketer_id in cleaned:
        item = db.scalar(
            select(PayoutItem).where(
                PayoutItem.batch_id == batch_id, PayoutItem.marketer_id == marketer_id
            )
        )
        if item is None:
            db.commit()
            raise PayoutItemNotFoundError(f"marketer {marketer_id} has no item in this batch")
        if item.status == _PS.FAILED and item.transfer_reference:
            to_verify[marketer_id] = item.transfer_reference
            verify_status[marketer_id] = item.provider_status
    db.commit()
    if to_verify:
        provider = provider or get_payout_provider(db)
        for marketer_id, reference in to_verify.items():
            verifier = get_verifier(db, reference, verify_status[marketer_id], provider)
            try:
                verification = verifier.verify(reference)
            except PayoutProviderError as exc:
                raise TransferUnverifiedError(
                    f"cannot confirm the failed transfer for marketer {marketer_id} did not "
                    f"succeed ({exc}); refusing to mark it paid"
                ) from exc
            if verification.status not in ("failed", "reversed", "not_found"):
                raise TransferUnverifiedError(
                    f"transfer for marketer {marketer_id} is {verification.status} at the "
                    "provider; refusing to mark it paid"
                )

    # Pass 1: lock everything (marketer -> item -> rewards, in a fixed order) and validate; change nothing,
    # so a refusal leaves no partial state and needs no rollback.
    locked: list[tuple[UUID, PayoutItem]] = []
    fresh: dict[UUID, tuple[list[ReferralReward], list[ReferralReward]]] = {}
    refusal: PayoutError | None = None
    for marketer_id in sorted(cleaned):
        _lock_marketer(db, marketer_id)
        item = db.scalar(
            select(PayoutItem)
            .where(PayoutItem.batch_id == batch_id, PayoutItem.marketer_id == marketer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            refusal = PayoutItemNotFoundError(f"marketer {marketer_id} has no item in this batch")
        elif item.status not in (_PS.AWAITING_MANUAL, _PS.FAILED):
            refusal = ItemNotMarkableError(
                f"item for marketer {marketer_id} is {item.status.value}: only "
                "awaiting_manual or failed items can be marked paid",
                marketer_id,
                item.status,
            )
        elif item.status == _PS.FAILED and item.transfer_reference != to_verify.get(marketer_id):
            # Not the transfer we verified (it changed in between): nothing was confirmed.
            refusal = TransferUnverifiedError("the item changed while it was being verified; retry")
        else:
            # Never pay a stale claim: the set (and amount) is what is batched RIGHT NOW (R38),
            # or, for an awaiting_manual item only, the claimed set the admin may have paid (R49).
            locked_rewards = _lock_claim_rewards(db, item)
            batched, stale_held = _split_claim_rewards(item, locked_rewards)
            current = sum(r.amount_kobo for r in batched)
            paid = paid_amounts[marketer_id]
            claimed_set = (
                _claimed_payable_set(item, locked_rewards)
                if item.status == _PS.AWAITING_MANUAL and paid == item.amount_kobo
                else None
            )
            if batched and current == paid:
                fresh[marketer_id] = (batched, stale_held)
            elif claimed_set is not None:
                fresh[marketer_id] = (claimed_set, [])
            elif not batched and item.status == _PS.FAILED:
                refusal = ItemNotMarkableError(
                    f"item for marketer {marketer_id} has no payable rewards left",
                    marketer_id,
                    item.status,
                )
            else:
                refusal = AmountMismatchError(
                    f"amount for marketer {marketer_id} differs from the item's amounts",
                    marketer_id,
                    current,
                    item.amount_kobo if item.status == _PS.AWAITING_MANUAL else None,
                )
        if refusal is not None:
            db.commit()  # release the locks; nothing was modified
            raise refusal
        locked.append((marketer_id, item))

    taken = db.scalar(
        select(PayoutItem.id)
        .where(
            PayoutItem.transfer_reference.in_(list(cleaned.values())),
            PayoutItem.id.not_in([item.id for _, item in locked]),
        )
        .limit(1)
    )
    if taken is not None:
        db.commit()
        raise BankReferenceInUseError("a bank reference is already used by another payment")

    # Pass 2: apply all together (one transaction: all or nothing).
    now = datetime.now(UTC)
    try:
        for marketer_id, item in locked:
            batched, stale_held = fresh[marketer_id]
            _detach(stale_held)  # held since the claim: leaves the batch, releasable afterwards
            item.reward_ids = [str(r.id) for r in batched]
            item.amount_kobo = sum(r.amount_kobo for r in batched)
            item.transfer_reference = cleaned[marketer_id]
            _mark_item_paid(db, item, cleaned[marketer_id], now, "manual")
        db.flush()
    except IntegrityError as exc:  # lost a race on the unique reference after the check above
        db.rollback()
        raise BankReferenceInUseError(
            "a bank reference is already used by another payment"
        ) from exc
    db.commit()
    send_payout_notices(db)
    logger.info(f"payout batch {batch_id}: {len(cleaned)} item(s) marked paid by admin {admin_id}")
    _finalize_batch(db, batch_id)
    db.refresh(batch)
    return batch


# --------------------------------------------------------------------------- transfer webhooks

TRANSFER_EVENTS = ("transfer.success", "transfer.failed", "transfer.reversed")


def _result(status: str, reason: str | None = None, item: PayoutItem | None = None) -> dict:
    out: dict[str, Any] = {"status": status}
    if reason:
        out["reason"] = reason
    if item is not None:
        out["item_id"] = str(item.id)
        out["batch_id"] = str(item.batch_id)
        out["item_status"] = item.status.value
    return out


def apply_transfer_event(db: Session, event_type: str, data: Any) -> dict:
    """Apply a Paystack ``transfer.success|failed|reversed`` event to the item with that reference.

    Idempotent and never raises for a bad payload: an unknown reference or event is reported as
    ``ignored``. Returns ``{"status": "processed" | "duplicate" | "ignored", ...}``. The webhook's
    signature must already have been checked by the caller.

    * success: ``processing``/``failed`` -> ``paid`` (a late success after an OTP failure still
      counts: the money went); already ``paid`` -> duplicate. A payload amount that differs from
      the item's is refused.
    * failed: ``processing`` -> ``failed``; never un-pays a ``paid`` item.
    * reversed: ``paid`` -> ``failed`` (reason ``reversed``) with its rewards ``paid`` -> ``batched``
      (R35); ``processing`` -> ``failed``.
    """
    if event_type not in TRANSFER_EVENTS:
        return _result("ignored", "unsupported_event")
    reference = data.get("reference") if isinstance(data, dict) else None
    if not isinstance(reference, str) or not reference:
        return _result("ignored", "missing_reference")

    seen = db.scalar(select(PayoutItem).where(PayoutItem.transfer_reference == reference))
    if seen is None:
        db.commit()
        return _result("ignored", "unknown_reference")
    batch_id = seen.batch_id
    _lock_marketer(db, seen.marketer_id)
    item = _lock_item(db, seen.id)
    if item is None or item.transfer_reference != reference:
        db.commit()
        return _result("ignored", "unknown_reference")

    now = datetime.now(UTC)
    outcome: dict
    if event_type == "transfer.success":
        amount = data.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount != item.amount_kobo:
            logger.error(f"transfer {reference}: event amount differs from the item; ignored")
            outcome = _result("ignored", "amount_mismatch", item)
        elif item.status == _PS.PAID:
            outcome = _result("duplicate", None, item)
        elif item.failure_reason == REVERSED or item.provider_status == REVERSED:
            # A success arriving after the reversal (out of order, or a replayed signed body)
            # must not mark the item paid again.
            logger.error(f"transfer.success for reversed item {item.id} ignored")
            outcome = _result("ignored", "item_reversed", item)
        elif item.status == _PS.FAILED and not (
            isinstance(amount, int) and not isinstance(amount, bool)
        ):
            # A late success on a failed item (e.g. an OTP finalised in the dashboard) is only
            # honoured when the event proves the amount; the reference already matched.
            logger.error(f"transfer.success for failed item {item.id} without an amount; ignored")
            outcome = _result("ignored", "amount_missing", item)
        elif item.status in (_PS.PROCESSING, _PS.FAILED):
            _mark_item_paid(db, item, reference, now, "success")
            outcome = _result("processed", None, item)
        else:
            outcome = _result("ignored", "unexpected_state", item)
    elif event_type == "transfer.failed":
        if item.status == _PS.PROCESSING:
            item.status = _PS.FAILED
            item.failure_reason = _failure_text(data, "transfer_failed")
            item.provider_status = "failed"
            outcome = _result("processed", None, item)
        elif item.status == _PS.FAILED:
            outcome = _result("duplicate", None, item)
        else:
            if item.status == _PS.PAID:
                logger.warning(f"transfer.failed for already paid item {item.id}; ignored")
            outcome = _result("ignored", "unexpected_state", item)
    else:  # transfer.reversed
        if item.status == _PS.PAID:
            _unpay_item(db, item)
            outcome = _result("processed", None, item)
        elif item.status == _PS.PROCESSING:
            item.status = _PS.FAILED
            item.failure_reason = "reversed"
            item.provider_status = "reversed"
            outcome = _result("processed", None, item)
        elif item.status == _PS.FAILED:
            outcome = _result("duplicate", None, item)
        else:
            outcome = _result("ignored", "unexpected_state", item)
    db.flush()
    db.commit()
    send_payout_notices(db)  # only a "processed" success queued one; duplicates queue nothing
    if outcome["status"] in ("processed", "duplicate"):
        # A duplicate also recomputes: the first delivery may have committed the item and then
        # failed before the batch status was refreshed.
        _finalize_batch(db, batch_id)
        outcome["item_status"] = db.get(PayoutItem, item.id).status.value
    return outcome


def _failure_text(data: dict, default: str) -> str:
    message = data.get("message")  # never our own transfer `reason` (it echoes what we sent)
    return str(message)[:200] if isinstance(message, str) and message else default


def _unpay_item(db: Session, item: PayoutItem) -> None:
    """A paid transfer was reversed: the item fails and its rewards are re-opened (R35, R37).

    A reward goes back to ``batched`` ONLY if it was batched when the item was claimed (it is not
    ``paid_in_flight``) and its attribution carries no recorded dispute. Otherwise it goes to
    ``held`` (``payment_reversed_after_hold``, or ``payment_dispute`` when a dispute is recorded)
    with its batch link cleared, so it cannot be paid again without an admin's decision.
    """
    reference = item.transfer_reference
    for reward in _lock_rewards_by_id(db, item):
        if reward.status != RewardStatus.PAID or reward.transfer_reference != reference:
            continue
        reward.paid_at = None
        reward.transfer_reference = None
        disputed = (
            reward.tenant_id is not None
            and db.scalar(
                select(ReferralAttribution.disputed_at).where(
                    ReferralAttribution.tenant_id == reward.tenant_id
                )
            )
            is not None
        )
        in_flight = PAID_IN_FLIGHT in (reward.review_reasons or [])
        if disputed or in_flight:
            reasons = list(reward.review_reasons or [])
            reason = PAYMENT_DISPUTE if disputed else REVERSED_AFTER_HOLD
            if reason not in reasons:
                reasons.append(reason)
            reward.review_reasons = reasons
            reward.status = RewardStatus.HELD
            reward.payout_batch_id = None
        else:
            reward.status = RewardStatus.BATCHED
            reward.payout_batch_id = item.batch_id
    item.status = _PS.FAILED
    item.failure_reason = REVERSED
    item.provider_status = REVERSED
    item.paid_at = None
