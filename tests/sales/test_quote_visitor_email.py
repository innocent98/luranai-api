"""Deciding whether a visitor's quote is emailed: validity, one per quote, and the abuse caps."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.sales.quote_email import SentOutcome
from app.services import quote_email_service as svc

LINES = [{"label": "Growth", "detail": "11–50 users", "amount": 45000}]


def _tenant(db, *, notification_email="rep@example.com"):
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Acme Ltd",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notification_email = notification_email
    db.add(cfg)
    db.flush()
    return t


def _quote(db, tenant, *, email="alice@example.com"):
    lead = Lead(tenant_id=tenant.id, conversation_id=uuid4(), name="Alice", email=email)
    db.add(lead)
    db.flush()
    q = Quote(
        tenant_id=tenant.id,
        lead_id=lead.id,
        currency="NGN",
        total=45000,
        line_items=LINES,
        status=QuoteStatus.DRAFT,
    )
    db.add(q)
    db.commit()
    return q


@pytest.fixture
def enqueue():
    return AsyncMock()


@pytest.fixture(autouse=True)
def open_limiters(monkeypatch):
    """The Redis limiters allow everything unless a test says otherwise."""
    allow = AsyncMock()
    monkeypatch.setattr(svc, "enforce_identifier_limit", allow)
    return allow


async def _go(db, quote, enqueue, ip="203.0.113.7"):
    return await svc.email_quote_to_visitor(db, quote, client_ip=ip, enqueue=enqueue)


async def test_a_quote_for_a_lead_with_an_email_is_queued_and_logged(db_session, enqueue):
    t = _tenant(db_session)
    q = _quote(db_session, t, email="Alice@Example.com")
    outcome = await _go(db_session, q, enqueue)
    assert outcome == SentOutcome.queued("a***@example.com")
    log = db_session.query(NotificationLog).filter_by(event=NotifEvent.QUOTE_EMAILED_VISITOR).one()
    assert (log.tenant_id, log.entity_type, log.entity_id) == (t.id, "quote", q.id)
    assert log.channel == NotifChannel.EMAIL and log.status == NotifStatus.PENDING
    assert log.recipient == "alice@example.com"  # normalised
    enqueue.assert_awaited_once_with([(log.id, "send_visitor_quote_email")])


async def test_only_one_email_per_quote(db_session, enqueue):
    q = _quote(db_session, _tenant(db_session))
    await _go(db_session, q, enqueue)
    assert await _go(db_session, q, enqueue) is None
    assert enqueue.await_count == 1


async def test_a_second_quote_for_the_same_lead_gets_its_own_email(db_session, enqueue):
    t = _tenant(db_session)
    q1 = _quote(db_session, t)
    q2 = Quote(
        tenant_id=t.id, lead_id=q1.lead_id, currency="NGN", total=9, line_items=LINES,
        status=QuoteStatus.DRAFT,
    )
    db_session.add(q2)
    db_session.commit()
    await _go(db_session, q1, enqueue)
    assert (await _go(db_session, q2, enqueue)).kind == "queued"


async def test_an_identical_quote_the_visitor_already_received_is_not_sent_again(db_session, enqueue):
    """The model may offer the same quote on several turns: one email, not one per turn."""
    t = _tenant(db_session)
    q1 = _quote(db_session, t)
    q2 = Quote(
        tenant_id=t.id, lead_id=q1.lead_id, currency=q1.currency, total=q1.total,
        line_items=list(q1.line_items), status=QuoteStatus.DRAFT,
    )
    db_session.add(q2)
    db_session.commit()
    assert (await _go(db_session, q1, enqueue)).kind == "queued"
    assert await _go(db_session, q2, enqueue) is None
    assert enqueue.await_count == 1


@pytest.mark.parametrize("email", [None, "", "not-an-email", "a@b", "x@" + "a" * 300 + ".com"])
async def test_no_usable_email_means_no_send_and_no_log(db_session, enqueue, email):
    q = _quote(db_session, _tenant(db_session), email=email)
    assert await _go(db_session, q, enqueue) is None
    enqueue.assert_not_awaited()
    assert db_session.query(NotificationLog).count() == 0


async def test_the_kill_switch_turns_it_off(db_session, enqueue, monkeypatch):
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_ENABLED", False)
    t = _tenant(db_session)
    q = _quote(db_session, t)
    assert await _go(db_session, q, enqueue) is None
    assert svc.can_email_quote(db_session, t.id) is False
    enqueue.assert_not_awaited()


async def test_without_a_rep_address_to_reply_to_nothing_is_sent(db_session, enqueue):
    t = _tenant(db_session, notification_email=None)  # and no admin user
    q = _quote(db_session, t)
    assert await _go(db_session, q, enqueue) is None
    assert svc.can_email_quote(db_session, t.id) is False


async def test_the_per_tenant_daily_cap(db_session, enqueue, monkeypatch):
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_TENANT_DAILY_CAP", 2)
    t = _tenant(db_session)
    results = []
    for i in range(3):
        results.append(await _go(db_session, _quote(db_session, t, email=f"v{i}@example.com"), enqueue))
    assert [r.kind for r in results] == ["queued", "queued", "capped"]
    assert enqueue.await_count == 2


def _old_log(db, tenant, recipient, *, hours_ago, status=NotifStatus.SENT):
    log = NotificationLog(
        tenant_id=tenant.id, event=NotifEvent.QUOTE_EMAILED_VISITOR, channel=NotifChannel.EMAIL,
        entity_type="quote", entity_id=uuid4(), recipient=recipient, status=status,
        body=svc.recipient_key(recipient),
    )
    db.add(log)
    db.flush()
    log.created_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.commit()


async def test_a_single_address_gets_at_most_three_a_day_across_all_tenants(db_session, enqueue):
    other = _tenant(db_session)
    for _ in range(3):
        _old_log(db_session, other, "victim@example.com", hours_ago=2)
    mine = _tenant(db_session)
    outcome = await _go(db_session, _quote(db_session, mine, email="victim@example.com"), enqueue)
    assert outcome.kind == "capped"
    enqueue.assert_not_awaited()


async def test_plus_addressing_does_not_dodge_the_per_address_cap(db_session, enqueue):
    other = _tenant(db_session)
    for i in range(3):
        _old_log(db_session, other, f"victim+{i}@example.com", hours_ago=1)
    q = _quote(db_session, _tenant(db_session), email="Victim+zzz@example.com")
    assert (await _go(db_session, q, enqueue)).kind == "capped"


async def test_old_and_failed_sends_do_not_count_against_the_address(db_session, enqueue):
    other = _tenant(db_session)
    for _ in range(3):
        _old_log(db_session, other, "victim@example.com", hours_ago=30)  # outside 24 h
    for _ in range(3):
        _old_log(db_session, other, "victim@example.com", hours_ago=1, status=NotifStatus.FAILED)
    q = _quote(db_session, _tenant(db_session), email="victim@example.com")
    assert (await _go(db_session, q, enqueue)).kind == "queued"


async def test_a_session_or_ip_over_its_limit_is_capped(db_session, enqueue, open_limiters):
    open_limiters.side_effect = HTTPException(status_code=429)
    q = _quote(db_session, _tenant(db_session))
    assert (await _go(db_session, q, enqueue)).kind == "capped"
    enqueue.assert_not_awaited()
    assert db_session.query(NotificationLog).count() == 0


async def test_session_and_ip_are_both_limited_but_a_missing_ip_is_skipped(
    db_session, enqueue, open_limiters
):
    q = _quote(db_session, _tenant(db_session))
    await _go(db_session, q, enqueue, ip="203.0.113.7")
    keys = {call.args[0].key_prefix for call in open_limiters.await_args_list}
    assert keys == {"http_quote_email_session", "http_quote_email_ip"}
    open_limiters.reset_mock()
    q2 = _quote(db_session, _tenant(db_session))
    await _go(db_session, q2, enqueue, ip=None)
    assert {c.args[0].key_prefix for c in open_limiters.await_args_list} == {"http_quote_email_session"}


async def test_identifiers_sent_to_the_limiter_hold_no_address(db_session, enqueue, open_limiters):
    q = _quote(db_session, _tenant(db_session), email="alice@example.com")
    await _go(db_session, q, enqueue)
    for call in open_limiters.await_args_list:
        assert "alice" not in call.args[1]


async def test_a_failed_enqueue_marks_the_log_failed_and_makes_no_promise(db_session):
    q = _quote(db_session, _tenant(db_session))
    broken = AsyncMock(side_effect=ConnectionError("redis down"))
    assert await svc.email_quote_to_visitor(db_session, q, client_ip=None, enqueue=broken) is None
    log = db_session.query(NotificationLog).one()
    assert log.status == NotifStatus.FAILED


def test_gmail_dots_and_googlemail_do_not_dodge_the_cap():
    base = svc.recipient_key("victim.name@gmail.com")
    assert svc.recipient_key("v.i.c.t.i.m.n.a.m.e@gmail.com") == base
    assert svc.recipient_key("victimname+x@googlemail.com") == base
    # other providers keep their dots: they are different mailboxes there
    assert svc.recipient_key("a.b@example.com") != svc.recipient_key("ab@example.com")


async def test_the_cap_check_and_insert_run_under_an_advisory_lock(db_session, enqueue, monkeypatch):
    """Two concurrent requests for one address must not both pass the count (check-then-insert)."""
    taken = []
    monkeypatch.setattr(svc, "_lock", lambda db, name: taken.append(name))
    t = _tenant(db_session)
    await _go(db_session, _quote(db_session, t, email="Alice+1@example.com"), enqueue)
    assert f"quote-email-tenant:{t.id}" in taken
    assert f"quote-email-address:{svc.recipient_key('alice@example.com')}" in taken


def test_recipient_key_is_a_hash_that_ignores_case_and_plus_tags():
    assert svc.recipient_key("A.B+x@Example.com") == svc.recipient_key("a.b@example.com")
    assert "example" not in svc.recipient_key("a.b@example.com")
