"""Referral payout batches, per-item idempotency and providers.

This is money-movement code, so the tests run against real Postgres (``db_session``), and the
concurrency tests use real sessions on separate connections. NO test calls the real Paystack API:
the Paystack provider tests use ``httpx.MockTransport``, everything else a counting fake provider.
Bank details are obviously fake placeholders.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from loguru import logger
from sqlalchemy import BigInteger, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models.referral import (
    AttributionSource,
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
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.paystack_service import PaystackService, PaystackTransferError
from app.services.referral import payouts
from app.services.referral.marketer_service import MarketerService
from app.services.referral.payouts import (
    PayoutItemNotFoundError,
    PayoutStateError,
    apply_transfer_event,
    approve_batch,
    item_reference,
    mark_paid,
    prepare_batch,
    recompute_batch_status,
    retry_batch,
)
from app.services.referral.providers import (
    ManualPayoutProvider,
    PaystackTransferProvider,
    PayoutProviderError,
    TransferResult,
    TransferVerification,
    get_payout_provider,
)
from app.services.referral.rewards import (
    RewardError,
    RewardInFlightError,
    RewardStateError,
    hold_for_dispute,
    record_dispute,
    release,
    void,
)

NOW = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
PENDING = PayoutItemStatus.PENDING
PROCESSING = PayoutItemStatus.PROCESSING
PAID = PayoutItemStatus.PAID
FAILED = PayoutItemStatus.FAILED
SKIPPED = PayoutItemStatus.SKIPPED
AWAITING = PayoutItemStatus.AWAITING_MANUAL
SECRET = "sk_test_placeholder_not_a_real_key"


# --------------------------------------------------------------------------- builders


def _marketer(db, status=MarketerStatus.ACTIVE, *, bank=True) -> Marketer:
    marketer = Marketer(
        name="Payout Marketer",
        email=f"m_{uuid4().hex[:6]}@marketer-co.example",
        code=f"code-{uuid4().hex[:8]}",
        status=status,
        bank_code="000" if bank else None,
        account_number="0000000000" if bank else None,
        account_name="Placeholder Name" if bank else None,
    )
    db.add(marketer)
    db.flush()
    return marketer


def _reward(db, marketer, status=RewardStatus.PAYABLE, amount=500_000, tenant=None):
    reward = ReferralReward(
        tenant_id=tenant.id if tenant else None,
        marketer_id=marketer.id,
        amount_kobo=amount,
        status=status,
        converted_at=NOW - timedelta(days=10),
        payable_after=NOW - timedelta(days=3),
    )
    db.add(reward)
    db.flush()
    return reward


def _tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Payout Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    db.add(TenantCapabilities(tenant_id=tenant.id))
    db.flush()
    return tenant


def _batch_of(db, count=2, amount=500_000):
    """A prepared (draft) batch with one payable reward for each of ``count`` new marketers."""
    marketers = [_marketer(db) for _ in range(count)]
    rewards = [_reward(db, m, amount=amount) for m in marketers]
    batch = prepare_batch(db, NOW)
    assert batch is not None
    return batch, marketers, rewards


def _item(db, batch, marketer) -> PayoutItem:
    db.expire_all()
    return db.scalar(
        select(PayoutItem).where(
            PayoutItem.batch_id == batch.id, PayoutItem.marketer_id == marketer.id
        )
    )


def _reload(db, obj):
    db.expire_all()
    return db.get(type(obj), obj.id)


class FakeProvider:
    """Counts calls. ``pay_fn`` / ``verify_fn`` script the answers."""

    def __init__(self, pay_fn=None, verify_fn=None, delay=0.0):
        self.pay_calls: list[tuple[UUID, int, str]] = []
        self.verify_calls: list[str] = []
        self._pay_fn = pay_fn or (lambda m, a, r: TransferResult("success", r, "success"))
        self._verify_fn = verify_fn or (lambda r: TransferVerification("not_found", r))
        self._delay = delay

    def pay(self, marketer, amount_kobo, reference, *, reason=None):
        self.pay_calls.append((marketer.id, amount_kobo, reference))
        if self._delay:
            time.sleep(self._delay)
        return self._pay_fn(marketer, amount_kobo, reference)

    def verify(self, reference):
        self.verify_calls.append(reference)
        return self._verify_fn(reference)


def _pending_for(*ids):
    """pay_fn: the listed marketers get ``pending``, the rest succeed."""

    def fn(marketer, amount, reference):
        status = "pending" if marketer.id in ids else "success"
        return TransferResult(status, reference, status)

    return fn


def _failing_for(*ids, status="failed"):
    def fn(marketer, amount, reference):
        if marketer.id in ids:
            return TransferResult(status, reference, "rejected", "insufficient balance")
        return TransferResult("success", reference, "success")

    return fn


def _verify_as(status, amount=None):
    return lambda ref: TransferVerification(status, ref, status, amount)


# --------------------------------------------------------------------------- prepare


def test_prepare_batch_groups_by_marketer_and_skips_held_and_suspended(db_session):
    a, b = _marketer(db_session), _marketer(db_session)
    suspended = _marketer(db_session, MarketerStatus.SUSPENDED)
    a1, a2 = _reward(db_session, a), _reward(db_session, a, amount=250_000)
    b1 = _reward(db_session, b)
    held = _reward(db_session, b, RewardStatus.HELD)
    pending = _reward(db_session, b, RewardStatus.PENDING)
    s1 = _reward(db_session, suspended)

    batch = prepare_batch(db_session, NOW)

    assert batch is not None
    assert batch.status == BatchStatus.DRAFT
    assert batch.period_label == "2026-11-01"
    assert batch.total_kobo == 500_000 + 250_000 + 500_000
    items = {
        i.marketer_id: i
        for i in db_session.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id))
    }
    assert set(items) == {a.id, b.id}
    assert items[a.id].amount_kobo == 750_000
    assert set(items[a.id].reward_ids) == {str(a1.id), str(a2.id)}
    assert items[a.id].status == PENDING
    assert items[b.id].amount_kobo == 500_000
    db_session.expire_all()
    for reward in (a1, a2, b1):
        assert reward.status == RewardStatus.BATCHED and reward.payout_batch_id == batch.id
    for reward, status in ((held, RewardStatus.HELD), (pending, RewardStatus.PENDING)):
        assert reward.status == status and reward.payout_batch_id is None
    assert s1.status == RewardStatus.PAYABLE and s1.payout_batch_id is None


def test_prepare_batch_twice_creates_no_second_batch(db_session):
    _batch_of(db_session, 1)
    assert prepare_batch(db_session, NOW) is None
    assert prepare_batch(db_session, NOW) is None


def test_prepare_batch_returns_none_when_nothing_payable(db_session):
    _reward(db_session, _marketer(db_session), RewardStatus.PENDING)
    assert prepare_batch(db_session, NOW) is None


def test_nothing_moves_without_approval(db_session):
    batch, marketers, rewards = _batch_of(db_session)
    provider = FakeProvider()

    db_session.expire_all()
    assert provider.pay_calls == [] and provider.verify_calls == []
    assert _reload(db_session, batch).status == BatchStatus.DRAFT
    assert all(_item(db_session, batch, m).status == PENDING for m in marketers)
    assert all(_reload(db_session, r).status == RewardStatus.BATCHED for r in rewards)
    # Retrying a draft batch is refused outright and also moves nothing.
    with pytest.raises(PayoutStateError):
        retry_batch(db_session, batch.id, uuid4(), provider)
    assert provider.pay_calls == []


# --------------------------------------------------------------------------- approve


def test_approve_pays_each_item_once_with_the_stable_reference(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    provider = FakeProvider()

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert result.status == BatchStatus.PAID
    assert result.approved_by_admin_id == super_admin_user.id
    assert result.approved_at is not None
    assert sorted((m, amt, ref) for m, amt, ref in provider.pay_calls) == sorted(
        [
            (a.id, 500_000, f"ref-{batch.id}-{a.id}"),
            (b.id, 500_000, f"ref-{batch.id}-{b.id}"),
        ]
    )
    for marketer, reward in ((a, ra), (b, rb)):
        item = _item(db_session, batch, marketer)
        assert item.status == PAID and item.attempt == 1 and item.paid_at is not None
        assert item.transfer_reference == f"ref-{batch.id}-{marketer.id}"
        reward = _reload(db_session, reward)
        assert reward.status == RewardStatus.PAID and reward.paid_at is not None
        assert reward.transfer_reference == item.transfer_reference


def test_approve_twice_transfers_once(db_session, super_admin_user):
    batch, _, _ = _batch_of(db_session)
    provider = FakeProvider()

    approve_batch(db_session, batch.id, super_admin_user.id, provider)
    again = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert again.status == BatchStatus.PAID
    assert len(provider.pay_calls) == 2
    assert provider.verify_calls == []


def test_approve_unknown_batch_raises(db_session, super_admin_user):
    with pytest.raises(payouts.BatchNotFoundError):
        approve_batch(db_session, uuid4(), super_admin_user.id, FakeProvider())


def test_partial_failure_then_retry_pays_only_failed_with_same_reference(
    db_session, super_admin_user
):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    first = FakeProvider(pay_fn=_failing_for(b.id))

    result = approve_batch(db_session, batch.id, super_admin_user.id, first)

    assert result.status == BatchStatus.FAILED
    assert _item(db_session, batch, a).status == PAID
    failed = _item(db_session, batch, b)
    assert failed.status == FAILED and "insufficient" in failed.failure_reason
    assert _reload(db_session, rb).status == RewardStatus.BATCHED

    second = FakeProvider()  # verify -> not_found: the failed call never created a transfer
    result = retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert result.status == BatchStatus.PAID
    assert second.verify_calls == [f"ref-{batch.id}-{b.id}"]
    assert [(m, ref) for m, _, ref in second.pay_calls] == [(b.id, f"ref-{batch.id}-{b.id}")]
    retried = _item(db_session, batch, b)
    assert retried.status == PAID and retried.attempt == 1
    assert _reload(db_session, rb).status == RewardStatus.PAID
    # The paid item was never touched again.
    assert _item(db_session, batch, a).status == PAID
    assert len(first.pay_calls) == 2


def test_batch_not_paid_until_all_items_paid(db_session, super_admin_user):
    batch, (a, b), _ = _batch_of(db_session)
    provider = FakeProvider(pay_fn=_pending_for(b.id))

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert result.status == BatchStatus.PAYING
    assert _item(db_session, batch, a).status == PAID
    pending = _item(db_session, batch, b)
    assert pending.status == PROCESSING  # asynchronous: Paystack will tell us later
    assert _reload(db_session, batch).status == BatchStatus.PAYING

    event = apply_transfer_event(
        db_session, "transfer.success", {"reference": pending.transfer_reference, "amount": 500_000}
    )
    assert event["status"] == "processed"
    assert _reload(db_session, batch).status == BatchStatus.PAID


def test_bank_error_from_the_provider_fails_the_item_not_the_batch_run(
    db_session, super_admin_user
):
    batch, (a, b), _ = _batch_of(db_session)
    provider = FakeProvider(pay_fn=_failing_for(a.id))
    approve_batch(db_session, batch.id, super_admin_user.id, provider)
    assert _item(db_session, batch, a).status == FAILED
    assert _item(db_session, batch, b).status == PAID  # one failure never blocks the others


def test_otp_result_fails_the_item_with_otp_required(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    provider = FakeProvider(pay_fn=lambda m, amt, ref: TransferResult("otp", ref, "otp"))

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    item = _item(db_session, batch, a)
    assert item.status == FAILED and item.failure_reason == "otp_required"
    assert result.status == BatchStatus.FAILED


def test_a_provider_that_raises_leaves_the_item_processing_and_resolvable(
    db_session, super_admin_user
):
    batch, (a, b), _ = _batch_of(db_session)

    def boom(marketer, amount, reference):
        if marketer.id == a.id:
            raise RuntimeError("socket exploded")
        return TransferResult("success", reference, "success")

    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=boom))

    stuck = _item(db_session, batch, a)
    assert stuck.status == PROCESSING and stuck.transfer_reference == f"ref-{batch.id}-{a.id}"
    assert _item(db_session, batch, b).status == PAID
    assert _reload(db_session, batch).status == BatchStatus.PAYING


# --------------------------------------------------------------------------- R8 / R32


def test_suspended_marketer_is_skipped_at_approval_and_rewards_held(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    b.status = MarketerStatus.SUSPENDED  # suspended after the batch was prepared
    db_session.flush()
    provider = FakeProvider()

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert [m for m, _, _ in provider.pay_calls] == [a.id]
    skipped = _item(db_session, batch, b)
    assert skipped.status == SKIPPED and skipped.failure_reason == "marketer_suspended"
    rb = _reload(db_session, rb)
    assert rb.status == RewardStatus.HELD and rb.payout_batch_id is None
    assert "marketer_suspended" in rb.review_reasons
    assert result.status == BatchStatus.PAID  # every non-skipped item is paid
    assert result.total_kobo == 500_000  # skipped items are not part of the total


def test_all_skipped_batch_ends_failed_not_paid(db_session, super_admin_user):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    a.status = MarketerStatus.SUSPENDED
    db_session.flush()
    provider = FakeProvider()

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    # `paid` must always mean money moved: an all-skipped batch is a terminal non-paying `failed`.
    assert result.status == BatchStatus.FAILED
    assert result.total_kobo == 0
    assert provider.pay_calls == []
    assert _item(db_session, batch, a).status == SKIPPED
    assert recompute_batch_status(db_session, batch.id) == BatchStatus.FAILED


def test_reward_held_after_batching_is_excluded_from_the_amount(db_session, super_admin_user):
    marketer = _marketer(db_session)
    t1, t2 = _tenant(db_session), _tenant(db_session)
    r1 = _reward(db_session, marketer, tenant=t1)
    r2 = _reward(db_session, marketer, amount=300_000, tenant=t2)
    batch = prepare_batch(db_session, NOW)
    assert _item(db_session, batch, marketer).amount_kobo == 800_000

    hold_for_dispute(db_session, t2.id)  # the real dispute hold: detaches and holds the reward
    db_session.commit()
    provider = FakeProvider()
    approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert [(a, ref) for _, a, ref in provider.pay_calls] == [(500_000, provider.pay_calls[0][2])]
    item = _item(db_session, batch, marketer)
    assert item.amount_kobo == 500_000 and item.reward_ids == [str(r1.id)]
    assert _reload(db_session, batch).total_kobo == 500_000
    r1, r2 = _reload(db_session, r1), _reload(db_session, r2)
    assert r1.status == RewardStatus.PAID
    assert r2.status == RewardStatus.HELD and r2.paid_at is None and r2.transfer_reference is None


def test_item_with_no_batched_rewards_left_is_skipped(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    rb.status = RewardStatus.VOID  # voided out of band
    rb.payout_batch_id = None
    db_session.flush()
    provider = FakeProvider()

    approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert [m for m, _, _ in provider.pay_calls] == [a.id]
    skipped = _item(db_session, batch, b)
    assert skipped.status == SKIPPED and skipped.failure_reason == "no_batched_rewards"
    assert _reload(db_session, rb).status == RewardStatus.VOID


def test_marketer_suspended_before_a_retry_is_skipped_at_claim(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_failing_for(b.id))
    )
    b = _reload(db_session, b)
    b.status = MarketerStatus.SUSPENDED
    db_session.commit()
    second = FakeProvider()

    result = retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.pay_calls == []
    assert _item(db_session, batch, b).status == SKIPPED
    rb = _reload(db_session, rb)
    assert rb.status == RewardStatus.HELD and rb.payout_batch_id is None
    assert result.status == BatchStatus.PAID  # a is paid, b skipped


# --------------------------------------------------------------------------- retry (R33)


def test_retry_after_a_failed_transfer_that_exists_uses_a_new_reference(
    db_session, super_admin_user
):
    batch, (a,), _ = _batch_of(db_session, 1)
    first_ref = f"ref-{batch.id}-{a.id}"
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_failing_for(a.id))
    )
    second = FakeProvider(verify_fn=_verify_as("failed"))

    retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.verify_calls == [first_ref]
    assert [ref for _, _, ref in second.pay_calls] == [f"{first_ref}-r2"]
    item = _item(db_session, batch, a)
    assert (
        item.status == PAID and item.attempt == 2 and item.transfer_reference == f"{first_ref}-r2"
    )

    # A reversed original transfer is treated the same way, attempt 3.
    assert item_reference(batch.id, a.id, 3) == f"{first_ref}-r3"


def test_retry_whose_verify_says_success_marks_paid_without_paying_again(
    db_session, super_admin_user
):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    first = FakeProvider(pay_fn=_pending_for(a.id))
    approve_batch(db_session, batch.id, super_admin_user.id, first)
    assert _item(db_session, batch, a).status == PROCESSING
    second = FakeProvider(verify_fn=_verify_as("success", amount=500_000))

    result = retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.pay_calls == [] and len(first.pay_calls) == 1
    assert _item(db_session, batch, a).status == PAID
    assert _reload(db_session, ra).status == RewardStatus.PAID
    assert result.status == BatchStatus.PAID


def test_retry_whose_verify_success_amount_differs_does_not_mark_paid(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    second = FakeProvider(verify_fn=_verify_as("success", amount=1))

    retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.pay_calls == []
    assert _item(db_session, batch, a).status == PROCESSING


def test_retry_never_pays_an_item_whose_verify_says_pending(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    first = FakeProvider(pay_fn=_pending_for(a.id))
    approve_batch(db_session, batch.id, super_admin_user.id, first)
    second = FakeProvider(verify_fn=_verify_as("pending"))

    result = retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.pay_calls == []
    assert _item(db_session, batch, a).status == PROCESSING
    assert result.status == BatchStatus.PAYING


def test_retry_with_unreadable_verify_leaves_the_item_untouched(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_failing_for(a.id))
    )

    def cannot_tell(reference):
        raise PayoutProviderError("timeout")

    second = FakeProvider(verify_fn=cannot_tell)
    retry_batch(db_session, batch.id, super_admin_user.id, second)

    assert second.pay_calls == []
    assert _item(db_session, batch, a).status == FAILED


def test_fresh_processing_claim_is_not_resent_on_not_found_but_a_stale_one_is(
    db_session, super_admin_user
):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    ref = f"ref-{batch.id}-{a.id}"

    fresh = FakeProvider()  # verify -> not_found, but the claim is seconds old
    retry_batch(db_session, batch.id, super_admin_user.id, fresh)
    assert fresh.pay_calls == []
    assert _item(db_session, batch, a).status == PROCESSING

    item = _item(db_session, batch, a)
    item.claimed_at = datetime.now(UTC) - payouts.STALE_PROCESSING_AFTER - timedelta(minutes=1)
    db_session.commit()
    stale = FakeProvider()  # crash between claim and call: nothing exists, same reference
    retry_batch(db_session, batch.id, super_admin_user.id, stale)
    assert [r for _, _, r in stale.pay_calls] == [ref]
    assert _item(db_session, batch, a).status == PAID


def test_retry_claims_pending_items_an_interrupted_approval_never_reached(
    db_session, super_admin_user
):
    batch, (a,), _ = _batch_of(db_session, 1)
    batch.status = BatchStatus.PAYING  # approved, then the process died before the first claim
    db_session.commit()
    provider = FakeProvider()

    result = retry_batch(db_session, batch.id, super_admin_user.id, provider)

    assert provider.verify_calls == []  # nothing was ever sent, so nothing to verify
    assert [r for _, _, r in provider.pay_calls] == [f"ref-{batch.id}-{a.id}"]
    assert result.status == BatchStatus.PAID


def test_retry_of_a_paid_batch_is_a_noop(db_session, super_admin_user):
    batch, _, _ = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())
    provider = FakeProvider()
    assert retry_batch(db_session, batch.id, super_admin_user.id, provider).status == (
        BatchStatus.PAID
    )
    assert provider.pay_calls == [] and provider.verify_calls == []


# --------------------------------------------------------------------------- manual (R34)


def test_manual_provider_makes_items_awaiting_manual_and_moves_no_money(
    db_session, super_admin_user
):
    batch, (a, b), (ra, rb) = _batch_of(db_session)

    result = approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())

    assert result.status == BatchStatus.PAYING
    assert _item(db_session, batch, a).status == AWAITING
    assert _item(db_session, batch, b).status == AWAITING
    assert _reload(db_session, ra).status == RewardStatus.BATCHED
    assert _reload(db_session, ra).paid_at is None


def test_manual_mark_paid_records_reference(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())

    result = mark_paid(
        db_session,
        batch.id,
        [(a.id, "BANK-REF-0001", 500_000), (b.id, "BANK-REF-0002", 500_000)],
        super_admin_user.id,
    )

    assert result.status == BatchStatus.PAID
    for marketer, reward, ref in ((a, ra, "BANK-REF-0001"), (b, rb, "BANK-REF-0002")):
        item = _item(db_session, batch, marketer)
        assert item.status == PAID and item.transfer_reference == ref and item.paid_at is not None
        reward = _reload(db_session, reward)
        assert reward.status == RewardStatus.PAID and reward.transfer_reference == ref


def test_mark_paid_partial_leaves_the_batch_paying(db_session, super_admin_user):
    batch, (a, b), _ = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    result = mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert result.status == BatchStatus.PAYING
    assert _item(db_session, batch, b).status == AWAITING


def test_mark_paid_refuses_processing_pending_and_paid_items(db_session, super_admin_user):
    batch, (a, b, c), (ra, rb, rc) = _batch_of(db_session, 3)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=_pending_for(a.id)),  # a -> processing; b, c -> paid
    )
    pending = _item(db_session, batch, c)
    pending.status = PENDING
    pending.transfer_reference = None
    db_session.commit()

    for marketer, state in ((a, PROCESSING), (b, PAID), (c, PENDING)):
        with pytest.raises(PayoutStateError):
            mark_paid(
                db_session, batch.id, [(marketer.id, "BANK-REF-X", 500_000)], super_admin_user.id
            )
        item = _item(db_session, batch, marketer)
        assert item.status == state and item.transfer_reference != "BANK-REF-X"
    assert _reload(db_session, ra).status == RewardStatus.BATCHED
    assert _reload(db_session, rb).transfer_reference != "BANK-REF-X"


def test_mark_paid_is_all_or_nothing(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(b.id))
    )  # a paid by transfer, b processing

    with pytest.raises(PayoutStateError):
        mark_paid(
            db_session,
            batch.id,
            [(a.id, "BANK-REF-A", 500_000), (b.id, "BANK-REF-B", 500_000)],
            super_admin_user.id,
        )
    assert _item(db_session, batch, a).transfer_reference == f"ref-{batch.id}-{a.id}"


def test_mark_paid_validates_input(db_session, super_admin_user):
    batch, (a, b), _ = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    for entries in (
        [],
        [(a.id, "  ", 500_000)],
        [(a.id, "x" * 101, 500_000)],
        [(a.id, "REF1", 500_000), (a.id, "REF2", 500_000)],
        [(a.id, "SAME", 500_000), (b.id, "SAME", 500_000)],
    ):
        with pytest.raises(PayoutStateError):
            mark_paid(db_session, batch.id, entries, super_admin_user.id)
    with pytest.raises(PayoutItemNotFoundError):
        mark_paid(db_session, batch.id, [(uuid4(), "REF-NOPE", 500_000)], super_admin_user.id)
    assert _item(db_session, batch, a).status == AWAITING


def test_mark_paid_refuses_a_bank_reference_already_used_by_another_item(
    db_session, super_admin_user
):
    batch, (a, b), _ = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    mark_paid(db_session, batch.id, [(a.id, "BANK-REF-SAME", 500_000)], super_admin_user.id)
    with pytest.raises(PayoutStateError):
        mark_paid(db_session, batch.id, [(b.id, "BANK-REF-SAME", 500_000)], super_admin_user.id)
    assert _item(db_session, batch, b).status == AWAITING


def test_mark_paid_on_a_draft_batch_is_refused(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    with pytest.raises(PayoutStateError):
        mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)


def test_mark_paid_on_a_failed_item_needs_the_transfer_verified_as_not_successful(
    db_session, super_admin_user
):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_failing_for(a.id))
    )
    ref = f"ref-{batch.id}-{a.id}"

    def cannot_tell(reference):
        raise PayoutProviderError("down")

    refusals = [
        FakeProvider(verify_fn=_verify_as("success")),  # it actually went through
        FakeProvider(verify_fn=_verify_as("pending")),  # may still go through
        FakeProvider(verify_fn=cannot_tell),  # cannot confirm failure
        ManualPayoutProvider(),  # a provider that cannot verify at all
    ]
    for provider in refusals:
        with pytest.raises(PayoutStateError):
            mark_paid(
                db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id, provider
            )
        assert _item(db_session, batch, a).status == FAILED
    assert _reload(db_session, ra).status == RewardStatus.BATCHED

    ok = FakeProvider(verify_fn=_verify_as("failed"))
    result = mark_paid(
        db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id, ok
    )
    assert ok.verify_calls == [ref]
    assert result.status == BatchStatus.PAID
    assert _item(db_session, batch, a).transfer_reference == "BANK-REF-A"
    assert _reload(db_session, ra).status == RewardStatus.PAID


def test_otp_failed_item_can_fall_back_to_manual_only_once_the_transfer_is_dead(
    db_session, super_admin_user
):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=lambda m, amt, ref: TransferResult("otp", ref, "otp")),
    )
    with pytest.raises(PayoutStateError):  # Paystack still holds it waiting for an OTP
        mark_paid(
            db_session,
            batch.id,
            [(a.id, "BANK-REF-A", 500_000)],
            super_admin_user.id,
            FakeProvider(verify_fn=_verify_as("pending")),
        )
    mark_paid(
        db_session,
        batch.id,
        [(a.id, "BANK-REF-A", 500_000)],
        super_admin_user.id,
        FakeProvider(verify_fn=_verify_as("failed")),
    )
    assert _item(db_session, batch, a).status == PAID


# --------------------------------------------------------------------------- transfer events


def _paid_batch(db_session, admin, count=1):
    batch, marketers, rewards = _batch_of(db_session, count)
    approve_batch(db_session, batch.id, admin.id, FakeProvider())
    return batch, marketers, rewards


def test_apply_transfer_event_success_is_idempotent_on_replay(db_session, super_admin_user):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    ref = f"ref-{batch.id}-{a.id}"
    payload = {"reference": ref, "amount": 500_000, "status": "success"}

    first = apply_transfer_event(db_session, "transfer.success", payload)
    paid_at = _reload(db_session, ra).paid_at
    second = apply_transfer_event(db_session, "transfer.success", payload)

    assert first["status"] == "processed" and first["item_status"] == "paid"
    assert second["status"] == "duplicate"
    reward = _reload(db_session, ra)
    assert reward.status == RewardStatus.PAID and reward.paid_at == paid_at
    assert reward.transfer_reference == ref


def test_apply_transfer_event_ignores_unknown_reference_and_bad_payloads(db_session):
    for event, data in (
        ("transfer.success", {"reference": "ref-unknown-reference-0000"}),
        ("transfer.failed", {"reference": "ref-unknown-reference-0000"}),
        ("transfer.reversed", {"reference": "ref-unknown-reference-0000"}),
        ("transfer.success", {}),
        ("transfer.success", None),
        ("transfer.success", {"reference": 123}),
        ("transfer.unknown", {"reference": "x"}),
    ):
        result = apply_transfer_event(db_session, event, data)
        assert result["status"] == "ignored", (event, data)


def test_apply_transfer_event_refuses_a_success_with_a_different_amount(
    db_session, super_admin_user
):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    result = apply_transfer_event(
        db_session, "transfer.success", {"reference": f"ref-{batch.id}-{a.id}", "amount": 1}
    )
    assert result["status"] == "ignored" and result["reason"] == "amount_mismatch"
    assert _item(db_session, batch, a).status == PROCESSING


def test_transfer_failed_event_fails_a_processing_item_and_never_unpays(
    db_session, super_admin_user
):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    ref_a, ref_b = f"ref-{batch.id}-{a.id}", f"ref-{batch.id}-{b.id}"

    result = apply_transfer_event(
        db_session, "transfer.failed", {"reference": ref_a, "message": "Account closed"}
    )
    assert result["status"] == "processed"
    item = _item(db_session, batch, a)
    assert item.status == FAILED and item.failure_reason == "Account closed"
    assert _reload(db_session, ra).status == RewardStatus.BATCHED
    assert _reload(db_session, batch).status == BatchStatus.FAILED
    assert apply_transfer_event(db_session, "transfer.failed", {"reference": ref_a})["status"] == (
        "duplicate"
    )

    # A failed event for an item that is already paid must not undo the payment.
    ignored = apply_transfer_event(db_session, "transfer.failed", {"reference": ref_b})
    assert ignored["status"] == "ignored"
    assert _item(db_session, batch, b).status == PAID
    assert _reload(db_session, rb).status == RewardStatus.PAID


def test_late_success_after_an_otp_failure_still_pays_the_item(db_session, super_admin_user):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=lambda m, amt, ref: TransferResult("otp", ref, "otp")),
    )
    assert _item(db_session, batch, a).status == FAILED
    # The admin finalised the OTP transfer in the Paystack dashboard: the money did go out.
    apply_transfer_event(
        db_session, "transfer.success", {"reference": f"ref-{batch.id}-{a.id}", "amount": 500_000}
    )
    assert _item(db_session, batch, a).status == PAID
    assert _reload(db_session, ra).status == RewardStatus.PAID
    assert _reload(db_session, batch).status == BatchStatus.PAID


def test_reversed_event_unpays_the_item_and_a_retry_pays_it_again(db_session, super_admin_user):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())
    ref = f"ref-{batch.id}-{a.id}"
    assert _reload(db_session, batch).status == BatchStatus.PAID

    result = apply_transfer_event(db_session, "transfer.reversed", {"reference": ref})

    assert result["status"] == "processed" and result["item_status"] == "failed"
    item = _item(db_session, batch, a)
    assert item.status == FAILED and item.failure_reason == "reversed" and item.paid_at is None
    reward = _reload(db_session, ra)
    assert reward.status == RewardStatus.BATCHED and reward.payout_batch_id == batch.id
    assert reward.paid_at is None and reward.transfer_reference is None
    assert _reload(db_session, batch).status == BatchStatus.FAILED
    assert _reload(db_session, rb).status == RewardStatus.PAID  # the other item is untouched
    assert apply_transfer_event(db_session, "transfer.reversed", {"reference": ref})["status"] == (
        "duplicate"
    )

    retry = FakeProvider(verify_fn=_verify_as("reversed"))
    retry_batch(db_session, batch.id, super_admin_user.id, retry)
    assert [r for _, _, r in retry.pay_calls] == [f"{ref}-r2"]
    assert _reload(db_session, ra).status == RewardStatus.PAID
    assert _reload(db_session, batch).status == BatchStatus.PAID


def test_reward_voided_while_its_transfer_was_in_flight_is_still_marked_paid(
    db_session, super_admin_user
):
    """The money for a captured reward has gone out: leaving it releasable would pay it twice."""
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    ra = _reload(db_session, ra)
    ra.status = RewardStatus.HELD  # a dispute landed after the claim (the link is kept: R38)
    db_session.commit()

    apply_transfer_event(
        db_session, "transfer.success", {"reference": f"ref-{batch.id}-{a.id}", "amount": 500_000}
    )

    ra = _reload(db_session, ra)
    assert ra.status == RewardStatus.PAID and "paid_in_flight" in ra.review_reasons


# --------------------------------------------------------------------------- Paystack provider


class PaystackStub:
    """Records every request the Paystack service makes and answers like Paystack would."""

    def __init__(self, transfer_status="pending"):
        self.requests: list[httpx.Request] = []
        self.transfer_status = transfer_status
        self.recipient_codes = iter(f"RCP_stub{n:011d}" for n in range(1, 50))
        self.fail_transfer: httpx.Response | Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/transferrecipient":
            return httpx.Response(
                201,
                json={
                    "status": True,
                    "message": "Transfer recipient created successfully",
                    "data": {"recipient_code": next(self.recipient_codes)},
                },
            )
        if path == "/transfer":
            if isinstance(self.fail_transfer, Exception):
                raise self.fail_transfer
            if self.fail_transfer is not None:
                return self.fail_transfer
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Transfer has been queued",
                    "data": {
                        "status": self.transfer_status,
                        "reference": body["reference"],
                        "transfer_code": "TRF_stub",
                        "amount": body["amount"],
                    },
                },
            )
        return httpx.Response(404, json={"status": False, "message": "Route not found"})

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


@pytest.fixture
def paystack(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "PAYSTACK_BASE_URL", "https://paystack.test")
    stub = PaystackStub()
    service = PaystackService(db_session, transport=httpx.MockTransport(stub))
    return stub, service


def test_paystack_provider_builds_expected_requests(db_session, paystack, super_admin_user):
    stub, service = paystack
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    provider = PaystackTransferProvider(service=service)
    log: list[str] = []
    sink = logger.add(lambda message: log.append(str(message)), level="DEBUG")

    try:
        result = approve_batch(db_session, batch.id, super_admin_user.id, provider)
    finally:
        logger.remove(sink)

    assert [(r.method, r.url.path) for r in stub.requests] == [
        ("POST", "/transferrecipient"),
        ("POST", "/transfer"),
    ]
    recipient_body = stub.bodies("/transferrecipient")[0]
    assert recipient_body == {
        "type": "nuban",
        "name": "Placeholder Name",
        "account_number": "0000000000",
        "bank_code": "000",
        "currency": "NGN",
    }
    transfer_body = stub.bodies("/transfer")[0]
    code = _reload(db_session, a).paystack_recipient_code
    assert code and code.startswith("RCP_")
    assert transfer_body == {
        "source": "balance",
        "amount": 500_000,  # kobo
        "recipient": code,
        "reference": f"ref-{batch.id}-{a.id}",  # stable per (batch, marketer)
        "reason": "Luran referral payout 2026-11-01",  # period label only: no names, no bank data
        "currency": "NGN",
    }
    for request in stub.requests:
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert request.url.host == "paystack.test"
    # Paystack said "queued": the item waits for the webhook, nothing is marked paid yet.
    assert result.status == BatchStatus.PAYING
    assert _item(db_session, batch, a).status == PROCESSING
    assert _reload(db_session, ra).status == RewardStatus.BATCHED
    # The secret, the account number and the recipient code never reach the logs.
    joined = "\n".join(log)
    assert SECRET not in joined
    assert "0000000000" not in joined and code not in joined


def test_recipient_is_created_once_and_recreated_after_bank_details_change(db_session, paystack):
    stub, service = paystack
    marketer = _marketer(db_session)
    provider = PaystackTransferProvider(service=service)

    provider.pay(marketer, 500_000, f"ref-{uuid4()}-{marketer.id}")
    provider.pay(marketer, 500_000, f"ref-{uuid4()}-{marketer.id}")
    assert len(stub.bodies("/transferrecipient")) == 1
    assert len(stub.bodies("/transfer")) == 2
    first_code = stub.bodies("/transfer")[0]["recipient"]
    assert stub.bodies("/transfer")[1]["recipient"] == first_code

    MarketerService(db_session).update_bank_details(
        marketer.id, "057", "0000000001", "Placeholder Name"
    )
    assert _reload(db_session, marketer).paystack_recipient_code is None
    provider.pay(_reload(db_session, marketer), 500_000, f"ref-{uuid4()}-{marketer.id}")

    assert len(stub.bodies("/transferrecipient")) == 2
    assert stub.bodies("/transferrecipient")[1]["account_number"] == "0000000001"
    assert stub.bodies("/transfer")[2]["recipient"] != first_code


def test_paystack_provider_fails_without_bank_details_and_sends_nothing(db_session, paystack):
    stub, service = paystack
    marketer = _marketer(db_session, bank=False)
    result = PaystackTransferProvider(service=service).pay(marketer, 500_000, "ref-" + "a" * 20)
    assert result.status == "failed" and result.message == "bank_details_missing"
    assert stub.requests == []


def test_paystack_provider_maps_outcomes(db_session, paystack):
    stub, service = paystack
    provider = PaystackTransferProvider(service=service)
    reference = f"ref-{uuid4()}-{uuid4()}"

    for transfer_status, expected in (
        ("success", "success"),
        ("pending", "pending"),
        ("otp", "otp"),
        ("failed", "failed"),
        ("received", "pending"),
    ):
        stub.transfer_status = transfer_status
        marketer = _marketer(db_session)
        assert provider.pay(marketer, 100, reference).status == expected, transfer_status

    # Paystack refusing (HTTP 4xx) is definitive: the transfer was not created.
    stub.fail_transfer = httpx.Response(400, json={"status": False, "message": "Insufficient"})
    refused = provider.pay(_marketer(db_session), 100, reference)
    assert refused.status == "failed" and refused.provider_status == "rejected"

    # A timeout or a 5xx is ambiguous: never report failure, leave it for verify.
    for ambiguous in (httpx.ReadTimeout("slow"), httpx.Response(502, text="bad gateway")):
        stub.fail_transfer = ambiguous
        unknown = provider.pay(_marketer(db_session), 100, reference)
        assert unknown.status == "pending" and unknown.provider_status == "unconfirmed"


def test_paystack_recipient_failure_sends_no_transfer(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", SECRET)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(422, json={"status": False, "message": "Could not resolve account"})

    service = PaystackService(db_session, transport=httpx.MockTransport(handler))
    result = PaystackTransferProvider(service=service).pay(
        _marketer(db_session), 100, "ref-" + "b" * 20
    )
    assert result.status == "failed" and result.message == "recipient_error"
    assert [r.url.path for r in requests] == ["/transferrecipient"]


def test_paystack_verify_maps_statuses_and_only_a_real_not_found_is_not_found(
    db_session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", SECRET)
    answers: dict[str, httpx.Response] = {}
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return answers[request.url.path.rsplit("/", 1)[1]]

    def ok(status, amount=500_000):
        return httpx.Response(
            200,
            json={"status": True, "data": {"status": status, "reference": "r", "amount": amount}},
        )

    answers.update(
        {
            "succ": ok("success"),
            "fail": ok("failed"),
            "rev": ok("reversed"),
            "pend": ok("pending"),
            "otp": ok("otp"),
            "weird": ok("something-new"),
            "gone": httpx.Response(404, json={"status": False, "message": "Transfer not found"}),
            "wrongroute": httpx.Response(404, text="<html>nginx</html>"),
            "boom": httpx.Response(500, text="oops"),
            "denied": httpx.Response(401, json={"status": False, "message": "Invalid key"}),
        }
    )
    provider = PaystackTransferProvider(
        service=PaystackService(db_session, transport=httpx.MockTransport(handler))
    )

    assert provider.verify("succ").status == "success"
    assert provider.verify("succ").amount_kobo == 500_000
    assert provider.verify("fail").status == "failed"
    assert provider.verify("rev").status == "reversed"
    assert provider.verify("pend").status == "pending"
    assert provider.verify("otp").status == "pending"
    assert provider.verify("gone").status == "not_found"
    unknown = provider.verify("weird")  # R39: neither failed nor "does not exist"
    assert unknown.status == "pending" and unknown.provider_status == "something-new"
    for unreadable in ("wrongroute", "boom", "denied"):
        with pytest.raises(PayoutProviderError):
            provider.verify(unreadable)  # never mistaken for "no transfer exists"
    assert all(r.method == "GET" for r in seen)
    assert seen[0].url.path == "/transfer/verify/succ"
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET}"


def test_paystack_service_rejects_an_invalid_reference_or_amount(db_session, paystack):
    _, service = paystack
    for reference in ("short", "UPPERCASE-REFERENCE-0000", "has space in it 0000000"):
        with pytest.raises(ValueError):
            service.initiate_transfer(
                amount_kobo=100, recipient_code="RCP_x", reference=reference, reason="r"
            )
    with pytest.raises(ValueError):
        service.initiate_transfer(
            amount_kobo=0, recipient_code="RCP_x", reference="ref-" + "c" * 20, reason="r"
        )


def test_paystack_transfer_error_classification(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", SECRET)

    def make(response):
        return PaystackService(db_session, transport=httpx.MockTransport(lambda r: response))

    kwargs = {
        "amount_kobo": 100,
        "recipient_code": "RCP_x",
        "reference": "ref-" + "d" * 20,
        "reason": "r",
    }
    with pytest.raises(PaystackTransferError) as exc:
        make(httpx.Response(400, json={"status": False, "message": "no"})).initiate_transfer(
            **kwargs
        )
    assert exc.value.definitive is True
    with pytest.raises(PaystackTransferError) as exc:
        make(httpx.Response(503, text="x")).initiate_transfer(**kwargs)
    assert exc.value.definitive is False
    with pytest.raises(PaystackTransferError) as exc:
        make(httpx.Response(200, text="not json")).initiate_transfer(**kwargs)
    assert exc.value.definitive is False
    assert SECRET not in str(exc.value)


def test_paystack_provider_end_to_end_retry_after_timeout_never_double_sends(
    db_session, paystack, super_admin_user
):
    """Initiate times out (ambiguous) -> item stays processing; verify later says it exists."""
    stub, service = paystack
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    provider = PaystackTransferProvider(service=service)
    stub.fail_transfer = httpx.ReadTimeout("slow")

    approve_batch(db_session, batch.id, super_admin_user.id, provider)
    assert _item(db_session, batch, a).status == PROCESSING
    transfers_before = len(stub.bodies("/transfer"))

    # Paystack in fact created it: verify says pending, and the retry sends nothing.
    class Verifying(PaystackTransferProvider):
        def verify(self, reference):
            return TransferVerification("pending", reference, "pending")

    retry_batch(db_session, batch.id, super_admin_user.id, Verifying(service=service))
    assert len(stub.bodies("/transfer")) == transfers_before
    assert _item(db_session, batch, a).status == PROCESSING


def test_get_payout_provider_follows_the_setting(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "REFERRAL_PAYOUT_PROVIDER", "manual")
    assert isinstance(get_payout_provider(db_session), ManualPayoutProvider)
    monkeypatch.setattr(settings, "REFERRAL_PAYOUT_PROVIDER", "paystack")
    assert isinstance(get_payout_provider(db_session), PaystackTransferProvider)


# --------------------------------------------------------------------------- BigInteger


def test_kobo_columns_are_bigint_and_hold_more_than_2_pow_31(db_session):
    for column in (
        PayoutBatch.__table__.c.total_kobo,
        PayoutItem.__table__.c.amount_kobo,
        ReferralReward.__table__.c.amount_kobo,
    ):
        assert isinstance(column.type, BigInteger)
    marketer = _marketer(db_session)
    big = 3_000_000_000
    batch = PayoutBatch(period_label="2026-11-15", total_kobo=big)
    db_session.add(batch)
    db_session.flush()
    db_session.add(PayoutItem(batch_id=batch.id, marketer_id=marketer.id, amount_kobo=big))
    reward = _reward(db_session, marketer, amount=big)
    db_session.flush()
    db_session.expire_all()
    assert db_session.get(PayoutBatch, batch.id).total_kobo == big
    assert db_session.get(ReferralReward, reward.id).amount_kobo == big
    assert (
        db_session.scalar(select(PayoutItem.amount_kobo).where(PayoutItem.batch_id == batch.id))
        == big
    )


# --------------------------------------------------------------------------- real concurrency


def _run_in_threads(targets):
    outcomes: list = [None] * len(targets)

    def wrap(index, fn):
        try:
            outcomes[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the asserting test
            outcomes[index] = exc

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "worker thread hung"
    return outcomes


@pytest.fixture
def committed_batch(pg_engine):
    """A committed draft batch with 3 marketers, created and cleaned up with throwaway sessions."""
    factory = sessionmaker(bind=pg_engine)
    session = factory()
    try:
        marketers = [_marketer(session) for _ in range(3)]
        for marketer in marketers:
            _reward(session, marketer)
        session.commit()
        batch = prepare_batch(session, NOW)
        marketer_ids = [m.id for m in marketers]
        batch_id = batch.id
    finally:
        session.close()
    yield factory, batch_id, marketer_ids
    cleanup = factory()
    try:
        cleanup.execute(text("DELETE FROM payout_items WHERE batch_id = :b"), {"b": batch_id})
        cleanup.execute(
            text("DELETE FROM referral_rewards WHERE marketer_id = ANY(:m)"), {"m": marketer_ids}
        )
        cleanup.execute(text("DELETE FROM payout_batches WHERE id = :b"), {"b": batch_id})
        cleanup.execute(text("DELETE FROM marketers WHERE id = ANY(:m)"), {"m": marketer_ids})
        cleanup.commit()
    finally:
        cleanup.close()


def test_two_concurrent_approvals_call_the_provider_once_per_item(committed_batch):
    factory, batch_id, marketer_ids = committed_batch
    provider = FakeProvider(delay=0.15)  # widen the window in which a second worker could slip in
    barrier = threading.Barrier(2, timeout=15)

    def approve():
        session = factory()
        try:
            barrier.wait()
            return approve_batch(session, batch_id, None, provider).status
        finally:
            session.close()

    outcomes = _run_in_threads([approve, approve])

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    assert sorted(m for m, _, _ in provider.pay_calls) == sorted(marketer_ids)
    assert len({ref for _, _, ref in provider.pay_calls}) == 3
    check = factory()
    try:
        items = check.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch_id)).all()
        assert [i.status for i in items] == [PAID] * 3
        assert check.get(PayoutBatch, batch_id).status == BatchStatus.PAID
    finally:
        check.close()


def test_two_concurrent_retries_of_a_stale_processing_item_send_once(committed_batch):
    factory, batch_id, marketer_ids = committed_batch
    first = FakeProvider(pay_fn=lambda m, a, r: TransferResult("pending", r, "unconfirmed"))
    session = factory()
    try:
        approve_batch(session, batch_id, None, first)
        stale = datetime.now(UTC) - timedelta(hours=1)
        session.execute(
            text("UPDATE payout_items SET claimed_at = :t WHERE batch_id = :b"),
            {"t": stale, "b": batch_id},
        )
        session.commit()
    finally:
        session.close()
    assert len(first.pay_calls) == 3

    second = FakeProvider(delay=0.1)  # verify -> not_found for every item: crash-after-claim case
    barrier = threading.Barrier(2, timeout=15)

    def retry():
        s = factory()
        try:
            barrier.wait()
            return retry_batch(s, batch_id, None, second).status
        finally:
            s.close()

    outcomes = _run_in_threads([retry, retry])

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    assert sorted(m for m, _, _ in second.pay_calls) == sorted(marketer_ids)  # once each, not twice
    check = factory()
    try:
        assert check.get(PayoutBatch, batch_id).status == BatchStatus.PAID
    finally:
        check.close()


# --------------------------------------------------------------------------- fix round 1 (R37-R39)


def _attribute(db, tenant, marketer, *, disputed=False):
    attribution = ReferralAttribution(
        tenant_id=tenant.id,
        marketer_id=marketer.id,
        source=AttributionSource.COOKIE,
        disputed_at=NOW if disputed else None,
    )
    db.add(attribution)
    db.flush()
    return attribution


def _processing(db, admin, marketer_ids=None, rewards_per=1):
    """A marketer with ``rewards_per`` tenant-backed rewards whose item is left ``processing``."""
    marketer = _marketer(db)
    tenants = [_tenant(db) for _ in range(rewards_per)]
    rewards = [_reward(db, marketer, tenant=t) for t in tenants]
    batch = prepare_batch(db, NOW)
    approve_batch(db, batch.id, admin.id, FakeProvider(pay_fn=_pending_for(marketer.id)))
    assert _item(db, batch, marketer).status == PROCESSING
    return batch, marketer, tenants, rewards


@pytest.fixture
def mails(monkeypatch):
    """Every email EmailService would put on the wire (rendered, not sent)."""
    from types import SimpleNamespace

    from app.services.email_service import EmailService

    box: list = []

    def fake_send(self, to_email, subject, html_content, text_content=None, attachments=None):
        box.append(SimpleNamespace(to=to_email, subject=subject, html=html_content))
        return True

    monkeypatch.setattr(EmailService, "_send_email", fake_send)
    return box


def test_r49_dispute_on_an_awaiting_manual_item_keeps_the_link_and_release_is_refused(
    db_session, super_admin_user
):
    """The admin may already have sent the money at the bank: the reward stays tied to the item."""
    m, t = _marketer(db_session), _tenant(db_session)
    r = _reward(db_session, m, tenant=t)
    b1 = prepare_batch(db_session, NOW)
    approve_batch(db_session, b1.id, super_admin_user.id, ManualPayoutProvider())
    assert _item(db_session, b1, m).status == AWAITING

    hold_for_dispute(db_session, t.id)
    db_session.commit()

    held = _reload(db_session, r)
    assert held.status == RewardStatus.HELD and held.payout_batch_id == b1.id
    item = _item(db_session, b1, m)
    assert item.status == AWAITING and item.amount_kobo == 500_000
    assert item.reward_ids == [str(r.id)]
    assert _reload(db_session, b1).status == BatchStatus.PAYING
    for action in (
        lambda: release(db_session, r.id, super_admin_user.id, "n", now=NOW),
        lambda: void(db_session, r.id, super_admin_user.id, "n"),
    ):
        with pytest.raises(RewardInFlightError):
            action()
    assert prepare_batch(db_session, NOW) is None  # so it can never be batched and paid again


def test_r49_the_old_double_payment_sequence_can_no_longer_pay_a_reward_twice(
    db_session, super_admin_user, mails
):
    """Dispute lands after the admin paid at the bank; the amount they record is the claimed one."""
    m, t = _marketer(db_session), _tenant(db_session)
    r = _reward(db_session, m, tenant=t)
    b1 = prepare_batch(db_session, NOW)
    approve_batch(db_session, b1.id, super_admin_user.id, ManualPayoutProvider())
    hold_for_dispute(db_session, t.id)
    db_session.commit()
    with pytest.raises(RewardInFlightError):
        release(db_session, r.id, super_admin_user.id, "dispute irrelevant", now=NOW)
    assert prepare_batch(db_session, NOW) is None

    # The recorded payment is the one the admin actually made (the claimed amount).
    mark_paid(db_session, b1.id, [(m.id, "BANK-1", 500_000)], super_admin_user.id)

    paid = _reload(db_session, r)
    assert paid.status == RewardStatus.PAID and paid.transfer_reference == "BANK-1"
    assert "paid_in_flight" in paid.review_reasons
    assert _item(db_session, b1, m).status == PAID
    assert _reload(db_session, b1).status == BatchStatus.PAID
    assert len([x for x in mails if x.to == m.email and "BANK-1" in x.html]) == 1
    # Nothing is left that could be released, re-batched and paid a second time.
    assert prepare_batch(db_session, NOW) is None
    with pytest.raises(RewardStateError):
        release(db_session, r.id, super_admin_user.id, "n", now=NOW)
    refs = db_session.scalars(
        select(ReferralReward.transfer_reference).where(ReferralReward.marketer_id == m.id)
    ).all()
    assert refs == ["BANK-1"]  # one reward, paid by exactly one transfer reference


def _awaiting_two_rewards_one_disputed(db, admin):
    """An awaiting_manual item of 800_000 (r1 500_000 + r2 300_000); r2 is disputed afterwards."""
    m = _marketer(db)
    t1, t2 = _tenant(db), _tenant(db)
    r1 = _reward(db, m, tenant=t1)
    r2 = _reward(db, m, tenant=t2, amount=300_000)
    batch = prepare_batch(db, NOW)
    approve_batch(db, batch.id, admin.id, ManualPayoutProvider())
    hold_for_dispute(db, t2.id)
    db.commit()
    assert _reload(db, r2).status == RewardStatus.HELD
    assert _reload(db, r2).payout_batch_id == batch.id
    return batch, m, r1, r2


def test_r49_mark_paid_with_the_claimed_amount_pays_the_claimed_set_once(
    db_session, super_admin_user, mails
):
    batch, m, r1, r2 = _awaiting_two_rewards_one_disputed(db_session, super_admin_user)

    result = mark_paid(db_session, batch.id, [(m.id, "BANK-CLAIMED", 800_000)], super_admin_user.id)

    item = _item(db_session, batch, m)
    assert item.status == PAID and item.amount_kobo == 800_000
    assert sorted(item.reward_ids) == sorted([str(r1.id), str(r2.id)])
    assert item.transfer_reference == "BANK-CLAIMED"
    p1, p2 = _reload(db_session, r1), _reload(db_session, r2)
    assert p1.status == p2.status == RewardStatus.PAID
    assert p1.transfer_reference == p2.transfer_reference == "BANK-CLAIMED"
    assert "paid_in_flight" not in (p1.review_reasons or [])
    assert "paid_in_flight" in p2.review_reasons  # held after the claim, paid with the item
    assert result.status == BatchStatus.PAID
    assert len([x for x in mails if x.to == m.email]) == 1  # one payout notice
    assert prepare_batch(db_session, NOW) is None


def test_r49_mark_paid_with_the_recomputed_amount_pays_that_set_and_detaches_the_held_one(
    db_session, super_admin_user, mails
):
    batch, m, r1, r2 = _awaiting_two_rewards_one_disputed(db_session, super_admin_user)

    mark_paid(db_session, batch.id, [(m.id, "BANK-CURRENT", 500_000)], super_admin_user.id)

    item = _item(db_session, batch, m)
    assert item.status == PAID and item.amount_kobo == 500_000
    assert item.reward_ids == [str(r1.id)]
    assert _reload(db_session, r1).status == RewardStatus.PAID
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    assert held.paid_at is None and held.transfer_reference is None
    assert len([x for x in mails if x.to == m.email]) == 1


def test_r49_mark_paid_with_a_third_amount_is_refused_with_both_amounts(
    db_session, super_admin_user, mails
):
    batch, m, r1, r2 = _awaiting_two_rewards_one_disputed(db_session, super_admin_user)

    with pytest.raises(payouts.AmountMismatchError) as exc:
        mark_paid(db_session, batch.id, [(m.id, "BANK-X", 700_000)], super_admin_user.id)

    assert exc.value.current_amount_kobo == 500_000
    assert exc.value.claimed_amount_kobo == 800_000
    assert _item(db_session, batch, m).status == AWAITING
    assert _reload(db_session, r1).status == RewardStatus.BATCHED
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id == batch.id
    assert mails == []


def test_r49_a_pending_item_still_detaches_and_refreshes_on_dispute(db_session):
    """Only `pending` items (never exposed to the admin as payable) detach and refresh."""
    m = _marketer(db_session)
    t1, t2 = _tenant(db_session), _tenant(db_session)
    _reward(db_session, m, tenant=t1)
    r2 = _reward(db_session, m, tenant=t2, amount=300_000)
    batch = prepare_batch(db_session, NOW)
    hold_for_dispute(db_session, t2.id)
    db_session.commit()
    assert _item(db_session, batch, m).amount_kobo == 500_000
    assert _reload(db_session, r2).payout_batch_id is None


def test_c1_dispute_on_a_processing_item_keeps_the_link_and_release_is_refused(
    db_session, super_admin_user
):
    """Probe 2: with money possibly in flight the reward stays tied to its batch."""
    batch, m, (t,), (r,) = _processing(db_session, super_admin_user)
    ref = _item(db_session, batch, m).transfer_reference

    hold_for_dispute(db_session, t.id)
    db_session.commit()
    held = _reload(db_session, r)
    assert held.status == RewardStatus.HELD and held.payout_batch_id == batch.id
    assert _item(db_session, batch, m).status == PROCESSING

    for action in (
        lambda: release(db_session, r.id, super_admin_user.id, "n", now=NOW),
        lambda: void(db_session, r.id, super_admin_user.id, "n"),
    ):
        with pytest.raises(RewardInFlightError) as exc:
            action()
        assert exc.value.code == "reward_in_flight" and isinstance(exc.value, RewardStateError)
    assert _reload(db_session, r).status == RewardStatus.HELD
    assert prepare_batch(db_session, NOW) is None  # a held reward is never re-batched

    # The transfer did go out: the reward is marked paid (it was already in the amount).
    apply_transfer_event(db_session, "transfer.success", {"reference": ref, "amount": 500_000})
    paid = _reload(db_session, r)
    assert paid.status == RewardStatus.PAID and "paid_in_flight" in paid.review_reasons
    assert _reload(db_session, batch).status == BatchStatus.PAID


def test_c1_held_in_flight_reward_becomes_releasable_once_its_transfer_is_dead(
    db_session, super_admin_user
):
    batch, m, (t1, t2), (r1, r2) = _processing(db_session, super_admin_user, rewards_per=2)
    hold_for_dispute(db_session, t2.id)
    db_session.commit()
    ref = _item(db_session, batch, m).transfer_reference
    apply_transfer_event(db_session, "transfer.failed", {"reference": ref, "message": "declined"})
    with pytest.raises(RewardInFlightError):  # still tied until the item is re-claimed
        release(db_session, r2.id, super_admin_user.id, "n", now=NOW)

    provider = FakeProvider(verify_fn=_verify_as("failed"))
    retry_batch(db_session, batch.id, super_admin_user.id, provider)

    # Re-claim: the amount is recomputed from what is batched now, the held reward is detached.
    assert [(a, r) for _, a, r in provider.pay_calls] == [(500_000, f"{ref}-r2")]
    item = _item(db_session, batch, m)
    assert item.status == PAID and item.reward_ids == [str(r1.id)]
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    release(db_session, r2.id, super_admin_user.id, "n", now=NOW)  # now allowed
    assert _reload(db_session, r2).status == RewardStatus.PAYABLE


def test_c1_dispute_on_a_pending_item_refreshes_amount_total_and_reward_ids(db_session):
    m = _marketer(db_session)
    t1, t2 = _tenant(db_session), _tenant(db_session)
    r1 = _reward(db_session, m, tenant=t1)
    r2 = _reward(db_session, m, tenant=t2, amount=300_000)
    batch = prepare_batch(db_session, NOW)
    assert batch.total_kobo == 800_000

    hold_for_dispute(db_session, t2.id)
    db_session.commit()

    item = _item(db_session, batch, m)
    assert item.status == PENDING and item.amount_kobo == 500_000
    assert item.reward_ids == [str(r1.id)]
    assert _reload(db_session, batch).total_kobo == 500_000
    assert _reload(db_session, r2).payout_batch_id is None


def test_r41_dispute_on_a_failed_item_keeps_the_link_and_the_otp_finalised_later_is_recorded(
    db_session, super_admin_user
):
    """Probe r2: an OTP-pending transfer can still pay, so a held reward must stay tied to it."""
    m, t = _marketer(db_session), _tenant(db_session)
    r = _reward(db_session, m, tenant=t)
    b1 = prepare_batch(db_session, NOW)
    otp = FakeProvider(pay_fn=lambda mk, a, ref: TransferResult("otp", ref, "otp"))
    approve_batch(db_session, b1.id, super_admin_user.id, otp)
    item = _item(db_session, b1, m)
    sent = otp.pay_calls[0][1]
    assert item.status == FAILED and item.failure_reason == "otp_required"

    hold_for_dispute(db_session, t.id)
    db_session.commit()

    # Unlike a pending/awaiting_manual item, nothing is refreshed and the link stays.
    item = _item(db_session, b1, m)
    assert item.status == FAILED and item.amount_kobo == sent and item.reward_ids == [str(r.id)]
    held = _reload(db_session, r)
    assert held.status == RewardStatus.HELD and held.payout_batch_id == b1.id
    with pytest.raises(RewardInFlightError):
        release(db_session, r.id, super_admin_user.id, "dispute irrelevant", now=NOW)
    with pytest.raises(RewardInFlightError):
        void(db_session, r.id, super_admin_user.id, "n")
    assert prepare_batch(db_session, NOW) is None  # so it cannot be re-batched and paid twice

    # The admin finalises the OTP in the Paystack dashboard: the original amount is recorded.
    retry = FakeProvider(verify_fn=_verify_as("success", amount=sent))
    result = retry_batch(db_session, b1.id, super_admin_user.id, retry)
    assert retry.pay_calls == []
    item = _item(db_session, b1, m)
    assert item.status == PAID and item.amount_kobo == sent
    paid = _reload(db_session, r)
    assert paid.status == RewardStatus.PAID and "paid_in_flight" in paid.review_reasons
    assert result.status == BatchStatus.PAID


def test_r41_failed_item_whose_transfer_is_confirmed_dead_detaches_the_held_reward(
    db_session, super_admin_user
):
    m = _marketer(db_session)
    t1, t2 = _tenant(db_session), _tenant(db_session)
    _reward(db_session, m, tenant=t1)
    r2 = _reward(db_session, m, tenant=t2)
    batch = prepare_batch(db_session, NOW)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=lambda mk, a, ref: TransferResult("otp", ref, "otp")),
    )
    hold_for_dispute(db_session, t2.id)
    db_session.commit()
    with pytest.raises(RewardInFlightError):
        release(db_session, r2.id, super_admin_user.id, "n", now=NOW)

    again = FakeProvider(verify_fn=_verify_as("failed"))  # Paystack confirms it is dead
    retry_batch(db_session, batch.id, super_admin_user.id, again)

    assert [a for _, a, _ in again.pay_calls] == [500_000]  # only the clean reward
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    release(db_session, r2.id, super_admin_user.id, "n", now=NOW)  # releasable now


def test_release_and_void_refuse_a_held_reward_still_tied_to_a_batch(db_session, super_admin_user):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    ra.status = RewardStatus.HELD  # held, batch link still set
    db_session.flush()
    with pytest.raises(RewardInFlightError):
        release(db_session, ra.id, super_admin_user.id, "n", now=NOW)
    with pytest.raises(RewardInFlightError):
        void(db_session, ra.id, super_admin_user.id, "n")
    assert _reload(db_session, ra).status == RewardStatus.HELD


def test_mark_paid_pays_the_refreshed_set_not_a_stale_claim(db_session, super_admin_user):
    m = _marketer(db_session)
    r1, r2 = _reward(db_session, m), _reward(db_session, m, amount=300_000)
    batch = prepare_batch(db_session, NOW)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    assert _item(db_session, batch, m).amount_kobo == 800_000
    r2 = _reload(db_session, r2)
    r2.status = RewardStatus.HELD  # held after the claim, link left behind
    db_session.commit()

    result = mark_paid(db_session, batch.id, [(m.id, "BANK-REF-A", 500_000)], super_admin_user.id)

    item = _item(db_session, batch, m)
    assert item.status == PAID and item.amount_kobo == 500_000
    assert item.reward_ids == [str(r1.id)]
    assert _reload(db_session, r1).status == RewardStatus.PAID
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    assert held.paid_at is None and held.transfer_reference is None
    assert result.total_kobo == 500_000


def test_mark_paid_on_an_item_whose_only_reward_was_held_accepts_only_the_claimed_amount(
    db_session, super_admin_user
):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    ra = _reload(db_session, ra)
    ra.status = RewardStatus.HELD  # held after the claim, link left behind (R49)
    db_session.commit()
    with pytest.raises(payouts.AmountMismatchError) as exc:
        mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 100_000)], super_admin_user.id)
    assert exc.value.current_amount_kobo == 0 and exc.value.claimed_amount_kobo == 500_000
    assert _item(db_session, batch, a).status == AWAITING

    mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert _item(db_session, batch, a).status == PAID
    assert _reload(db_session, ra).status == RewardStatus.PAID


def test_mark_paid_on_a_failed_item_with_no_payable_rewards_left_is_refused(
    db_session, super_admin_user
):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=lambda mk, am, ref: TransferResult("failed", ref, "rejected")),
    )
    ra = _reload(db_session, ra)
    ra.status = RewardStatus.HELD
    db_session.commit()
    with pytest.raises(PayoutStateError):
        mark_paid(
            db_session,
            batch.id,
            [(a.id, "BANK-REF-A", 500_000)],
            super_admin_user.id,
            FakeProvider(verify_fn=_verify_as("failed")),
        )
    assert _item(db_session, batch, a).status == FAILED


def test_a_reward_already_paid_or_linked_elsewhere_is_never_paid_by_another_item(
    db_session, super_admin_user
):
    batch, (a, b), (ra, rb) = _batch_of(db_session)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=_pending_for(a.id, b.id)),
    )
    item_a = _item(db_session, batch, a)
    # Corrupt state: A's item captured a reward that is already paid by something else.
    ra = _reload(db_session, ra)
    ra.status = RewardStatus.PAID
    ra.transfer_reference = "SOMEONE-ELSES-PAYMENT"
    ra.paid_at = NOW
    # ...and B's item captured a reward linked to a different batch.
    other = PayoutBatch(period_label="2026-10-15")
    db_session.add(other)
    db_session.flush()
    rb = _reload(db_session, rb)
    rb.payout_batch_id = other.id
    db_session.commit()

    for item in (item_a, _item(db_session, batch, b)):
        apply_transfer_event(
            db_session,
            "transfer.success",
            {"reference": item.transfer_reference, "amount": 500_000},
        )

    ra, rb = _reload(db_session, ra), _reload(db_session, rb)
    assert ra.transfer_reference == "SOMEONE-ELSES-PAYMENT"
    assert rb.status == RewardStatus.BATCHED and rb.payout_batch_id == other.id
    assert rb.paid_at is None


# --- I1 / I2 ---------------------------------------------------------------------------------


def test_i1_success_arriving_after_a_reversal_does_not_pay_again(db_session, super_admin_user):
    batch, m, _, (r,) = _processing(db_session, super_admin_user)
    ref = _item(db_session, batch, m).transfer_reference
    body = {"reference": ref, "amount": 500_000}

    apply_transfer_event(db_session, "transfer.reversed", body)
    assert _item(db_session, batch, m).status == FAILED
    late = apply_transfer_event(db_session, "transfer.success", body)

    assert late["status"] == "ignored" and late["reason"] == "item_reversed"
    assert _item(db_session, batch, m).status == FAILED
    assert _reload(db_session, r).status == RewardStatus.BATCHED

    # Same after paid -> reversed -> replayed success.
    batch2, m2, _, (r2,) = _processing(db_session, super_admin_user)
    ref2 = _item(db_session, batch2, m2).transfer_reference
    apply_transfer_event(db_session, "transfer.success", {"reference": ref2, "amount": 500_000})
    apply_transfer_event(db_session, "transfer.reversed", {"reference": ref2, "amount": 500_000})
    replay = apply_transfer_event(
        db_session, "transfer.success", {"reference": ref2, "amount": 500_000}
    )
    assert replay["status"] == "ignored"
    assert _item(db_session, batch2, m2).status == FAILED
    assert _reload(db_session, r2).status == RewardStatus.BATCHED


def test_i1_late_success_on_a_failed_item_needs_a_matching_amount(db_session, super_admin_user):
    batch, (a,), (ra,) = _batch_of(db_session, 1)
    approve_batch(
        db_session,
        batch.id,
        super_admin_user.id,
        FakeProvider(pay_fn=lambda m, amt, ref: TransferResult("otp", ref, "otp")),
    )
    ref = f"ref-{batch.id}-{a.id}"
    missing = apply_transfer_event(db_session, "transfer.success", {"reference": ref})
    wrong = apply_transfer_event(db_session, "transfer.success", {"reference": ref, "amount": 7})
    assert missing["reason"] == "amount_missing" and wrong["reason"] == "amount_mismatch"
    assert _item(db_session, batch, a).status == FAILED
    ok = apply_transfer_event(db_session, "transfer.success", {"reference": ref, "amount": 500_000})
    assert ok["status"] == "processed" and _item(db_session, batch, a).status == PAID


def test_i2_reversal_after_a_dispute_holds_the_reward_and_a_retry_does_not_pay_it(
    db_session, super_admin_user
):
    """Probe 4: a dispute recorded after payment, then a reversal, must not re-batch the reward."""
    m, t = _marketer(db_session), _tenant(db_session)
    _attribute(db_session, t, m)
    r = _reward(db_session, m, tenant=t)
    batch = prepare_batch(db_session, NOW)
    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())
    ref = _item(db_session, batch, m).transfer_reference
    record_dispute(db_session, t.id)
    db_session.commit()
    assert _reload(db_session, r).status == RewardStatus.PAID

    apply_transfer_event(db_session, "transfer.reversed", {"reference": ref, "amount": 500_000})

    held = _reload(db_session, r)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    assert "payment_dispute" in held.review_reasons
    assert held.paid_at is None and held.transfer_reference is None
    again = FakeProvider(verify_fn=_verify_as("reversed"))
    retry_batch(db_session, batch.id, super_admin_user.id, again)
    assert again.pay_calls == []
    assert _item(db_session, batch, m).status == SKIPPED


def test_i2_reversal_of_a_reward_paid_in_flight_holds_it(db_session, super_admin_user):
    batch, m, (t,), (r,) = _processing(db_session, super_admin_user)
    ref = _item(db_session, batch, m).transfer_reference
    hold_for_dispute(db_session, t.id)
    db_session.commit()
    apply_transfer_event(db_session, "transfer.success", {"reference": ref, "amount": 500_000})
    assert _reload(db_session, r).status == RewardStatus.PAID

    apply_transfer_event(db_session, "transfer.reversed", {"reference": ref, "amount": 500_000})

    held = _reload(db_session, r)
    assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    assert "payment_reversed_after_hold" in held.review_reasons


# --- I3 / I4 / minors -------------------------------------------------------------------------


def test_i3_a_failing_transfer_handler_is_a_500_so_paystack_redelivers(
    pg_client, db_session, super_admin_user, monkeypatch
):
    from app.core.config import settings
    from tests.referral.test_payout_webhook import TEST_SECRET, _post, _processing_batch

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", TEST_SECRET)
    batch, marketer, reward, ref = _processing_batch(db_session, super_admin_user)

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(payouts, "apply_transfer_event", boom)
    # The endpoint rolls the (shared, savepoint-based) test session back; record it instead of
    # letting it discard the fixture's own setup.
    rollbacks = []
    monkeypatch.setattr(db_session, "rollback", lambda: rollbacks.append(1))
    resp = _post(pg_client, "transfer.success", {"reference": ref, "amount": 500_000})

    assert resp.status_code == 500
    assert "secret internal detail" not in resp.text and "RuntimeError" not in resp.text
    assert rollbacks, "the failed handler's session must be rolled back"
    assert _item(db_session, batch, marketer).status == PROCESSING

    monkeypatch.undo()  # the redelivery succeeds
    from app.core.config import settings

    monkeypatch.setattr(
        settings, "PAYSTACK_SECRET_KEY", "sk_test_placeholder_webhook_secret_not_real"
    )
    again = _post(pg_client, "transfer.success", {"reference": ref, "amount": 500_000})
    assert again.status_code == 200 and again.json()["result"]["status"] == "processed"


def test_i3_other_events_keep_the_200_behaviour(pg_client, db_session, monkeypatch):
    from app.core.config import settings
    from tests.referral.test_payout_webhook import TEST_SECRET, _post

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", TEST_SECRET)

    def boom(self, data):
        raise RuntimeError("x")

    from app.services.webhook_handler_service import WebhookHandlerService

    monkeypatch.setattr(WebhookHandlerService, "_handle_charge_success", boom)
    resp = _post(pg_client, "charge.success", {"reference": "r"})
    assert resp.status_code == 200 and resp.json()["status"] == "error"


def test_i3_a_failure_midway_leaves_no_partial_state(pg_engine, monkeypatch):
    """The handler commits only at the end: an exception after the writes discards them."""
    factory = sessionmaker(bind=pg_engine)
    session = factory()
    try:
        marketer = _marketer(session)
        _reward(session, marketer)
        session.commit()
        batch = prepare_batch(session, NOW)
        approve_batch(session, batch.id, None, FakeProvider(pay_fn=_pending_for(marketer.id)))
        ref = f"ref-{batch.id}-{marketer.id}"
        marketer_id, batch_id = marketer.id, batch.id

        def explode(db, item, reference, now, provider_status):
            item.status = PAID  # the write happens, then the handler dies before its commit
            db.flush()
            raise RuntimeError("after the writes")

        monkeypatch.setattr(payouts, "_mark_item_paid", explode)
        with pytest.raises(RuntimeError):
            apply_transfer_event(session, "transfer.success", {"reference": ref, "amount": 500_000})
        session.rollback()
        monkeypatch.undo()
        item = session.scalar(select(PayoutItem).where(PayoutItem.batch_id == batch_id))
        assert item.status == PROCESSING
    finally:
        session.close()
        cleanup = factory()
        cleanup.execute(text("DELETE FROM payout_items WHERE batch_id = :b"), {"b": batch_id})
        cleanup.execute(
            text("DELETE FROM referral_rewards WHERE marketer_id = :m"), {"m": marketer_id}
        )
        cleanup.execute(text("DELETE FROM payout_batches WHERE id = :b"), {"b": batch_id})
        cleanup.execute(text("DELETE FROM marketers WHERE id = :m"), {"m": marketer_id})
        cleanup.commit()
        cleanup.close()


def test_i4_unknown_paystack_statuses_keep_the_item_processing_with_the_status_recorded(
    db_session, paystack, super_admin_user
):
    stub, service = paystack
    stub.transfer_status = "abandoned"
    batch, (a,), _ = _batch_of(db_session, 1)
    provider = PaystackTransferProvider(service=service)

    approve_batch(db_session, batch.id, super_admin_user.id, provider)

    item = _item(db_session, batch, a)
    assert item.status == PROCESSING and item.provider_status == "abandoned"
    assert _reload(db_session, batch).status == BatchStatus.PAYING

    class Verifying(PaystackTransferProvider):
        def verify(self, reference):
            return TransferVerification("pending", reference, "blocked")

    retry_batch(db_session, batch.id, super_admin_user.id, Verifying(service=service))
    item = _item(db_session, batch, a)
    assert item.status == PROCESSING and item.provider_status == "blocked"
    assert len(stub.bodies("/transfer")) == 1  # never re-sent


@pytest.fixture
def switched_to_manual(db_session, monkeypatch):
    """REFERRAL_PAYOUT_PROVIDER is back to ``manual`` after Paystack-era batches; Paystack answers
    ``verify`` through a mock transport (the answer is scripted via ``answer["status"]``)."""
    from app.core.config import settings
    from app.services.referral import providers

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "PAYSTACK_BASE_URL", "https://paystack.test")
    monkeypatch.setattr(settings, "REFERRAL_PAYOUT_PROVIDER", "manual")
    answer = {"status": "failed", "amount": None, "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/transfer/verify/"), request.url.path
        reference = request.url.path.rsplit("/", 1)[1]
        answer["calls"].append(reference)
        if answer["status"] == "not_found":
            return httpx.Response(404, json={"status": False, "message": "Transfer not found"})
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Transfer retrieved",
                "data": {
                    "status": answer["status"],
                    "reference": reference,
                    "amount": answer["amount"],
                },
            },
        )

    monkeypatch.setattr(
        providers,
        "PaystackService",
        lambda db: PaystackService(db, transport=httpx.MockTransport(handler)),
    )
    return answer


def _paystack_era_failed_item(db, admin):
    batch, (a,), (ra,) = _batch_of(db, 1)
    approve_batch(db, batch.id, admin.id, FakeProvider(pay_fn=_failing_for(a.id)))
    item = _item(db, batch, a)
    assert item.status == FAILED and item.provider_status == "rejected"
    return batch, a, ra, item.transfer_reference


def test_m5_switching_back_to_manual_still_verifies_a_failed_paystack_item_as_paid(
    db_session, super_admin_user, switched_to_manual
):
    batch, a, ra, ref = _paystack_era_failed_item(db_session, super_admin_user)
    assert isinstance(get_payout_provider(db_session), ManualPayoutProvider)
    switched_to_manual.update(status="success", amount=500_000)

    result = retry_batch(db_session, batch.id, super_admin_user.id)

    assert switched_to_manual["calls"] == [ref]  # verified at Paystack, not by the manual provider
    item = _item(db_session, batch, a)
    assert item.status == PAID and item.transfer_reference == ref  # recorded, never re-sent
    assert _reload(db_session, ra).status == RewardStatus.PAID
    assert result.status == BatchStatus.PAID


def test_m5_a_failed_paystack_item_is_retried_through_the_current_manual_provider(
    db_session, super_admin_user, switched_to_manual
):
    batch, a, ra, ref = _paystack_era_failed_item(db_session, super_admin_user)
    switched_to_manual.update(status="failed")  # Paystack confirms the transfer is dead

    retry_batch(db_session, batch.id, super_admin_user.id)

    assert switched_to_manual["calls"] == [ref]
    item = _item(db_session, batch, a)
    # a new attempt was claimed and, because the setting is manual, handed to a human to pay
    assert item.attempt == 2 and item.transfer_reference == f"{ref}-r2"
    assert item.status == AWAITING and item.provider_status == "manual"


def test_m5_mark_paid_on_a_failed_paystack_item_verifies_with_paystack_under_manual(
    db_session, super_admin_user, switched_to_manual
):
    batch, a, ra, ref = _paystack_era_failed_item(db_session, super_admin_user)

    switched_to_manual.update(status="success")  # the money did go: refuse to record it again
    with pytest.raises(payouts.TransferUnverifiedError):
        mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert _item(db_session, batch, a).status == FAILED

    switched_to_manual.update(status="reversed")
    mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert switched_to_manual["calls"] == [ref, ref]
    item = _item(db_session, batch, a)
    assert item.status == PAID and item.transfer_reference == "BANK-REF-A"


def test_m5_manual_items_keep_manual_behaviour_and_unmarked_ones_stay_unverifiable(
    db_session, super_admin_user, switched_to_manual
):
    from app.services.referral.providers import get_verifier, is_paystack_item

    # An awaiting_manual item carries a ``ref-`` reference too, but it was never sent to Paystack.
    assert not is_paystack_item("ref-b-m", "manual")
    assert not is_paystack_item("BANK-REF-1", "manual")
    assert not is_paystack_item(None, "rejected")
    assert is_paystack_item("ref-b-m", "rejected") and is_paystack_item("ref-b-m-r2", None)

    manual = ManualPayoutProvider()
    fake = FakeProvider()
    assert get_verifier(db_session, "ref-b-m", "manual", manual) is manual
    assert get_verifier(db_session, "ref-b-m", "rejected", fake) is fake  # injected/Paystack: as is
    assert isinstance(get_verifier(db_session, "ref-b-m", None, manual), PaystackTransferProvider)

    # A manual-era awaiting_manual item is not retried and never calls Paystack.
    batch, (b,), _ = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, manual)
    retry_batch(db_session, batch.id, super_admin_user.id)
    assert switched_to_manual["calls"] == []
    assert _item(db_session, batch, b).status == AWAITING


def test_m1_a_non_positive_amount_is_skipped_without_calling_the_provider(
    db_session, super_admin_user
):
    m = _marketer(db_session)
    r = _reward(db_session, m, amount=0)
    batch = prepare_batch(db_session, NOW)
    provider = FakeProvider()

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider)

    assert provider.pay_calls == []
    item = _item(db_session, batch, m)
    assert item.status == SKIPPED and item.failure_reason == "non_positive_amount"
    assert _reload(db_session, r).status == RewardStatus.HELD
    assert result.status == BatchStatus.FAILED


def test_m2_the_approval_hook_runs_before_any_provider_call_and_only_once(
    db_session, super_admin_user
):
    batch, _, _ = _batch_of(db_session)
    provider = FakeProvider()
    seen = []

    def hook(b):
        seen.append((len(provider.pay_calls), b.status))

    approve_batch(db_session, batch.id, super_admin_user.id, provider, after_approval=hook)
    approve_batch(db_session, batch.id, super_admin_user.id, provider, after_approval=hook)

    assert seen == [(0, BatchStatus.PAYING)]  # before the first transfer; not on the repeat
    assert len(provider.pay_calls) == 2


def test_m2_a_failing_approval_hook_never_blocks_the_money_flow(db_session, super_admin_user):
    batch, _, _ = _batch_of(db_session)
    provider = FakeProvider()

    def hook(b):
        raise RuntimeError("audit down")

    result = approve_batch(db_session, batch.id, super_admin_user.id, provider, after_approval=hook)
    assert result.status == BatchStatus.PAID and len(provider.pay_calls) == 2


def test_m3_failure_text_never_falls__back_to_our_own_transfer_reason(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(a.id))
    )
    ref = f"ref-{batch.id}-{a.id}"
    apply_transfer_event(
        db_session,
        "transfer.failed",
        {"reference": ref, "reason": "Luran referral payout 2026-11-01"},
    )
    assert _item(db_session, batch, a).failure_reason == "transfer_failed"


# --------------------------------------------------------------------------- fix round 2 (R41-R43)


def test_r43_mark_paid_refuses_an_amount_that_differs_from_the_current_item(
    db_session, super_admin_user
):
    """An amount that is neither the current (500k) nor the claimed (800k) one: refuse, change nothing."""
    m = _marketer(db_session)
    r1 = _reward(db_session, m)
    r2 = _reward(db_session, m, amount=300_000)
    batch = prepare_batch(db_session, NOW)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    r2 = _reload(db_session, r2)
    r2.status = RewardStatus.HELD  # held after the claim, link left behind
    db_session.commit()

    with pytest.raises(payouts.AmountMismatchError) as exc:
        mark_paid(db_session, batch.id, [(m.id, "BANK-REF-A", 650_000)], super_admin_user.id)
    assert exc.value.code == "amount_mismatch" and exc.value.current_amount_kobo == 500_000
    assert exc.value.claimed_amount_kobo == 800_000
    assert isinstance(exc.value, PayoutStateError)
    item = _item(db_session, batch, m)
    assert item.status == AWAITING and item.transfer_reference != "BANK-REF-A"
    assert _reload(db_session, r1).status == RewardStatus.BATCHED
    held = _reload(db_session, r2)
    assert held.status == RewardStatus.HELD and held.payout_batch_id == batch.id  # untouched

    mark_paid(db_session, batch.id, [(m.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert _item(db_session, batch, m).status == PAID


def test_r43_mark_paid_requires_a_positive_integer_amount(db_session, super_admin_user):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    for bad in (0, -1, 500_000.0, True, None, "500000"):
        with pytest.raises(PayoutStateError):
            mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", bad)], super_admin_user.id)
    assert _item(db_session, batch, a).status == AWAITING


def test_r4_transfer_event_redelivery_after_a_failed_finalize_still_recomputes_the_batch(
    db_session, super_admin_user, monkeypatch
):
    batch, m, _, (r,) = _processing(db_session, super_admin_user)
    ref = _item(db_session, batch, m).transfer_reference
    body = {"reference": ref, "amount": 500_000}

    def explode(db, batch_id):
        raise RuntimeError("finalize failed after the commit")

    real = payouts._finalize_batch
    monkeypatch.setattr(payouts, "_finalize_batch", explode)
    with pytest.raises(RuntimeError):
        apply_transfer_event(db_session, "transfer.success", body)
    monkeypatch.setattr(payouts, "_finalize_batch", real)
    assert _item(db_session, batch, m).status == PAID
    assert _reload(db_session, batch).status == BatchStatus.PAYING  # stale: the finalize was lost

    redelivered = apply_transfer_event(db_session, "transfer.success", body)

    assert redelivered["status"] == "duplicate"
    assert _reload(db_session, batch).status == BatchStatus.PAID


def test_i3_a_dispute_handler_failure_is_a_5xx_so_paystack_redelivers(
    pg_client, db_session, monkeypatch
):
    from app.core.config import settings
    from app.services.webhook_handler_service import WebhookHandlerService
    from tests.referral.test_payout_webhook import TEST_SECRET, _post

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", TEST_SECRET)

    def boom(self, data):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(WebhookHandlerService, "_handle_dispute_create", boom)
    monkeypatch.setattr(db_session, "rollback", lambda: None)  # shared savepoint-based session
    resp = _post(pg_client, "charge.dispute.create", {"transaction": {"reference": "r"}})

    assert resp.status_code == 500
    assert "secret internal detail" not in resp.text and "RuntimeError" not in resp.text


@pytest.fixture
def committed_reward(pg_engine):
    """A committed payable reward (with tenant) and two committed batches to flip its link to."""
    factory = sessionmaker(bind=pg_engine)
    session = factory()
    try:
        marketer = _marketer(session)
        tenant = _tenant(session)
        reward = _reward(session, marketer, tenant=tenant)
        batches = [PayoutBatch(period_label="2026-11-01") for _ in range(2)]
        session.add_all(batches)
        session.commit()
        ids = (marketer.id, tenant.id, reward.id, [b.id for b in batches])
    finally:
        session.close()
    yield factory, ids
    marketer_id, tenant_id, reward_id, batch_ids = ids
    cleanup = factory()
    try:
        cleanup.execute(text("DELETE FROM referral_rewards WHERE id = :r"), {"r": reward_id})
        cleanup.execute(text("DELETE FROM payout_batches WHERE id = ANY(:b)"), {"b": batch_ids})
        cleanup.execute(
            text("DELETE FROM tenant_capabilities WHERE tenant_id = :t"), {"t": tenant_id}
        )
        cleanup.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        cleanup.execute(text("DELETE FROM marketers WHERE id = :m"), {"m": marketer_id})
        cleanup.commit()
    finally:
        cleanup.close()


def _flipping(factory, reward_id, batch_ids, times):
    """Wrap lock_payout_context so the reward's link moves under it ``times`` times."""
    real = payouts.lock_payout_context
    calls = []

    def wrapper(db, marketer_id, link):
        calls.append(link)
        if len(calls) <= times:  # the link moves after hold_for_dispute read it, before it locks
            other = factory()
            try:
                new = batch_ids[0] if link is None else None
                other.execute(
                    text(
                        "UPDATE referral_rewards SET payout_batch_id = :b, status = :s WHERE id = :r"
                    ),
                    {"b": new, "s": "batched" if new else "payable", "r": reward_id},
                )
                other.commit()
            finally:
                other.close()
        return real(db, marketer_id, link)

    return wrapper, calls


def test_r42_hold_retries_when_the_batch_link_moves_between_read_and_lock(
    committed_reward, monkeypatch
):
    factory, (marketer_id, tenant_id, reward_id, batch_ids) = committed_reward
    wrapper, calls = _flipping(factory, reward_id, batch_ids, times=1)
    monkeypatch.setattr(payouts, "lock_payout_context", wrapper)
    session = factory()
    try:
        hold_for_dispute(session, tenant_id)
        session.commit()
        held = session.get(ReferralReward, reward_id)
        assert held.status == RewardStatus.HELD and held.payout_batch_id is None
    finally:
        session.close()
    assert calls == [None, batch_ids[0]]  # second attempt took the locks for the NEW link


def test_r42_hold_gives_up_cleanly_when_the_link_keeps_moving(committed_reward, monkeypatch):
    factory, (marketer_id, tenant_id, reward_id, batch_ids) = committed_reward
    wrapper, calls = _flipping(factory, reward_id, batch_ids, times=99)
    monkeypatch.setattr(payouts, "lock_payout_context", wrapper)
    session = factory()
    try:
        with pytest.raises(RewardError):
            hold_for_dispute(session, tenant_id)
        session.rollback()
    finally:
        session.close()
    assert len(calls) == 3
