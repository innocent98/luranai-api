"""Marketer notification emails: a conversion notice and a payout notice.

Each email is sent after the commit that made the transition real, exactly once per reward / per
item, and a failure to send can never undo the reward or the payout. Most tests let the real
templates render and capture what would be sent by stubbing only the SMTP-level ``_send_email``;
nothing here touches a mail server.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    BatchStatus,
    MarketerStatus,
    PayoutItemStatus,
    ReferralReward,
    RewardStatus,
)
from app.db.models.subscription import SubscriptionHistory
from app.services import email_service as email_module
from app.services.email_service import EmailService
from app.services.referral import payouts as payouts_module
from app.services.referral import rewards as rewards_module
from app.services.referral.payouts import (
    AmountMismatchError,
    BankReferenceInUseError,
    ItemNotMarkableError,
    apply_transfer_event,
    approve_batch,
    mark_paid,
    retry_batch,
)
from app.services.referral.notifications import (
    send_payout_notices,
    send_queued_conversion_notices,
)
from app.services.referral.providers import (
    ManualPayoutProvider,
    TransferResult,
    TransferVerification,
)
from app.services.webhook_handler_service import WebhookHandlerService
from tests.referral.test_payouts import (
    FakeProvider,
    _batch_of,
    _item,
    _pending_for,
)
from tests.referral.test_rewards import (
    _attribute,
    _charge,
    _marketer,
    _payment,
    _tenant,
)

# Wording that must never appear in a marketer-facing notice (fraud, flag and hold language).
FORBIDDEN_WORDING = re.compile(
    r"\b(flag\w*|fraud\w*|held|hold\w*|review\w*|suspect\w*|suspend\w*|shared_domain|blocked)\b"
)


@pytest.fixture
def sent(monkeypatch):
    """Everything EmailService would put on the wire, rendered but not sent."""
    box: list[SimpleNamespace] = []

    def fake_send(self, to_email, subject, html_content, text_content=None, attachments=None):
        box.append(SimpleNamespace(to=to_email, subject=subject, html=html_content))
        return True

    monkeypatch.setattr(EmailService, "_send_email", fake_send)
    return box


def _to(sent, marketer):
    return [mail for mail in sent if mail.to == marketer.email]


def _referred_payment(db, *, marketer_status=MarketerStatus.ACTIVE, display_name=None):
    tenant = _tenant(db)
    if display_name:
        tenant.display_name = display_name
    marketer = _marketer(db, marketer_status)
    _attribute(db, tenant, marketer)
    payment = _payment(db, tenant, status="pending")
    db.commit()
    return tenant, marketer, payment


def _reward_of(db, tenant):
    db.expire_all()
    return db.scalar(select(ReferralReward).where(ReferralReward.tenant_id == tenant.id))


# --------------------------------------------------------------------------- conversion


def test_conversion_email_sent_once_per_reward(db_session, sent):
    tenant, marketer, payment = _referred_payment(db_session, display_name="Placeholder Stores Ltd")
    handler = WebhookHandlerService(db_session)

    handler.handle_event("charge.success", _charge(payment.reference))

    mails = _to(sent, marketer)
    assert len(mails) == 1
    assert "5,000.00" in mails[0].html  # 500 000 kobo
    assert "Placeholder Stores Ltd" in mails[0].html
    assert _reward_of(db_session, tenant).status == RewardStatus.PENDING

    # a replay of the same event, and a direct second creation attempt, send nothing more
    replay = handler.handle_event("charge.success", _charge(payment.reference))
    assert replay == {"status": "already_processed"}
    assert (
        rewards_module.create_reward_for_first_payment(
            db_session, tenant.id, payment.reference, payment.paid_at
        )
        is None
    )
    assert len(_to(sent, marketer)) == 1


def test_email_failure_does_not_roll_back_reward_or_payout(
    db_session, super_admin_user, monkeypatch
):
    """Both mails raise: the reward, the payment, the transfer and the batch all stay committed."""
    calls = []

    def boom(self, *args, **kwargs):
        calls.append(args or kwargs)
        raise RuntimeError("smtp exploded for secret-person@example.com")

    monkeypatch.setattr(EmailService, "send_referral_conversion_email", boom)
    monkeypatch.setattr(EmailService, "send_referral_payout_email", boom)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")

    try:
        # conversion
        tenant, marketer, payment = _referred_payment(db_session)
        result = WebhookHandlerService(db_session).handle_event(
            "charge.success", _charge(payment.reference)
        )
        assert result["status"] == "success"
        db_session.expire_all()
        assert db_session.get(PaymentTransaction, payment.id).status == "success"
        assert _reward_of(db_session, tenant) is not None

        # payout
        batch, (payee,), (reward,) = _batch_of(db_session, 1)
        approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())
    finally:
        logger.remove(sink)

    assert len(calls) == 2  # both sends were attempted
    db_session.expire_all()
    assert _item(db_session, batch, payee).status == PayoutItemStatus.PAID
    assert db_session.get(ReferralReward, reward.id).status == RewardStatus.PAID
    assert db_session.get(type(batch), batch.id).status == BatchStatus.PAID
    log_text = "\n".join(lines)
    assert "RuntimeError" in log_text  # class is logged
    assert "secret-person@example.com" not in log_text  # the message (and address) never is
    assert payee.email not in log_text and marketer.email not in log_text


def test_a_false_return_from_the_mailer_changes_nothing(db_session, monkeypatch):
    monkeypatch.setattr(EmailService, "_send_email", lambda self, *a, **k: False)
    tenant, _, payment = _referred_payment(db_session)

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    assert _reward_of(db_session, tenant).status == RewardStatus.PENDING


def _record_order(db_session, monkeypatch, marketer_emails):
    """Interleave every ``commit`` and every mail to a marketer into one ordered list.

    (The test session shares one outer transaction, so visibility from a second session cannot
    prove durability here; the order of calls can: the mail must come after the owning commit.)
    """
    events: list[str] = []
    real_commit = db_session.commit

    def commit():
        events.append("commit")
        return real_commit()

    def spy(self, to_email, subject, html_content, text_content=None, attachments=None):
        if to_email in marketer_emails:
            events.append("mail")
        return True

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(EmailService, "_send_email", spy)
    return events


def test_conversion_email_is_sent_after_the_final_commit(db_session, monkeypatch):
    tenant, marketer, payment = _referred_payment(db_session)
    events = _record_order(db_session, monkeypatch, {marketer.email})

    WebhookHandlerService(db_session).handle_event("charge.success", _charge(payment.reference))

    assert events.count("mail") == 1
    assert events[-1] == "mail"  # nothing is committed after the mail goes out
    assert events[events.index("mail") - 1] == "commit"


def test_payout_email_is_sent_right_after_the_commit_that_paid(
    db_session, super_admin_user, monkeypatch
):
    batch, (payee,), _ = _batch_of(db_session, 1)
    events = _record_order(db_session, monkeypatch, {payee.email})

    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())

    assert events.count("mail") == 1
    assert events[events.index("mail") - 1] == "commit"


def test_held_reward_sends_the_same_generic_notice(db_session, sent):
    tenant, marketer, payment = _referred_payment(
        db_session, marketer_status=MarketerStatus.SUSPENDED, display_name="Placeholder Stores Ltd"
    )

    WebhookHandlerService(db_session).handle_event("charge.success", _charge(payment.reference))

    assert _reward_of(db_session, tenant).status == RewardStatus.HELD
    (mail,) = _to(sent, marketer)
    text = (mail.subject + " " + mail.html).lower()
    assert FORBIDDEN_WORDING.search(text) is None, FORBIDDEN_WORDING.search(text)
    assert "5,000.00" in mail.html


def test_conversion_email_has_no_tenant_email_or_plan_names(db_session, sent):
    tenant, marketer, payment = _referred_payment(db_session, display_name="Placeholder Stores Ltd")
    payload = _charge(payment.reference)
    payload["customer"]["email"] = "buyer-private@example.com"

    WebhookHandlerService(db_session).handle_event("charge.success", payload)

    (mail,) = _to(sent, marketer)
    body = mail.html.lower()
    assert "buyer-private@example.com" not in body
    # the only address in the body is the template's own support link, nobody's customer
    assert set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body)) == {"support@luranai.com"}
    for plan in ("professional", "starter", "enterprise", "growth", "trial"):
        assert plan not in body, plan
    assert "pending" not in body  # the status of the reward is not part of the notice


def test_hostile_company_name_is_escaped(db_session, sent):
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    tenant, marketer, payment = _referred_payment(db_session, display_name=hostile)

    WebhookHandlerService(db_session).handle_event("charge.success", _charge(payment.reference))

    (mail,) = _to(sent, marketer)
    assert "<script>alert(1)</script>" not in mail.html
    assert "<img src=x" not in mail.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in mail.html


def test_reconcile_notifies_for_the_reward_it_creates(db_session, sent):
    tenant = _tenant(db_session)
    marketer = _marketer(db_session)
    _attribute(db_session, tenant, marketer)
    now = datetime.now(UTC)
    _payment(db_session, tenant, paid_at=now - timedelta(days=1))
    db_session.commit()

    count = rewards_module.reconcile_missing_rewards(db_session, now)
    assert _to(sent, marketer) == []  # nothing goes out before the job commits
    db_session.commit()
    send_queued_conversion_notices(db_session)
    send_queued_conversion_notices(db_session)  # the queue is drained: no second email

    assert count == 1
    assert len(_to(sent, marketer)) == 1


def test_reconcile_job_sends_the_notice_after_its_commit(db_session, sent, monkeypatch):
    import asyncio

    from app.workers import referral as referral_worker
    from tests.workers.test_referral_jobs import _NoCloseSession

    tenant = _tenant(db_session)
    marketer = _marketer(db_session)
    _attribute(db_session, tenant, marketer)
    _payment(db_session, tenant, paid_at=datetime.now(UTC) - timedelta(days=1))
    db_session.commit()
    monkeypatch.setattr(referral_worker, "SessionLocal", lambda: _NoCloseSession(db_session))

    result = asyncio.run(referral_worker.referral_promote_pending({}))

    assert result["reconciled"] == 1
    assert len(_to(sent, marketer)) == 1


# --------------------------------------------------------------------------- payout


def test_payout_email_content(db_session, super_admin_user, sent):
    batch, (payee,), _ = _batch_of(db_session, 1)

    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())

    (mail,) = _to(sent, payee)
    assert "5,000.00" in mail.html
    assert batch.period_label in mail.html
    assert f"ref-{batch.id}-{payee.id}" in mail.html
    text = (mail.subject + " " + mail.html).lower()
    assert FORBIDDEN_WORDING.search(text) is None, FORBIDDEN_WORDING.search(text)


def test_payout_email_once_for_sync_success_and_a_later_duplicate_webhook(
    db_session, super_admin_user, sent
):
    batch, (payee,), _ = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())
    assert len(_to(sent, payee)) == 1
    reference = f"ref-{batch.id}-{payee.id}"

    again = apply_transfer_event(
        db_session, "transfer.success", {"reference": reference, "amount": 500_000}
    )
    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())

    assert again["status"] == "duplicate"
    assert len(_to(sent, payee)) == 1


def test_payout_email_once_for_a_webhook_success_then_replay(db_session, super_admin_user, sent):
    batch, (payee,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(payee.id))
    )
    assert _to(sent, payee) == []  # still processing: nothing was paid yet
    event = {"reference": f"ref-{batch.id}-{payee.id}", "amount": 500_000}

    first = apply_transfer_event(db_session, "transfer.success", event)
    replay = apply_transfer_event(db_session, "transfer.success", event)

    assert (first["status"], replay["status"]) == ("processed", "duplicate")
    assert len(_to(sent, payee)) == 1


def test_payout_email_once_for_verify_based_recovery(db_session, super_admin_user, sent):
    batch, (payee,), _ = _batch_of(db_session, 1)
    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_pending_for(payee.id))
    )
    provider = FakeProvider(
        verify_fn=lambda ref: TransferVerification("success", ref, "success", 500_000)
    )

    retry_batch(db_session, batch.id, super_admin_user.id, provider)
    retry_batch(db_session, batch.id, super_admin_user.id, provider)

    assert provider.pay_calls == []  # never re-sent
    assert len(_to(sent, payee)) == 1


def test_mark_paid_sends_once(db_session, super_admin_user, sent):
    batch, (a, b), _ = _batch_of(db_session, 2)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    assert sent == []  # awaiting_manual is not a payment

    mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 500_000)], super_admin_user.id)
    assert len(_to(sent, a)) == 1 and _to(sent, b) == []
    assert "BANK-REF-A" in _to(sent, a)[0].html

    # a second call for the same item is refused and sends nothing
    with pytest.raises(ItemNotMarkableError):
        mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A2", 500_000)], super_admin_user.id)
    mark_paid(db_session, batch.id, [(b.id, "BANK-REF-B", 500_000)], super_admin_user.id)

    assert len(_to(sent, a)) == 1
    assert len(_to(sent, b)) == 1


def test_a_refused_mark_paid_sends_nothing(db_session, super_admin_user, sent):
    batch, (a,), _ = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())

    with pytest.raises(AmountMismatchError):
        mark_paid(db_session, batch.id, [(a.id, "BANK-REF-A", 1)], super_admin_user.id)

    assert sent == []


def test_failed_and_pending_transfers_send_nothing(db_session, super_admin_user, sent):
    batch, (a, b), _ = _batch_of(db_session, 2)

    approve_batch(
        db_session, batch.id, super_admin_user.id, FakeProvider(pay_fn=_mixed(a.id, b.id))
    )

    assert sent == []


def _mixed(failing_id, pending_id):
    def fn(marketer, amount, reference):
        if marketer.id == failing_id:
            return TransferResult("failed", reference, "rejected", "insufficient balance")
        return TransferResult("pending", reference, "pending")

    return fn


def test_payout_mail_failure_for_one_item_does_not_stop_the_others(
    db_session, super_admin_user, monkeypatch
):
    batch, (a, b), _ = _batch_of(db_session, 2)
    attempts = []

    def flaky(self, marketer_email, *args, **kwargs):
        attempts.append(marketer_email)
        raise OSError("connection refused")

    monkeypatch.setattr(EmailService, "send_referral_payout_email", flaky)

    approve_batch(db_session, batch.id, super_admin_user.id, FakeProvider())

    assert sorted(attempts) == sorted([a.email, b.email])
    assert _item(db_session, batch, a).status == PayoutItemStatus.PAID
    assert _item(db_session, batch, b).status == PayoutItemStatus.PAID


def test_mark_paid_through_the_endpoint_sends_the_payout_email(
    pg_client, db_session, super_admin_token, super_admin_user, sent
):
    batch, (payee,), _ = _batch_of(db_session, 1)
    headers = {"Authorization": f"Bearer {super_admin_token}"}
    base = "/api/v1/platform/referrals"
    approve = pg_client.post(f"{base}/batches/{batch.id}/approve", headers=headers)
    assert approve.status_code == 200, approve.text

    paid = pg_client.post(
        f"{base}/batches/{batch.id}/mark-paid",
        json={
            "items": [
                {
                    "marketer_id": str(payee.id),
                    "bank_reference": "BANK-REF-ENDPOINT",
                    "amount_kobo": 500_000,
                }
            ]
        },
        headers=headers,
    )

    assert paid.status_code == 200, paid.text
    (mail,) = _to(sent, payee)
    assert "BANK-REF-ENDPOINT" in mail.html


def test_a_mark_paid_that_rolls_back_announces_nothing(
    db_session, super_admin_user, sent, monkeypatch
):
    """A unique-violation on the bank reference rolls the whole call back; the queued notice goes."""
    batch, (payee,), _ = _batch_of(db_session, 1)
    approve_batch(db_session, batch.id, super_admin_user.id, ManualPayoutProvider())
    real = payouts_module._mark_item_paid

    def mark_then_collide(db, item, reference, now, provider_status):
        real(db, item, reference, now, provider_status)  # queues the notice
        raise IntegrityError("INSERT", {}, Exception("duplicate transfer_reference"))

    monkeypatch.setattr(payouts_module, "_mark_item_paid", mark_then_collide)

    with pytest.raises(BankReferenceInUseError):
        mark_paid(db_session, batch.id, [(payee.id, "BANK-REF-DUP", 500_000)], super_admin_user.id)

    assert sent == []
    assert "referral_payout_notices" not in db_session.info  # dropped by the rollback hook
    send_payout_notices(db_session)  # even a later drain has nothing to announce
    assert sent == []
    # (the item's state is not re-read: this fixture's shared outer transaction means the
    # rollback also discards the test's own setup rows)


def test_smtp_connections_carry_a_socket_timeout(monkeypatch):
    seen = []

    class FakeServer:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("timeout"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, *args):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(email_module.smtplib, "SMTP_SSL", FakeServer)
    monkeypatch.setattr(email_module.settings, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(email_module.settings, "EMAILS_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setattr(email_module.settings, "EMAILS_FROM_NAME", "Luran")
    for port in (587, 465):
        monkeypatch.setattr(email_module.settings, "SMTP_PORT", port)
        assert EmailService()._send_email("to@example.com", "s", "<p>x</p>") is True

    assert seen == [email_module.SMTP_TIMEOUT_SECONDS] * 2
    assert 0 < email_module.SMTP_TIMEOUT_SECONDS <= 30


def test_a_flush_failure_inside_the_reward_savepoint_cannot_break_the_payment(
    db_session, sent, monkeypatch
):
    """The savepoint is deactivated by a failed FLUSH (not just failed SQL); the handler must still
    return success and its final commit must work, with the payment, upgrade and history kept."""
    tenant, marketer, payment = _referred_payment(db_session)

    def insert_a_row_the_database_refuses(db, tenant_id, reference, paid_at):
        db.add(
            ReferralReward(
                tenant_id=tenant_id,
                marketer_id=uuid4(),  # no such marketer: the foreign key fails at flush
                amount_kobo=1,
                status=RewardStatus.PENDING,
                converted_at=datetime.now(UTC),
                payable_after=datetime.now(UTC),
            )
        )
        db.flush()

    monkeypatch.setattr(
        rewards_module, "create_reward_for_first_payment", insert_a_row_the_database_refuses
    )

    result = WebhookHandlerService(db_session).handle_event(
        "charge.success", _charge(payment.reference)
    )

    assert result["status"] == "success"
    db_session.commit()  # nothing is left pending a rollback
    db_session.expire_all()
    assert db_session.get(PaymentTransaction, payment.id).status == "success"
    history = db_session.scalars(
        select(SubscriptionHistory).where(
            SubscriptionHistory.payment_reference == payment.reference
        )
    ).all()
    assert len(history) == 1
    assert _reward_of(db_session, tenant) is None
    assert _to(sent, marketer) == []  # no reward, so no notice
