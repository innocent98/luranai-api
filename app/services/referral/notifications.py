"""Marketer notification emails (conversion and payout), sent strictly AFTER the owning commit.

Two rules make these safe on a money path:

* **Once, on the real transition only.** A conversion notice is requested only for a reward that
  ``create_reward_for_first_payment`` newly created in this call; a payout notice only for an item
  that newly became ``paid`` in this call (``payouts._mark_item_paid``). Replays, duplicates and
  ``already_processed`` paths never reach those points, so they never email. No schema column is
  needed for this.
* **Never an effect on the money.** Nothing here runs before the commit, and every failure
  (including a mailer that returns ``False`` or raises) is caught and logged with the exception
  class and ids only, never an address or a message body. A notice that cannot be sent is lost
  (the reward / payout is still correct and visible in the marketer's dashboard).

Payout notices are queued on the session (``Session.info``) by ``payouts._mark_item_paid`` and
drained by ``send_payout_notices`` right after the commit that stored the payment; the queue is
dropped on any real rollback (a Session ``after_rollback`` hook), so a payment that did not
commit is never announced.
"""

from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models.referral import Marketer, PayoutBatch, ReferralReward
from app.db.models.tenant import Tenant
from app.services.email_service import EmailService

_PAYOUT_QUEUE = "referral_payout_notices"
_CONVERSION_QUEUE = "referral_conversion_notices"


@dataclass(frozen=True)
class PayoutNotice:
    """What a payout email needs; ids and numbers only (the marketer is loaded when sending)."""

    item_id: UUID
    marketer_id: UUID
    batch_id: UUID
    amount_kobo: int
    reference: str


def send_conversion_notices(db: Session, reward_ids: list[UUID]) -> None:
    """Email the marketer of each newly created reward. Call after the commit. Never raises."""
    for reward_id in reward_ids:
        try:
            reward = db.get(ReferralReward, reward_id)
            if reward is None:
                continue
            marketer = db.get(Marketer, reward.marketer_id)
            if marketer is None:
                continue
            tenant = db.get(Tenant, reward.tenant_id) if reward.tenant_id else None
            EmailService().send_referral_conversion_email(
                marketer_email=marketer.email,
                marketer_name=marketer.name,
                amount_kobo=reward.amount_kobo,
                company_name=tenant.display_name if tenant else None,
            )
        except Exception as exc:  # noqa: BLE001 - a notice must never touch the reward
            _clear_failed_transaction(db, exc)
            logger.error(
                f"referral conversion email failed for reward {reward_id}: {type(exc).__name__}"
            )


def queue_conversion_notice(db: Session, reward_id: UUID) -> None:
    """Remember a reward created in the open transaction (for jobs that create many at once)."""
    db.info.setdefault(_CONVERSION_QUEUE, []).append(reward_id)


def send_queued_conversion_notices(db: Session) -> None:
    """Email for every queued reward. Call right after the commit. Never raises."""
    send_conversion_notices(db, db.info.pop(_CONVERSION_QUEUE, []))


def queue_payout_notice(db: Session, notice: PayoutNotice) -> None:
    """Remember that an item just became paid in the open transaction (sent after the commit)."""
    db.info.setdefault(_PAYOUT_QUEUE, []).append(notice)


@event.listens_for(Session, "after_rollback")
def _drop_notices_on_rollback(session: Session) -> None:
    """A real rollback undoes the payments and rewards the queued notices announce: forget them.

    (Not fired for SAVEPOINT rollbacks, which leave the outer transaction's payments in place.)
    """
    session.info.pop(_PAYOUT_QUEUE, None)
    session.info.pop(_CONVERSION_QUEUE, None)


def send_payout_notices(db: Session) -> None:
    """Email every queued payout notice. Call right after the commit. Never raises."""
    notices: list[PayoutNotice] = db.info.pop(_PAYOUT_QUEUE, [])
    for notice in notices:
        try:
            marketer = db.get(Marketer, notice.marketer_id)
            batch = db.get(PayoutBatch, notice.batch_id)
            if marketer is None or batch is None:
                continue
            EmailService().send_referral_payout_email(
                marketer_email=marketer.email,
                marketer_name=marketer.name,
                amount_kobo=notice.amount_kobo,
                period_label=batch.period_label,
                reference=notice.reference,
            )
        except Exception as exc:  # noqa: BLE001 - a notice must never touch the payout
            _clear_failed_transaction(db, exc)
            logger.error(
                f"referral payout email failed for item {notice.item_id}: {type(exc).__name__}"
            )


def _clear_failed_transaction(db: Session, exc: Exception) -> None:
    """After a failed lookup (a database error) the session needs a rollback to be usable.

    A mailer failure leaves the session alone: everything was committed before sending, so there
    is nothing to roll back and nothing to disturb.
    """
    if isinstance(exc, SQLAlchemyError):
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - best effort; the caller's own cleanup follows
            pass
