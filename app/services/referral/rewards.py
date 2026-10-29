"""Referral reward lifecycle: creation on the first payment, hold clearing, release/void, disputes.

This is a money path. The invariants, in order of importance:

* A tenant earns at most one reward, ever. ``referral_rewards.tenant_id`` is unique and is the last
  line of defence; every other check here is an optimisation or a business rule on top of it.
* Rewarding can never break the payment. ``create_reward_for_first_payment`` itself may raise (for
  example when the fraud check cannot run: fail closed, no reward), but the webhook calls it behind
  a SAVEPOINT and swallows the failure. ``reconcile_missing_rewards`` (daily job) repairs any
  reward a failed attempt left missing, which matters because a replayed ``charge.success`` is a
  no-op and would never retry it.
* Only a successful, real payment of the tenant itself earns a reward. Admin overrides and
  promotional grants create no ``PaymentTransaction`` and so can never reach this code path.

State machine::

    pending --(hold passed, marketer active)--> payable --> batched --> paid
    held --release--> pending | payable        any of pending/payable/batched --dispute--> held
    held | pending | payable --void--> void    (batched, paid and void are never voided)
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    Marketer,
    MarketerStatus,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.services.enhanced_audit_service import EnhancedAuditService
from app.services.referral.config import get_referral_config
from app.services.referral.fraud import evaluate_flags

MARKETER_SUSPENDED = "marketer_suspended"
PAYMENT_DISPUTE = "payment_dispute"


class RewardError(Exception):
    """Base class: ``code`` is the stable machine-readable error string."""

    code = "reward_error"


class RewardNotFoundError(RewardError):
    code = "reward_not_found"


class RewardStateError(RewardError):
    """The reward is not in a state the requested transition allows. Nothing was changed."""

    code = "reward_state_invalid"


def _aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC so stored and supplied values always compare."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- creation


def create_reward_for_first_payment(
    db: Session, tenant_id: UUID, payment_reference: str, paid_at: datetime
) -> ReferralReward | None:
    """Create the tenant's one reward for its first successful payment; ``None`` if not earned.

    ``None`` means *this call created nothing*: it is returned both when the tenant does not
    qualify and when a reward already exists (including when a concurrent call won the race on the
    unique ``tenant_id``), so callers can count creations. A reward is created only when ALL hold:

    * ``payment_reference`` is a ``success`` payment of this tenant, and no other successful
      payment of the tenant has an earlier ``paid_at`` (the first payment earns it, never a later
      one that was merely processed first);
    * the tenant has a referral attribution that is not ``self_referral`` (the attribution row is
      required: a reward whose ``tenant_id`` was NULLed by an account purge never counts);
    * ``paid_at <= tenant.created_at + commission_window_days`` (trial payments count);
    * the tenant has no reward yet.

    The reward is ``held`` (never blocked) when the marketer is suspended or the fraud check
    returns reasons, otherwise ``pending``. ``evaluate_flags`` errors propagate and nothing is
    written (fail closed). The insert runs in its own SAVEPOINT, so a unique violation leaves the
    caller's transaction usable.
    """
    paid_at = _aware(paid_at)
    db.flush()  # make a payment the caller just marked as success visible to the queries below

    payment = db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.reference == payment_reference,
            PaymentTransaction.tenant_id == tenant_id,
            PaymentTransaction.status == "success",
        )
    )
    if payment is None:
        return None
    earlier_payment = db.scalar(
        select(PaymentTransaction.id)
        .where(
            PaymentTransaction.tenant_id == tenant_id,
            PaymentTransaction.status == "success",
            PaymentTransaction.id != payment.id,
            PaymentTransaction.paid_at < paid_at,
        )
        .limit(1)
    )
    if earlier_payment is not None:
        return None

    attribution = db.scalar(
        select(ReferralAttribution).where(ReferralAttribution.tenant_id == tenant_id)
    )
    if attribution is None or attribution.self_referral:
        return None
    marketer = db.get(Marketer, attribution.marketer_id)
    tenant = db.get(Tenant, tenant_id)
    if marketer is None or tenant is None:
        return None

    config = get_referral_config(db)
    window_end = _aware(tenant.created_at) + timedelta(days=config.commission_window_days)
    if paid_at > window_end:
        return None

    if _has_reward(db, tenant_id):
        return None

    reasons = list(evaluate_flags(db, tenant_id))
    if marketer.status == MarketerStatus.SUSPENDED:
        reasons.append(MARKETER_SUSPENDED)

    reward = ReferralReward(
        tenant_id=tenant_id,
        marketer_id=marketer.id,
        amount_kobo=config.reward_kobo,
        status=RewardStatus.HELD if reasons else RewardStatus.PENDING,
        converted_at=paid_at,
        payable_after=paid_at + timedelta(days=config.hold_days),
        review_reasons=reasons,
    )
    try:
        with db.begin_nested():
            db.add(reward)
    except IntegrityError:
        # Lost a race on unique(tenant_id): the other writer's reward stands. Anything else (a
        # genuine FK/data error) is not a duplicate and must surface.
        if _has_reward(db, tenant_id):
            logger.info(f"referral reward already exists for tenant {tenant_id}; none created")
            return None
        raise
    logger.info(
        f"referral reward {reward.id} created for tenant {tenant_id} "
        f"(status={reward.status.value}, flags={len(reasons)})"
    )
    return reward


def _has_reward(db: Session, tenant_id: UUID) -> bool:
    return (
        db.scalar(select(ReferralReward.id).where(ReferralReward.tenant_id == tenant_id).limit(1))
        is not None
    )


def reconcile_missing_rewards(db: Session, now: datetime) -> int:
    """Create the reward for every attributed tenant that paid but has none; returns how many.

    Repairs rewards that the webhook's safety net swallowed (or that a crash between the payment
    commit and the reward lost). Considers non-self-referral attributions only, and for each tenant
    its earliest successful payment, which must fall inside the commission window; the creation
    rules themselves are ``create_reward_for_first_payment``'s. Idempotent: a second run finds
    nothing. One tenant failing is logged (ids only) and skipped; the others still proceed, and the
    next run retries it. Does not commit; the caller does.
    """
    now = _aware(now)
    window = timedelta(days=get_referral_config(db).commission_window_days)
    first_payment = (
        select(
            PaymentTransaction.tenant_id.label("tenant_id"),
            func.min(PaymentTransaction.paid_at).label("paid_at"),
        )
        .where(
            PaymentTransaction.status == "success",
            PaymentTransaction.paid_at.is_not(None),
            PaymentTransaction.paid_at <= now,
        )
        .group_by(PaymentTransaction.tenant_id)
        .subquery()
    )
    candidates = db.execute(
        select(ReferralAttribution.tenant_id, first_payment.c.paid_at)
        .join(first_payment, first_payment.c.tenant_id == ReferralAttribution.tenant_id)
        .join(Tenant, Tenant.id == ReferralAttribution.tenant_id)
        .where(
            ReferralAttribution.self_referral.is_(False),
            ~exists().where(ReferralReward.tenant_id == ReferralAttribution.tenant_id),
            first_payment.c.paid_at <= Tenant.created_at + window,
        )
        .order_by(first_payment.c.paid_at)
    ).all()

    created = 0
    for tenant_id, paid_at in candidates:
        try:
            with db.begin_nested():
                reference = db.scalar(
                    select(PaymentTransaction.reference)
                    .where(
                        PaymentTransaction.tenant_id == tenant_id,
                        PaymentTransaction.status == "success",
                        PaymentTransaction.paid_at == paid_at,
                    )
                    .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
                    .limit(1)
                )
                if reference is None:
                    continue
                if create_reward_for_first_payment(db, tenant_id, reference, paid_at):
                    created += 1
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            logger.error(
                f"referral reward reconcile failed for tenant {tenant_id}: {type(exc).__name__}"
            )
    return created


# --------------------------------------------------------------------------- clearing


def promote_pending(db: Session, now: datetime) -> int:
    """``pending`` rewards past their hold whose marketer is active become ``payable``.

    A single conditional UPDATE, so it is atomic and safe against a concurrent dispute hold or
    release (the WHERE is re-checked on each row after waiting for its lock). Held rewards and
    rewards of suspended marketers are untouched. Returns the number promoted; does not commit.
    """
    active_marketers = select(Marketer.id).where(Marketer.status == MarketerStatus.ACTIVE)
    result = db.execute(
        update(ReferralReward)
        .where(
            ReferralReward.status == RewardStatus.PENDING,
            ReferralReward.payable_after <= _aware(now),
            ReferralReward.marketer_id.in_(active_marketers),
        )
        .values(status=RewardStatus.PAYABLE)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


# --------------------------------------------------------------------------- admin actions


def _locked_reward(db: Session, reward_id: UUID) -> ReferralReward:
    reward = db.scalar(
        select(ReferralReward)
        .where(ReferralReward.id == reward_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reward is None:
        raise RewardNotFoundError(f"reward {reward_id} not found")
    return reward


def _require_note(note: str | None) -> str:
    cleaned = (note or "").strip()
    if not cleaned:
        raise ValueError("A note is required")
    return cleaned


def _audit(
    db: Session,
    action: str,
    reward: ReferralReward,
    admin_id: UUID,
    admin_name: str | None,
    from_status: RewardStatus,
    note: str,
) -> None:
    """Record admin id + note. Runs after the change is committed; failure is logged, not raised."""
    try:
        EnhancedAuditService(db).log_action(
            action_type=action,
            performed_by_type="platform_admin",
            performed_by_id=admin_id,
            performed_by_name=admin_name,
            target_type="referral_reward",
            target_id=reward.id,
            description=f"{action.replace('_', ' ').capitalize()}",
            severity="medium",
            metadata={
                "note": note,
                "from_status": from_status.value,
                "to_status": reward.status.value,
                "marketer_id": str(reward.marketer_id),
            },
        )
    except Exception as exc:  # noqa: BLE001 - the money change already committed
        db.rollback()
        logger.error(f"Failed to write audit log for {action} on reward {reward.id}: {exc}")


def release(
    db: Session,
    reward_id: UUID,
    admin_id: UUID,
    note: str,
    *,
    now: datetime | None = None,
    admin_name: str | None = None,
) -> ReferralReward:
    """Admin clears a ``held`` reward: ``payable`` if its hold date has passed, else ``pending``.

    ``payable_after`` is kept as is. Raises ``ValueError`` without a note, ``RewardNotFoundError``,
    or ``RewardStateError`` (nothing changed) when the reward is not ``held``. Commits.
    """
    note = _require_note(note)
    reward = _locked_reward(db, reward_id)
    if reward.status != RewardStatus.HELD:
        raise RewardStateError(
            f"reward {reward_id} is {reward.status.value}, only held can be released"
        )
    moment = _aware(now) if now else datetime.now(UTC)
    reward.status = (
        RewardStatus.PAYABLE if _aware(reward.payable_after) <= moment else RewardStatus.PENDING
    )
    db.commit()
    _audit(db, "release_referral_reward", reward, admin_id, admin_name, RewardStatus.HELD, note)
    return reward


def void(
    db: Session,
    reward_id: UUID,
    admin_id: UUID,
    note: str,
    *,
    admin_name: str | None = None,
) -> ReferralReward:
    """Admin voids a ``held``, ``pending`` or ``payable`` reward; never one batched, paid or void.

    Raises like ``release``. Commits.
    """
    note = _require_note(note)
    reward = _locked_reward(db, reward_id)
    previous = reward.status
    if previous not in (RewardStatus.HELD, RewardStatus.PENDING, RewardStatus.PAYABLE):
        raise RewardStateError(f"reward {reward_id} is {previous.value} and cannot be voided")
    reward.status = RewardStatus.VOID
    db.commit()
    _audit(db, "void_referral_reward", reward, admin_id, admin_name, previous, note)
    return reward


# --------------------------------------------------------------------------- disputes


def hold_for_dispute(db: Session, tenant_id: UUID) -> ReferralReward | None:
    """Hold the tenant's unpaid reward after a payment dispute; returns it, or ``None`` if none.

    ``pending``, ``payable`` and ``batched`` become ``held`` with reason ``payment_dispute`` (a
    batched reward also leaves its payout batch). An already ``held`` reward keeps its status and
    gains the reason once. ``paid`` and ``void`` rewards are untouched. Row-locked; does not commit.
    """
    reward = db.scalar(
        select(ReferralReward)
        .where(ReferralReward.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reward is None:
        return None
    if reward.status in (RewardStatus.PAID, RewardStatus.VOID):
        return reward
    if reward.status == RewardStatus.BATCHED:
        reward.payout_batch_id = None
    reward.status = RewardStatus.HELD
    if PAYMENT_DISPUTE not in (reward.review_reasons or []):
        reward.review_reasons = [*(reward.review_reasons or []), PAYMENT_DISPUTE]
    db.flush()
    logger.warning(f"referral reward {reward.id} held for payment dispute")
    return reward
