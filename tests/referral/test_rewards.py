"""Referral reward lifecycle: creation on first payment, hold clearing, release/void, disputes.

This is a money path, so most tests run against real Postgres (``db_session``), and the two
concurrency tests use two real sessions on separate connections.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models.audit import AuditLog
from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    AttributionSource,
    Marketer,
    MarketerStatus,
    PayoutBatch,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
    SignupOrigin,
)
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.referral import rewards as rewards_module
from app.services.referral.rewards import (
    RewardNotFoundError,
    RewardStateError,
    create_reward_for_first_payment,
    hold_for_dispute,
    promote_pending,
    reconcile_missing_rewards,
    record_dispute,
    release,
    void,
)
from app.services.subscription_service import SubscriptionService
from app.services.webhook_handler_service import WebhookHandlerService

NOW = datetime.now(UTC)


# --------------------------------------------------------------------------- builders


def _tenant(db, *, created_at=None) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Reward Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        created_at=created_at or NOW - timedelta(days=10),
    )
    db.add(tenant)
    db.flush()
    db.add(TenantCapabilities(tenant_id=tenant.id))
    db.flush()
    return tenant


def _marketer(db, status=MarketerStatus.ACTIVE) -> Marketer:
    marketer = Marketer(
        name="Reward Marketer",
        email=f"m_{uuid4().hex[:6]}@marketer-co.example",
        code=f"code-{uuid4().hex[:8]}",
        status=status,
    )
    db.add(marketer)
    db.flush()
    return marketer


def _attribute(db, tenant, marketer, *, self_referral=False, origin=None) -> ReferralAttribution:
    attribution = ReferralAttribution(
        tenant_id=tenant.id,
        marketer_id=marketer.id,
        source=AttributionSource.COOKIE,
        self_referral=self_referral,
        attributed_at=tenant.created_at,
    )
    db.add(attribution)
    if origin:
        db.add(SignupOrigin(tenant_id=tenant.id, origin_hash=origin))
    db.flush()
    return attribution


def _payment(db, tenant, *, status="success", paid_at=None, reference=None) -> PaymentTransaction:
    payment = PaymentTransaction(
        tenant_id=tenant.id,
        reference=reference or f"ref_{uuid4().hex[:12]}",
        amount=1_000_000,
        tier="professional",
        billing_cycle="monthly",
        status=status,
        paid_at=paid_at,
    )
    db.add(payment)
    db.flush()
    return payment


def _referred(db, *, tenant_age_days=10, marketer_status=MarketerStatus.ACTIVE, **attr_kwargs):
    """A tenant attributed to a fresh marketer, plus one pending (unpaid) transaction."""
    tenant = _tenant(db, created_at=NOW - timedelta(days=tenant_age_days))
    marketer = _marketer(db, marketer_status)
    _attribute(db, tenant, marketer, **attr_kwargs)
    return tenant, marketer


def _rewards(db, tenant_id):
    db.expire_all()
    return list(
        db.execute(select(ReferralReward).where(ReferralReward.tenant_id == tenant_id)).scalars()
    )


def _reward(db, *, status=RewardStatus.PENDING, payable_after=None, marketer=None, reasons=None):
    tenant = _tenant(db)
    marketer = marketer or _marketer(db)
    reward = ReferralReward(
        tenant_id=tenant.id,
        marketer_id=marketer.id,
        amount_kobo=500_000,
        status=status,
        converted_at=NOW - timedelta(days=1),
        payable_after=payable_after or NOW + timedelta(days=6),
        review_reasons=reasons or [],
    )
    db.add(reward)
    db.flush()
    return reward


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- creation


def test_first_payment_creates_one_reward_amount_500000(db_session):
    tenant, marketer = _referred(db_session)
    paid_at = NOW - timedelta(days=2)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)

    assert reward is not None
    assert reward.amount_kobo == 500_000
    assert reward.status == RewardStatus.PENDING
    assert reward.marketer_id == marketer.id
    assert reward.tenant_id == tenant.id
    assert reward.review_reasons == []
    assert _utc(reward.converted_at) == paid_at
    assert _utc(reward.payable_after) == paid_at + timedelta(days=7)
    assert len(_rewards(db_session, tenant.id)) == 1


def test_amount_and_hold_follow_runtime_config(db_session):
    from app.db.models.referral import ReferralConfig

    db_session.merge(ReferralConfig(key="reward_kobo", value=123_000))
    db_session.merge(ReferralConfig(key="hold_days", value=3))
    db_session.flush()
    tenant, _ = _referred(db_session)
    paid_at = NOW - timedelta(days=1)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)

    assert reward.amount_kobo == 123_000
    assert _utc(reward.payable_after) == paid_at + timedelta(days=3)


def test_second_payment_creates_nothing(db_session):
    tenant, _ = _referred(db_session)
    first_at = NOW - timedelta(days=5)
    first = _payment(db_session, tenant, paid_at=first_at)
    assert create_reward_for_first_payment(db_session, tenant.id, first.reference, first_at)

    second_at = NOW - timedelta(days=1)
    second = _payment(db_session, tenant, paid_at=second_at)

    assert (
        create_reward_for_first_payment(db_session, tenant.id, second.reference, second_at) is None
    )
    assert len(_rewards(db_session, tenant.id)) == 1


def test_only_the_earliest_successful_payment_is_rewarded(db_session):
    """R30: a later payment (webhook delivered first, or reconciled late) never takes the reward."""
    tenant, _ = _referred(db_session)
    _payment(db_session, tenant, paid_at=NOW - timedelta(days=5))
    later_at = NOW - timedelta(days=1)
    later = _payment(db_session, tenant, paid_at=later_at)

    assert create_reward_for_first_payment(db_session, tenant.id, later.reference, later_at) is None
    assert _rewards(db_session, tenant.id) == []


def test_failed_earlier_payment_does_not_block_the_first_success(db_session):
    tenant, _ = _referred(db_session)
    _payment(db_session, tenant, status="failed", paid_at=NOW - timedelta(days=5))
    paid_at = NOW - timedelta(days=1)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    assert create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)


def test_payment_after_day_90_creates_nothing(db_session):
    tenant, _ = _referred(db_session, tenant_age_days=120)
    paid_at = tenant.created_at + timedelta(days=90, minutes=1)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    assert (
        create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at) is None
    )
    assert _rewards(db_session, tenant.id) == []


def test_payment_exactly_on_day_90_is_rewarded(db_session):
    tenant, _ = _referred(db_session, tenant_age_days=120)
    paid_at = tenant.created_at + timedelta(days=90)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    assert create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)


def test_payment_during_trial_counts(db_session):
    tenant, _ = _referred(db_session, tenant_age_days=2)
    assert tenant.subscription_status == "trialing"
    paid_at = NOW - timedelta(hours=1)
    payment = _payment(db_session, tenant, paid_at=paid_at)

    assert create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)


def test_admin_override_or_promotional_grant_creates_nothing(db_session):
    """Only a successful Paystack payment earns a reward; an upgrade without one never does."""
    tenant, _ = _referred(db_session)
    # Admin override / promotional grant: the tenant is upgraded with no PaymentTransaction.
    SubscriptionService(db_session).upgrade_subscription(
        tenant_id=tenant.id, tier="professional", billing_cycle="monthly"
    )
    assert reconcile_missing_rewards(db_session, NOW) >= 0
    assert _rewards(db_session, tenant.id) == []
    # Even if something called creation with an invented reference it must not reward.
    assert create_reward_for_first_payment(db_session, tenant.id, "ref_not_a_payment", NOW) is None
    # A pending or failed transaction is not a payment either.
    pending = _payment(db_session, tenant, status="pending")
    assert create_reward_for_first_payment(db_session, tenant.id, pending.reference, NOW) is None
    # Nor is another tenant's successful payment.
    other = _tenant(db_session)
    theirs = _payment(db_session, other, paid_at=NOW)
    assert create_reward_for_first_payment(db_session, tenant.id, theirs.reference, NOW) is None
    assert _rewards(db_session, tenant.id) == []


def test_unattributed_and_self_referral_create_nothing(db_session):
    plain = _tenant(db_session)
    plain_payment = _payment(db_session, plain, paid_at=NOW)
    assert (
        create_reward_for_first_payment(db_session, plain.id, plain_payment.reference, NOW) is None
    )

    own, _ = _referred(db_session, self_referral=True)
    own_payment = _payment(db_session, own, paid_at=NOW)
    assert create_reward_for_first_payment(db_session, own.id, own_payment.reference, NOW) is None
    assert _rewards(db_session, plain.id) == []
    assert _rewards(db_session, own.id) == []


def test_attribution_is_required_even_when_a_purged_reward_row_exists(db_session):
    """R13: a purged tenant's reward keeps tenant_id NULL; that must never reward a new tenant."""
    marketer = _marketer(db_session)
    orphan = ReferralReward(
        tenant_id=None,
        marketer_id=marketer.id,
        amount_kobo=500_000,
        status=RewardStatus.PENDING,
        converted_at=NOW,
        payable_after=NOW + timedelta(days=7),
    )
    db_session.add(orphan)
    stranger = _tenant(db_session)  # no attribution row
    payment = _payment(db_session, stranger, paid_at=NOW)
    db_session.flush()

    assert create_reward_for_first_payment(db_session, stranger.id, payment.reference, NOW) is None
    assert _rewards(db_session, stranger.id) == []


def test_flagged_goes_held_with_reasons(db_session):
    first, marketer = _referred(db_session, origin="same-origin-hash")
    second = _tenant(db_session)
    _attribute(db_session, second, marketer, origin="same-origin-hash")
    paid_at = NOW - timedelta(hours=2)
    payment = _payment(db_session, second, paid_at=paid_at)

    reward = create_reward_for_first_payment(db_session, second.id, payment.reference, paid_at)

    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["shared_origin"]
    assert reward.amount_kobo == 500_000
    assert _utc(reward.payable_after) == paid_at + timedelta(days=7)


def test_suspended_marketer_at_conversion_goes_held(db_session):
    tenant, _ = _referred(db_session, marketer_status=MarketerStatus.SUSPENDED)
    payment = _payment(db_session, tenant, paid_at=NOW)

    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)

    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["marketer_suspended"]


def test_suspended_marketer_and_flags_combine(db_session, monkeypatch):
    monkeypatch.setattr(rewards_module, "evaluate_flags", lambda db, tid: ["shared_domain"])
    tenant, _ = _referred(db_session, marketer_status=MarketerStatus.SUSPENDED)
    payment = _payment(db_session, tenant, paid_at=NOW)

    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)

    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["shared_domain", "marketer_suspended"]


def test_evaluate_flags_failure_creates_no_reward(db_session, monkeypatch):
    """Fail closed: if the fraud check cannot run, no reward row is written."""

    def boom(db, tenant_id):
        raise RuntimeError("flags down")

    monkeypatch.setattr(rewards_module, "evaluate_flags", boom)
    tenant, _ = _referred(db_session)
    payment = _payment(db_session, tenant, paid_at=NOW)

    with pytest.raises(RuntimeError):
        create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)
    assert _rewards(db_session, tenant.id) == []


def test_existing_reward_makes_creation_return_none(db_session):
    tenant, _ = _referred(db_session)
    payment = _payment(db_session, tenant, paid_at=NOW)
    assert create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)
    assert create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW) is None
    assert len(_rewards(db_session, tenant.id)) == 1


# --------------------------------------------------------------------------- webhook


@pytest.fixture
def pending_referred_payment(db_session):
    tenant = _tenant(db_session)
    marketer = _marketer(db_session)
    _attribute(db_session, tenant, marketer)
    payment = _payment(db_session, tenant, status="pending")
    db_session.commit()
    return tenant, marketer, payment


def _charge(reference: str) -> dict:
    return {
        "reference": reference,
        "channel": "card",
        "customer": {"email": "buyer@example.com", "customer_code": "CUS_placeholder"},
        "authorization": {"authorization_code": "AUTH_placeholder"},
    }


def test_webhook_charge_success_creates_the_reward(db_session, pending_referred_payment):
    tenant, marketer, payment = pending_referred_payment

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    rewards = _rewards(db_session, tenant.id)
    assert len(rewards) == 1
    assert rewards[0].marketer_id == marketer.id
    assert rewards[0].status == RewardStatus.PENDING
    db_session.refresh(payment)
    assert _utc(rewards[0].converted_at) == _utc(payment.paid_at)


def test_webhook_replay_creates_no_second_reward(db_session, pending_referred_payment, monkeypatch):
    tenant, _, payment = pending_referred_payment
    handler = WebhookHandlerService(db_session)
    calls = []
    real = rewards_module.create_reward_for_first_payment
    monkeypatch.setattr(
        rewards_module,
        "create_reward_for_first_payment",
        lambda *a, **kw: calls.append(a) or real(*a, **kw),
    )
    handler.handle_event("charge.success", _charge(payment.reference))
    assert len(calls) == 1

    replay = handler.handle_event("charge.success", _charge(payment.reference))

    assert replay == {"status": "already_processed"}
    assert len(calls) == 1  # the replay returned before the reward hook ran
    assert len(_rewards(db_session, tenant.id)) == 1


def test_reward_creation_failure_never_breaks_the_payment(
    db_session, pending_referred_payment, monkeypatch
):
    """R26: an exception inside reward creation is logged, the payment still completes."""
    tenant, _, payment = pending_referred_payment

    def boom(*args, **kwargs):
        raise RuntimeError("reward creation exploded")

    monkeypatch.setattr(rewards_module, "create_reward_for_first_payment", boom)

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    db_session.expire_all()
    assert db_session.get(PaymentTransaction, payment.id).status == "success"
    assert db_session.get(Tenant, tenant.id).subscription_tier == "professional"
    assert _rewards(db_session, tenant.id) == []


def test_database_error_in_reward_creation_does_not_poison_the_session(
    db_session, pending_referred_payment, monkeypatch
):
    """A failed SQL statement (aborts a Postgres transaction) is contained by the savepoint."""
    tenant, _, payment = pending_referred_payment

    def bad_sql(db, *args, **kwargs):
        db.execute(text("SELECT * FROM table_that_does_not_exist"))

    monkeypatch.setattr(rewards_module, "create_reward_for_first_payment", bad_sql)

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    db_session.expire_all()
    assert db_session.get(PaymentTransaction, payment.id).status == "success"
    history = db_session.query(SubscriptionHistory).filter_by(payment_reference=payment.reference)
    assert history.count() == 1


def test_evaluate_flags_failure_in_webhook_creates_no_reward(
    db_session, pending_referred_payment, monkeypatch
):
    tenant, _, payment = pending_referred_payment

    def boom(db, tenant_id):
        raise RuntimeError("flags down")

    monkeypatch.setattr(rewards_module, "evaluate_flags", boom)

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    db_session.expire_all()
    assert db_session.get(PaymentTransaction, payment.id).status == "success"
    assert _rewards(db_session, tenant.id) == []


def test_reconcile_creates_the_missing_reward_exactly_once(
    db_session, pending_referred_payment, monkeypatch
):
    tenant, _, payment = pending_referred_payment
    # The first delivery succeeded but reward creation failed (flags down) ...
    monkeypatch.setattr(
        rewards_module, "evaluate_flags", lambda db, tid: (_ for _ in ()).throw(RuntimeError("x"))
    )
    WebhookHandlerService(db_session).handle_event("charge.success", _charge(payment.reference))
    assert _rewards(db_session, tenant.id) == []
    monkeypatch.undo()
    # ... and a replay is now a no-op, so only the reconcile job can repair it.
    assert WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    ) == {"status": "already_processed"}

    first = reconcile_missing_rewards(db_session, datetime.now(UTC))
    second = reconcile_missing_rewards(db_session, datetime.now(UTC))

    assert first >= 1
    assert second <= first
    rewards = _rewards(db_session, tenant.id)
    assert len(rewards) == 1  # the second run did not add another for this tenant
    db_session.refresh(payment)
    assert _utc(rewards[0].converted_at) == _utc(payment.paid_at)


def test_reconcile_skips_self_referral_unpaid_outside_window_and_rewarded(db_session):
    self_ref, _ = _referred(db_session, self_referral=True)
    _payment(db_session, self_ref, paid_at=NOW)
    unpaid, _ = _referred(db_session)
    _payment(db_session, unpaid, status="pending")
    old, _ = _referred(db_session, tenant_age_days=200)
    _payment(db_session, old, paid_at=old.created_at + timedelta(days=95))
    done, _ = _referred(db_session)
    done_payment = _payment(db_session, done, paid_at=NOW - timedelta(days=1))
    existing = create_reward_for_first_payment(
        db_session, done.id, done_payment.reference, NOW - timedelta(days=1)
    )
    ok, _ = _referred(db_session)
    _payment(db_session, ok, paid_at=NOW - timedelta(days=3))
    _payment(db_session, ok, paid_at=NOW - timedelta(days=1))  # later payment, same tenant

    reconcile_missing_rewards(db_session, NOW)

    for tenant in (self_ref, unpaid, old):
        assert _rewards(db_session, tenant.id) == []
    assert [r.id for r in _rewards(db_session, done.id)] == [existing.id]
    ok_rewards = _rewards(db_session, ok.id)
    assert len(ok_rewards) == 1
    assert abs(_utc(ok_rewards[0].converted_at) - (NOW - timedelta(days=3))) < timedelta(seconds=1)


# --------------------------------------------------------------------------- promote


def test_promote_pending_respects_7_day_hold(db_session):
    tenant, marketer = _referred(db_session)
    paid_at = NOW - timedelta(days=3)
    payment = _payment(db_session, tenant, paid_at=paid_at)
    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, paid_at)

    promote_pending(db_session, NOW)
    db_session.refresh(reward)
    assert reward.status == RewardStatus.PENDING  # day 3 of 7

    promote_pending(db_session, paid_at + timedelta(days=7) - timedelta(seconds=1))
    db_session.refresh(reward)
    assert reward.status == RewardStatus.PENDING

    promoted = promote_pending(db_session, paid_at + timedelta(days=7))
    db_session.refresh(reward)
    assert promoted >= 1
    assert reward.status == RewardStatus.PAYABLE
    assert promote_pending(db_session, paid_at + timedelta(days=8)) == 0  # idempotent


def test_promote_pending_leaves_held_and_suspended_marketer_rewards(db_session):
    past = NOW - timedelta(days=1)
    held = _reward(db_session, status=RewardStatus.HELD, payable_after=past, reasons=["x"])
    suspended = _reward(
        db_session,
        status=RewardStatus.PENDING,
        payable_after=past,
        marketer=_marketer(db_session, MarketerStatus.SUSPENDED),
    )
    active = _reward(db_session, status=RewardStatus.PENDING, payable_after=past)

    assert promote_pending(db_session, NOW) >= 1

    for reward in (held, suspended, active):
        db_session.refresh(reward)
    assert held.status == RewardStatus.HELD
    assert suspended.status == RewardStatus.PENDING
    assert active.status == RewardStatus.PAYABLE


# --------------------------------------------------------------------------- release / void


def test_release_semantics_before_and_after_hold(db_session):
    early_due = NOW + timedelta(days=4)
    early = _reward(db_session, status=RewardStatus.HELD, payable_after=early_due, reasons=["a"])
    late = _reward(db_session, status=RewardStatus.HELD, payable_after=NOW - timedelta(days=1))
    admin_id = uuid4()

    release(db_session, early.id, admin_id, "looked fine", now=NOW)
    release(db_session, late.id, admin_id, "looked fine", now=NOW)

    db_session.refresh(early)
    db_session.refresh(late)
    assert early.status == RewardStatus.PENDING
    assert _utc(early.payable_after) == early_due  # original date kept
    assert late.status == RewardStatus.PAYABLE


@pytest.mark.parametrize("fn", [release, void])
@pytest.mark.parametrize("note", ["", "   ", None])
def test_release_and_void_require_a_note(db_session, fn, note):
    reward = _reward(db_session, status=RewardStatus.HELD)
    with pytest.raises(ValueError):
        fn(db_session, reward.id, uuid4(), note)
    db_session.refresh(reward)
    assert reward.status == RewardStatus.HELD


@pytest.mark.parametrize(
    "status", [RewardStatus.PENDING, RewardStatus.PAYABLE, RewardStatus.BATCHED, RewardStatus.PAID]
)
def test_release_only_from_held(db_session, status):
    reward = _reward(db_session, status=status)
    with pytest.raises(RewardStateError):
        release(db_session, reward.id, uuid4(), "note")
    db_session.refresh(reward)
    assert reward.status == status


@pytest.mark.parametrize("status", [RewardStatus.HELD, RewardStatus.PENDING, RewardStatus.PAYABLE])
def test_void_from_open_states(db_session, status):
    reward = _reward(db_session, status=status)
    void(db_session, reward.id, uuid4(), "fraud confirmed")
    db_session.refresh(reward)
    assert reward.status == RewardStatus.VOID


@pytest.mark.parametrize("status", [RewardStatus.BATCHED, RewardStatus.PAID, RewardStatus.VOID])
def test_void_never_touches_batched_paid_or_void(db_session, status):
    reward = _reward(db_session, status=status)
    with pytest.raises(RewardStateError):
        void(db_session, reward.id, uuid4(), "note")
    db_session.refresh(reward)
    assert reward.status == status


def test_unknown_reward_is_a_domain_error(db_session):
    with pytest.raises(RewardNotFoundError):
        release(db_session, uuid4(), uuid4(), "note")
    with pytest.raises(RewardNotFoundError):
        void(db_session, uuid4(), uuid4(), "note")


def test_release_and_void_write_an_audit_entry_with_admin_and_note(db_session):
    admin_id = uuid4()
    held = _reward(db_session, status=RewardStatus.HELD)
    other = _reward(db_session, status=RewardStatus.PENDING)

    release(db_session, held.id, admin_id, "checked the domains")
    void(db_session, other.id, admin_id, "confirmed duplicate account")

    def entry(reward_id):
        return db_session.execute(
            select(AuditLog).where(AuditLog.resource_id == reward_id)
        ).scalar_one()

    released = entry(held.id)
    assert released.resource_type == "referral_reward"
    assert released.action == "release_referral_reward"
    assert released.changes["performed_by_id"] == str(admin_id)
    assert released.changes["metadata"]["note"] == "checked the domains"
    voided = entry(other.id)
    assert voided.action == "void_referral_reward"
    assert voided.changes["metadata"]["note"] == "confirmed duplicate account"


@pytest.mark.parametrize("action", ["release", "void"])
def test_audit_failure_rolls_the_change_back_and_raises(pg_engine, monkeypatch, action):
    """Change and audit are atomic: no silent release/void without a record (real commits)."""
    from app.services import enhanced_audit_service

    def boom(self, **kwargs):
        raise RuntimeError("audit down: SECRET-NOTE-TEXT")

    monkeypatch.setattr(enhanced_audit_service.EnhancedAuditService, "log_action", boom)
    factory = sessionmaker(bind=pg_engine)
    setup = factory()
    try:
        reward = _reward(setup, status=RewardStatus.HELD)
        reward_id, tenant_id, marketer_id = reward.id, reward.tenant_id, reward.marketer_id
        setup.commit()
    finally:
        setup.close()
    try:
        session = factory()
        try:
            with pytest.raises(RuntimeError):
                {"release": release, "void": void}[action](
                    session, reward_id, uuid4(), "SECRET-NOTE-TEXT"
                )
        finally:
            session.close()
        check = factory()
        try:
            assert check.get(ReferralReward, reward_id).status == RewardStatus.HELD
            assert (
                check.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.resource_id == reward_id)
                )
                == 0
            )
        finally:
            check.close()
    finally:
        _purge_committed(pg_engine, tenant_id, marketer_id)


def test_audit_failure_log_does_not_leak_the_exception_text(pg_engine, monkeypatch):
    from loguru import logger

    from app.services import enhanced_audit_service

    def boom(self, **kwargs):
        raise RuntimeError("SECRET-NOTE-TEXT")

    monkeypatch.setattr(enhanced_audit_service.EnhancedAuditService, "log_action", boom)
    messages = []
    sink = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    factory = sessionmaker(bind=pg_engine)
    setup = factory()
    try:
        reward = _reward(setup, status=RewardStatus.HELD)
        reward_id, tenant_id, marketer_id = reward.id, reward.tenant_id, reward.marketer_id
        setup.commit()
    finally:
        setup.close()
    try:
        session = factory()
        try:
            with pytest.raises(RuntimeError):
                release(session, reward_id, uuid4(), "SECRET-NOTE-TEXT")
        finally:
            session.close()
    finally:
        logger.remove(sink)
        _purge_committed(pg_engine, tenant_id, marketer_id)
    assert messages and not any("SECRET-NOTE-TEXT" in m for m in messages)
    assert any("RuntimeError" in m for m in messages)


def test_released_reward_with_old_review_reasons_is_later_promoted(db_session):
    reward = _reward(
        db_session,
        status=RewardStatus.HELD,
        payable_after=NOW + timedelta(days=3),
        reasons=["shared_domain"],
    )
    release(db_session, reward.id, uuid4(), "domains are unrelated", now=NOW)
    db_session.refresh(reward)
    assert reward.status == RewardStatus.PENDING
    assert reward.review_reasons == ["shared_domain"]  # kept as history

    promote_pending(db_session, NOW + timedelta(days=4))

    db_session.refresh(reward)
    assert reward.status == RewardStatus.PAYABLE


# --------------------------------------------------------------------------- disputes


def test_dispute_holds_unpaid_but_not_paid(db_session):
    results = {}
    for status in (
        RewardStatus.PENDING,
        RewardStatus.PAYABLE,
        RewardStatus.BATCHED,
        RewardStatus.PAID,
        RewardStatus.VOID,
    ):
        reward = _reward(db_session, status=status)
        results[status] = reward
        hold_for_dispute(db_session, reward.tenant_id)
        db_session.refresh(reward)

    for status in (RewardStatus.PENDING, RewardStatus.PAYABLE, RewardStatus.BATCHED):
        assert results[status].status == RewardStatus.HELD
        assert results[status].review_reasons == ["payment_dispute"]
    assert results[RewardStatus.PAID].status == RewardStatus.PAID
    assert results[RewardStatus.PAID].review_reasons == []
    assert results[RewardStatus.VOID].status == RewardStatus.VOID


def test_dispute_on_batched_reward_clears_the_batch_link(db_session):
    batch = PayoutBatch(period_label=f"test-{uuid4().hex[:6]}")
    db_session.add(batch)
    db_session.flush()
    reward = _reward(db_session, status=RewardStatus.BATCHED)
    reward.payout_batch_id = batch.id
    db_session.flush()

    hold_for_dispute(db_session, reward.tenant_id)

    db_session.refresh(reward)
    assert reward.status == RewardStatus.HELD
    assert reward.payout_batch_id is None


def test_dispute_reason_is_appended_once_to_an_already_held_reward(db_session):
    reward = _reward(db_session, status=RewardStatus.HELD, reasons=["shared_domain"])

    hold_for_dispute(db_session, reward.tenant_id)
    hold_for_dispute(db_session, reward.tenant_id)

    db_session.refresh(reward)
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["shared_domain", "payment_dispute"]


def test_dispute_for_tenant_without_reward_is_a_no_op(db_session):
    tenant = _tenant(db_session)
    assert hold_for_dispute(db_session, tenant.id) is None


# Sample shaped from Paystack's docs, NOT a captured payload: the docs name the event
# `charge.dispute.create` but do not publish the payload fields (see task-7-report.md).
def _dispute_payload(reference: str) -> dict:
    return {
        "id": 1234567,
        "status": "awaiting-merchant-feedback",
        "transaction": {"reference": reference},
    }


def test_dispute_event_holds_the_reward_end_to_end(db_session, pending_referred_payment):
    tenant, _, payment = pending_referred_payment
    handler = WebhookHandlerService(db_session)
    handler.handle_event("charge.success", _charge(payment.reference))

    result = handler.handle_event("charge.dispute.create", _dispute_payload(payment.reference))

    assert result["status"] == "success"
    reward = _rewards(db_session, tenant.id)[0]
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["payment_dispute"]


def test_dispute_event_falls_back_to_other_reference_fields(db_session, pending_referred_payment):
    tenant, _, payment = pending_referred_payment
    handler = WebhookHandlerService(db_session)
    handler.handle_event("charge.success", _charge(payment.reference))

    result = handler.handle_event(
        "charge.dispute.create", {"transaction_reference": payment.reference}
    )

    assert result["status"] == "success"
    assert _rewards(db_session, tenant.id)[0].status == RewardStatus.HELD


def test_dispute_event_for_unknown_or_missing_reference_is_skipped(db_session):
    handler = WebhookHandlerService(db_session)
    assert (
        handler.handle_event("charge.dispute.create", _dispute_payload("nope"))["status"]
        == "skipped"
    )
    assert handler.handle_event("charge.dispute.create", {})["status"] == "skipped"


def test_unknown_events_are_still_ignored(db_session):
    result = WebhookHandlerService(db_session).handle_event("charge.dispute.remind", {})
    assert result["status"] == "ignored"


def test_dispute_before_reward_creates_it_held_with_payment_dispute(db_session):
    tenant, _ = _referred(db_session)
    payment = _payment(db_session, tenant, paid_at=NOW - timedelta(hours=3))

    record_dispute(db_session, tenant.id)  # no reward exists yet
    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)

    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["payment_dispute"]


def test_reconcile_after_a_dispute_creates_the_reward_held(db_session):
    tenant, _ = _referred(db_session)
    _payment(db_session, tenant, paid_at=NOW - timedelta(hours=3))
    record_dispute(db_session, tenant.id)

    reconcile_missing_rewards(db_session, datetime.now(UTC))

    (reward,) = _rewards(db_session, tenant.id)
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["payment_dispute"]


def test_dispute_reason_is_not_duplicated_when_flags_also_apply(db_session, monkeypatch):
    monkeypatch.setattr(rewards_module, "evaluate_flags", lambda db, tid: ["shared_domain"])
    tenant, _ = _referred(db_session)
    payment = _payment(db_session, tenant, paid_at=NOW)
    record_dispute(db_session, tenant.id)
    record_dispute(db_session, tenant.id)

    reward = create_reward_for_first_payment(db_session, tenant.id, payment.reference, NOW)

    assert reward.review_reasons == ["shared_domain", "payment_dispute"]


def test_dispute_stamp_keeps_the_earliest_time(db_session):
    tenant, _ = _referred(db_session)
    first = NOW - timedelta(days=2)
    record_dispute(db_session, tenant.id, now=first)
    record_dispute(db_session, tenant.id, now=NOW)

    attribution = db_session.scalar(
        select(ReferralAttribution).where(ReferralAttribution.tenant_id == tenant.id)
    )
    assert _utc(attribution.disputed_at) == first


def test_dispute_event_before_reward_stamps_the_attribution(db_session, pending_referred_payment):
    """Webhook level: dispute lands, then the reward is created later (reconcile path)."""
    tenant, _, payment = pending_referred_payment
    payment.status = "success"
    payment.paid_at = NOW - timedelta(hours=1)
    db_session.commit()

    result = WebhookHandlerService(db_session).handle_event(
        "charge.dispute.create", _dispute_payload(payment.reference)
    )
    assert result["status"] == "success"
    assert result["reward_status"] is None  # no reward yet

    reconcile_missing_rewards(db_session, datetime.now(UTC))
    (reward,) = _rewards(db_session, tenant.id)
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["payment_dispute"]


@pytest.mark.parametrize(
    "payload",
    [
        {"transaction": "just-a-string"},
        {"transaction": None},
        {"transaction": ["ref"]},
        {"transaction": {"reference": 12345}},
        {"reference": 12345},
        {"reference": "   "},
        {"reference": "x" * 101},
        None,
        [],
    ],
)
def test_dispute_event_with_malformed_payload_is_skipped_not_500(db_session, payload):
    result = WebhookHandlerService(db_session).handle_event("charge.dispute.create", payload)
    assert result["status"] == "skipped"


# --------------------------------------------------------------------------- real concurrency


def _committed_fixture(pg_engine):
    """Commit a referred tenant with a pending payment using a throwaway session."""
    session = sessionmaker(bind=pg_engine)()
    try:
        tenant = _tenant(session)
        marketer = _marketer(session)
        _attribute(session, tenant, marketer)
        payment = _payment(session, tenant, status="pending")
        session.commit()
        return tenant.id, marketer.id, payment.id, payment.reference
    finally:
        session.close()


def _purge_committed(pg_engine, tenant_id, marketer_id):
    session = sessionmaker(bind=pg_engine)()
    try:
        session.execute(
            text("DELETE FROM referral_rewards WHERE marketer_id = :m"), {"m": marketer_id}
        )
        session.execute(
            text("DELETE FROM subscription_history WHERE tenant_id = :t"), {"t": tenant_id}
        )
        session.execute(
            text("DELETE FROM payment_transactions WHERE tenant_id = :t"), {"t": tenant_id}
        )
        session.execute(
            text("DELETE FROM referral_attributions WHERE tenant_id = :t"), {"t": tenant_id}
        )
        session.execute(
            text("DELETE FROM tenant_capabilities WHERE tenant_id = :t"), {"t": tenant_id}
        )
        session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        session.execute(text("DELETE FROM marketers WHERE id = :m"), {"m": marketer_id})
        session.commit()
    finally:
        session.close()


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


def test_replay_and_parallel_delivery_one_reward(pg_engine):
    """Two real sessions deliver the same charge.success: one reward, one history row."""
    tenant_id, marketer_id, _, reference = _committed_fixture(pg_engine)
    barrier = threading.Barrier(2, timeout=15)
    factory = sessionmaker(bind=pg_engine)

    def deliver():
        session = factory()
        try:
            barrier.wait()
            return WebhookHandlerService(session).handle_event("charge.success", _charge(reference))
        finally:
            session.close()

    try:
        outcomes = _run_in_threads([deliver, deliver])

        errors = [o for o in outcomes if isinstance(o, BaseException)]
        assert not errors, errors
        assert sorted(o["status"] for o in outcomes) == ["already_processed", "success"]

        check = factory()
        try:
            rewards = (
                check.execute(select(ReferralReward).where(ReferralReward.tenant_id == tenant_id))
                .scalars()
                .all()
            )
            history = check.scalar(
                select(func.count(SubscriptionHistory.id)).where(
                    SubscriptionHistory.payment_reference == reference
                )
            )
            payment_status = check.scalar(
                select(PaymentTransaction.status).where(PaymentTransaction.reference == reference)
            )
        finally:
            check.close()
        assert len(rewards) == 1
        assert rewards[0].amount_kobo == 500_000
        assert history == 1
        assert payment_status == "success"
    finally:
        _purge_committed(pg_engine, tenant_id, marketer_id)


def _mark_paid(pg_engine, payment_id, paid_at):
    session = sessionmaker(bind=pg_engine)()
    try:
        payment = session.get(PaymentTransaction, payment_id)
        payment.status = "success"
        payment.paid_at = paid_at
        session.commit()
    finally:
        session.close()


def _count_rewards(factory, tenant_id):
    check = factory()
    try:
        return check.scalar(
            select(func.count(ReferralReward.id)).where(ReferralReward.tenant_id == tenant_id)
        )
    finally:
        check.close()


def test_concurrent_creation_for_the_same_tenant_yields_one_row(pg_engine):
    """Two real sessions create for the same tenant: the attribution lock serialises them."""
    tenant_id, marketer_id, payment_id, reference = _committed_fixture(pg_engine)
    factory = sessionmaker(bind=pg_engine)
    paid_at = datetime.now(UTC)
    _mark_paid(pg_engine, payment_id, paid_at)
    barrier = threading.Barrier(2, timeout=15)

    def create():
        session = factory()
        try:
            barrier.wait()
            reward = create_reward_for_first_payment(session, tenant_id, reference, paid_at)
            session.commit()
            return reward is not None
        finally:
            session.close()

    try:
        outcomes = _run_in_threads([create, create])

        errors = [o for o in outcomes if isinstance(o, BaseException)]
        assert not errors, errors
        assert sorted(outcomes) == [False, True]  # exactly one call created the row
        assert _count_rewards(factory, tenant_id) == 1
    finally:
        _purge_committed(pg_engine, tenant_id, marketer_id)


def test_unique_violation_after_the_existence_check_is_treated_as_already_rewarded(
    pg_engine, monkeypatch
):
    """The last line of defence: a competing reward committed between check and insert."""
    tenant_id, marketer_id, payment_id, reference = _committed_fixture(pg_engine)
    factory = sessionmaker(bind=pg_engine)
    paid_at = datetime.now(UTC)
    _mark_paid(pg_engine, payment_id, paid_at)
    real_flags = rewards_module.evaluate_flags

    def flags_then_rival_commits(db, tid):
        flags = real_flags(db, tid)
        rival = factory()
        try:
            rival.add(
                ReferralReward(
                    tenant_id=tenant_id,
                    marketer_id=marketer_id,
                    amount_kobo=500_000,
                    status=RewardStatus.PENDING,
                    converted_at=paid_at,
                    payable_after=paid_at + timedelta(days=7),
                )
            )
            rival.commit()
        finally:
            rival.close()
        return flags

    monkeypatch.setattr(rewards_module, "evaluate_flags", flags_then_rival_commits)
    try:
        session = factory()
        try:
            assert create_reward_for_first_payment(session, tenant_id, reference, paid_at) is None
            session.commit()
        finally:
            session.close()
        assert _count_rewards(factory, tenant_id) == 1
    finally:
        _purge_committed(pg_engine, tenant_id, marketer_id)


def test_dispute_racing_reward_creation_never_leaves_a_clean_pending_reward(pg_engine, monkeypatch):
    """Creation has read the attribution when the dispute arrives: the dispute waits, then holds."""
    tenant_id, marketer_id, payment_id, reference = _committed_fixture(pg_engine)
    factory = sessionmaker(bind=pg_engine)
    paid_at = datetime.now(UTC)
    _mark_paid(pg_engine, payment_id, paid_at)
    creation_inside = threading.Event()
    dispute_started = threading.Event()
    real_flags = rewards_module.evaluate_flags

    def paused_flags(db, tid):
        flags = real_flags(db, tid)
        creation_inside.set()
        dispute_started.wait(timeout=15)
        time.sleep(0.5)  # let the dispute reach (and block on) the attribution lock
        return flags

    monkeypatch.setattr(rewards_module, "evaluate_flags", paused_flags)

    def create():
        session = factory()
        try:
            reward = create_reward_for_first_payment(session, tenant_id, reference, paid_at)
            session.commit()
            return reward is not None
        finally:
            session.close()

    def dispute():
        assert creation_inside.wait(timeout=15)
        session = factory()
        try:
            dispute_started.set()
            record_dispute(session, tenant_id)
            session.commit()
            return True
        finally:
            session.close()

    try:
        outcomes = _run_in_threads([create, dispute])

        errors = [o for o in outcomes if isinstance(o, BaseException)]
        assert not errors, errors
        check = factory()
        try:
            reward = check.execute(
                select(ReferralReward).where(ReferralReward.tenant_id == tenant_id)
            ).scalar_one()
            stamped = check.scalar(
                select(ReferralAttribution.disputed_at).where(
                    ReferralAttribution.tenant_id == tenant_id
                )
            )
        finally:
            check.close()
        assert reward.status == RewardStatus.HELD
        assert "payment_dispute" in reward.review_reasons
        assert stamped is not None
    finally:
        _purge_committed(pg_engine, tenant_id, marketer_id)
