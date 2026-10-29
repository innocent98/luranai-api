"""Daily referral job: promote pending rewards past their hold and repair missing rewards."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    AttributionSource,
    Marketer,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.workers import referral as referral_worker
from app.workers.document_processor import WorkerSettings

NOW = datetime.now(UTC)


class _NoCloseSession:
    """Proxy that swallows close() so the test can keep using its transaction."""

    def __init__(self, session):
        self._session = session
        self.closed = False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        return getattr(self._session, name)


def _tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Job Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        created_at=NOW - timedelta(days=20),
    )
    db.add(tenant)
    db.flush()
    return tenant


def _marketer(db) -> Marketer:
    marketer = Marketer(
        name="Job Marketer",
        email=f"m_{uuid4().hex[:6]}@marketer-co.example",
        code=f"code-{uuid4().hex[:8]}",
    )
    db.add(marketer)
    db.flush()
    return marketer


def test_job_promotes_due_rewards_and_reconciles_missing_ones(db_session, monkeypatch):
    marketer = _marketer(db_session)

    due_tenant = _tenant(db_session)
    due = ReferralReward(
        tenant_id=due_tenant.id,
        marketer_id=marketer.id,
        amount_kobo=500_000,
        status=RewardStatus.PENDING,
        converted_at=NOW - timedelta(days=9),
        payable_after=NOW - timedelta(days=2),
    )
    db_session.add(due)

    missing_tenant = _tenant(db_session)
    db_session.add(
        ReferralAttribution(
            tenant_id=missing_tenant.id,
            marketer_id=marketer.id,
            source=AttributionSource.COOKIE,
            attributed_at=missing_tenant.created_at,
        )
    )
    db_session.add(
        PaymentTransaction(
            tenant_id=missing_tenant.id,
            reference=f"ref_{uuid4().hex[:12]}",
            amount=1_000_000,
            tier="professional",
            billing_cycle="monthly",
            status="success",
            paid_at=NOW - timedelta(days=1),
        )
    )
    db_session.flush()

    session = _NoCloseSession(db_session)
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)

    result = asyncio.run(referral_worker.referral_promote_pending({}))

    assert result["promoted"] >= 1
    assert result["reconciled"] >= 1
    assert session.closed is True
    db_session.refresh(due)
    assert due.status == RewardStatus.PAYABLE
    created = db_session.execute(
        select(ReferralReward).where(ReferralReward.tenant_id == missing_tenant.id)
    ).scalar_one()
    assert created.status == RewardStatus.PENDING

    reward_id = created.id
    asyncio.run(referral_worker.referral_promote_pending({}))  # second run: nothing new for us
    db_session.expire_all()
    rows = (
        db_session.execute(select(ReferralReward).where(ReferralReward.marketer_id == marketer.id))
        .scalars()
        .all()
    )
    assert sorted(str(r.tenant_id) for r in rows) == sorted(
        [str(due_tenant.id), str(missing_tenant.id)]
    )
    assert {r.id for r in rows if r.tenant_id == missing_tenant.id} == {reward_id}


def test_one_step_failing_does_not_stop_the_other(db_session, monkeypatch):
    def boom(db, now):
        raise RuntimeError("promote failed")

    monkeypatch.setattr(referral_worker, "promote_pending", boom)
    called = []
    monkeypatch.setattr(
        referral_worker, "reconcile_missing_rewards", lambda db, now: called.append(1) or 0
    )
    session = _NoCloseSession(db_session)
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)

    result = asyncio.run(referral_worker.referral_promote_pending({}))

    assert called == [1]
    assert result["errors"] == 1
    assert session.closed is True


def test_job_is_registered_daily_at_0300_utc():
    jobs = [
        j
        for j in WorkerSettings.cron_jobs
        if j.coroutine is referral_worker.referral_promote_pending
    ]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.hour in (3, {3})
    assert job.minute in (0, {0})
    assert job.run_at_startup is False


# --- payout batch preparation (1st and 15th, 04:00 UTC) -----------------------------------------


def _payable_reward(db, marketer) -> ReferralReward:
    reward = ReferralReward(
        tenant_id=_tenant(db).id,
        marketer_id=marketer.id,
        amount_kobo=500_000,
        status=RewardStatus.PAYABLE,
        converted_at=NOW - timedelta(days=12),
        payable_after=NOW - timedelta(days=5),
    )
    db.add(reward)
    db.flush()
    return reward


def test_prepare_batch_job_creates_one_batch_and_a_second_run_creates_none(db_session, monkeypatch):
    from app.db.models.referral import BatchStatus, PayoutBatch, PayoutItem

    marketer = _marketer(db_session)
    reward = _payable_reward(db_session, marketer)
    session = _NoCloseSession(db_session)
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    before = db_session.scalar(select(func.count()).select_from(PayoutBatch))

    first = asyncio.run(referral_worker.referral_prepare_batch({}))
    second = asyncio.run(referral_worker.referral_prepare_batch({}))

    assert first is not None and first["items"] >= 1 and first["total_kobo"] >= 500_000
    assert second is None
    assert session.closed is True
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(PayoutBatch)) == before + 1
    batch = db_session.get(PayoutBatch, first["batch_id"])
    assert batch.status == BatchStatus.DRAFT  # nothing is approved or sent by the job
    assert db_session.get(ReferralReward, reward.id).status == RewardStatus.BATCHED
    items = db_session.scalars(select(PayoutItem).where(PayoutItem.batch_id == batch.id)).all()
    assert all(i.status.value == "pending" and i.transfer_reference is None for i in items)


def test_prepare_batch_job_with_nothing_payable_returns_none(db_session, monkeypatch):
    session = _NoCloseSession(db_session)
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(referral_worker, "prepare_batch", lambda db, now: None)

    assert asyncio.run(referral_worker.referral_prepare_batch({})) is None
    assert session.closed is True


def test_prepare_batch_job_failure_rolls_back_closes_and_logs_class_only(db_session, monkeypatch):
    from loguru import logger

    class Session:
        closed = rolled_back = False

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            raise AssertionError("must not commit a failed run")

        def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)

    def boom(db, now):
        raise RuntimeError("secret-detail-in-exception")

    monkeypatch.setattr(referral_worker, "prepare_batch", boom)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(referral_worker.referral_prepare_batch({}))
    finally:
        logger.remove(sink)

    assert session.rolled_back is True and session.closed is True
    logged = "\n".join(messages)
    assert "RuntimeError" in logged
    assert "secret-detail-in-exception" not in logged


class _PgError(Exception):
    """Stands in for a psycopg2 error: only ``pgcode`` matters."""

    def __init__(self, pgcode: str, message: str = "boom"):
        super().__init__(message)
        self.pgcode = pgcode


def _db_error(pgcode: str, message: str = "boom"):
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "UPDATE secret_table SET x = :p", {"p": "SECRET-PARAM"}, _PgError(pgcode, message)
    )


class _CountingSession:
    closed = False

    def __init__(self):
        self.rollbacks = 0
        self.commits = 0

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_prepare_batch_job_retries_a_deadlock_and_then_succeeds(monkeypatch):
    session = _CountingSession()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(referral_worker, "PREPARE_RETRY_DELAY_SECONDS", 0)
    calls = []

    def flaky(db, now):
        calls.append(now)
        if len(calls) <= 2:
            raise _db_error("40P01")  # deadlock_detected
        return None

    monkeypatch.setattr(referral_worker, "prepare_batch", flaky)

    assert asyncio.run(referral_worker.referral_prepare_batch({})) is None

    assert len(calls) == 3
    assert session.rollbacks == 2 and session.closed is True


def test_prepare_batch_job_retries_a_serialization_failure(monkeypatch):
    session = _CountingSession()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(referral_worker, "PREPARE_RETRY_DELAY_SECONDS", 0)
    calls = []

    def flaky(db, now):
        calls.append(1)
        if len(calls) == 1:
            raise _db_error("40001")
        return None

    monkeypatch.setattr(referral_worker, "prepare_batch", flaky)
    assert asyncio.run(referral_worker.referral_prepare_batch({})) is None
    assert len(calls) == 2


def test_prepare_batch_job_gives_up_after_three_retries_logging_the_class_only(monkeypatch):
    from loguru import logger

    session = _CountingSession()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(referral_worker, "PREPARE_RETRY_DELAY_SECONDS", 0)
    calls = []

    def always(db, now):
        calls.append(1)
        raise _db_error("40P01", "deadlock detected SECRET-DETAIL")

    monkeypatch.setattr(referral_worker, "prepare_batch", always)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        with pytest.raises(Exception) as exc:
            asyncio.run(referral_worker.referral_prepare_batch({}))
    finally:
        logger.remove(sink)

    assert type(exc.value).__name__ == "OperationalError"
    assert len(calls) == 4  # the first try and three retries
    assert session.closed is True
    errors = [m for m in messages if "ERROR" in m.split("|")[1]] if messages else []
    logged = "\n".join(messages)
    assert "OperationalError" in logged
    assert "SECRET-DETAIL" not in logged and "SECRET-PARAM" not in logged
    assert any("preparation failed" in m for m in errors)


def test_prepare_batch_job_does_not_retry_other_errors(monkeypatch):
    session = _CountingSession()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: session)
    monkeypatch.setattr(referral_worker, "PREPARE_RETRY_DELAY_SECONDS", 0)
    calls = []

    def other(db, now):
        calls.append(1)
        raise _db_error("23505")  # unique violation: not a lock conflict

    monkeypatch.setattr(referral_worker, "prepare_batch", other)
    with pytest.raises(OperationalError):
        asyncio.run(referral_worker.referral_prepare_batch({}))
    assert len(calls) == 1


def test_prepare_batch_job_is_registered_on_the_1st_and_15th_at_0400_utc():
    jobs = [
        j for j in WorkerSettings.cron_jobs if j.coroutine is referral_worker.referral_prepare_batch
    ]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.day == {1, 15}
    assert job.hour in (4, {4})
    assert job.minute in (0, {0})
    assert job.run_at_startup is False
    assert referral_worker.referral_prepare_batch in WorkerSettings.functions
