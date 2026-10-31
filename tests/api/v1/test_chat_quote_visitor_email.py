"""Chat flow: the quote is emailed to the visitor and the reply says so truthfully.

The model never promises an email: code removes any such sentence from every reply and adds the
one sentence that is true (a masked address when a send was queued, a plain "couldn't" when a
cap stopped it, nothing when nothing can be sent).
"""

from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.notification_log import NotificationLog, NotifEvent
from app.db.models.quote import Quote
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from tests.api.v1.test_chat_lead_capture import (
    _automation_patch,
    _make_tenant,
    _retrieval_patch,
)
from tests.api.v1.test_chat_quote import (
    _CAPTURED_WITH_TEAM_SIZE,
    _OFFER_QUOTE_SIGNALS,
    _make_quote_tenant,
)

PROMISE = "Here's your quote. Should I email it to you?"


async def _noop(jobs):
    jobs_seen.extend(jobs)


jobs_seen: list = []


def _llm(reply: str, captured: dict | None = None, signals: dict | None = None):
    async def _fake(
        self,
        prompt,
        config,
        *,
        model_name=None,
        known_facts=None,
        company_website=None,
        image_urls=None,
    ):
        meta = {
            "intent": "PRICING_INQUIRY",
            "captured": captured if captured is not None else _CAPTURED_WITH_TEAM_SIZE,
            "signals": signals if signals is not None else _OFFER_QUOTE_SIGNALS,
        }
        return reply, meta, 50, False

    return patch("app.services.rag_service.RAGService.generate_sales_response", new=_fake)


def _plain_llm(reply: str):
    async def _fake(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        return reply, 30, False

    return patch("app.services.rag_service.RAGService.generate_ai_response", new=_fake)


def _tenant(db: Session, *, notification_email="rep@example.com") -> UUID:
    tenant_id = _make_quote_tenant(db, quotation_enabled=True)
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one()
    cfg.notification_email = notification_email
    cfg.notify_channels = {"email": True}
    db.commit()
    return tenant_id


def _send(client, tenant_id, llm, *, user="qe1", session_id=None, message="How much for 58?"):
    body = {"message": message, "tenant_id": str(tenant_id), "user_identifier": user}
    if session_id:
        body["session_id"] = session_id
    with (
        llm,
        _retrieval_patch(),
        _automation_patch(),
        patch("app.services.notification_enqueue.enqueue_jobs_async", _noop),
        patch("app.services.quote_email_service.enforce_identifier_limit", _allow),
    ):
        r = client.post("/api/v1/chat/message", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _allow(limiter, identifier):
    return None


def _visitor_logs(db, tenant_id):
    return (
        db.execute(
            select(NotificationLog).where(
                NotificationLog.tenant_id == tenant_id,
                NotificationLog.event == NotifEvent.QUOTE_EMAILED_VISITOR,
            )
        )
        .scalars()
        .all()
    )


def test_quote_for_a_visitor_with_an_email_is_emailed_and_the_reply_says_so(client, db: Session):
    jobs_seen.clear()
    tenant_id = _tenant(db)
    out = _send(client, tenant_id, _llm(PROMISE))
    content = out["message"]["content"]
    assert "I've sent the quote to a***@example.com" in content
    assert "alice@example.com" not in content  # the full address is never echoed
    assert "email it to you" not in content  # the model's own offer is gone
    (log,) = _visitor_logs(db, tenant_id)
    assert log.recipient == "alice@example.com"
    assert [name for _id, name in jobs_seen if name == "send_visitor_quote_email"]
    assert db.execute(select(Quote).where(Quote.tenant_id == tenant_id)).scalars().one()
    assert "quote" in out["message"]["meta"]  # the quote card is unchanged


def test_a_second_turn_does_not_email_or_announce_again(client, db: Session):
    tenant_id = _tenant(db)
    first = _send(client, tenant_id, _llm("Here is your quote."))
    second = _send(
        client,
        tenant_id,
        _llm("Anything else?", signals={**_OFFER_QUOTE_SIGNALS, "offer_quote": False}),
        session_id=first["session_id"],
        message="thanks",
    )
    assert "I've sent the quote" not in second["message"]["content"]
    assert len(_visitor_logs(db, tenant_id)) == 1


def test_email_given_after_the_quote_triggers_the_send(client, db: Session):
    tenant_id = _tenant(db)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    first = _send(client, tenant_id, _llm("Here is your quote.", captured=no_email))
    assert "share your email address" in first["message"]["content"]  # truthful invitation
    assert _visitor_logs(db, tenant_id) == []
    quiet = {**_OFFER_QUOTE_SIGNALS, "offer_quote": False}
    second = _send(
        client,
        tenant_id,
        _llm("Thanks Alice!", signals=quiet),
        session_id=first["session_id"],
        message="yes please, alice@example.com",
    )
    assert "I've sent the quote to a***@example.com" in second["message"]["content"]
    assert len(_visitor_logs(db, tenant_id)) == 1


# --- guard: no reply path promises an email when none will be sent ---------------------------

PROMISES = [
    "Should I email it to you?",
    "Would you like me to email the quote to you?",
    "I'll email you the quote shortly.",
    "I can send the full proposal to your inbox.",
]


def _switch_off(db, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_ENABLED", False)


def _no_reply_to(db, tenant_id, monkeypatch):
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
    ).scalar_one()
    cfg.notification_email = None
    db.commit()


@pytest.mark.parametrize("promise", PROMISES)
@pytest.mark.parametrize("disable", [_switch_off, _no_reply_to], ids=["switch-off", "no-reply-to"])
def test_nothing_is_promised_when_nothing_can_be_sent(client, db, monkeypatch, disable, promise):
    tenant_id = _tenant(db)
    disable(db, tenant_id, monkeypatch)
    out = _send(client, tenant_id, _llm(f"Here is your quote. {promise}"))
    content = out["message"]["content"].lower()
    assert "email" not in content and "inbox" not in content, content
    assert _visitor_logs(db, tenant_id) == []


@pytest.mark.parametrize("promise", PROMISES)
def test_a_visitor_without_an_email_is_not_promised_one_by_the_model(
    client, db, monkeypatch, promise
):
    tenant_id = _tenant(db)
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_ENABLED", False)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    out = _send(client, tenant_id, _llm(f"Here is your quote. {promise}", captured=no_email))
    assert "email" not in out["message"]["content"].lower()


def test_a_cap_is_reported_honestly_and_nothing_is_promised(client, db, monkeypatch):
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_TENANT_DAILY_CAP", 0)
    tenant_id = _tenant(db)
    out = _send(client, tenant_id, _llm(PROMISE))
    content = out["message"]["content"]
    assert "couldn't email the quote" in content
    assert "I've sent" not in content and "email it to you" not in content
    assert _visitor_logs(db, tenant_id) == []


@pytest.mark.parametrize("promise", PROMISES)
def test_the_plain_support_path_never_promises_an_email_either(client, db, promise):
    tenant_id = _make_tenant(db, sales_enabled=False)
    with (
        _plain_llm(f"Our plans start at 50,000. {promise}"),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={"message": "price?", "tenant_id": str(tenant_id), "user_identifier": "plain1"},
        )
    assert r.status_code == 200, r.text
    content = r.json()["message"]["content"]
    assert content.startswith("Our plans start at 50,000.")
    assert "email" not in content.lower() and "inbox" not in content.lower()


# --- meta.quote says whether the card may still offer an email --------------------------------


def _quote_meta(out):
    return out["message"]["meta"]["quote"]


def test_card_says_emailed_and_to_whom_when_the_send_was_queued(client, db: Session):
    tenant_id = _tenant(db)
    out = _send(client, tenant_id, _llm(PROMISE))
    q = _quote_meta(out)
    assert q["emailed"] is True and q["emailed_to"] == "a***@example.com"
    assert q["email_offer"] is False
    assert q["total"] == 62050 and q["line_items"]  # the card's own data is untouched
    assert "alice@example.com" not in str(out["message"]["meta"]["quote"])


def test_card_keeps_the_offer_when_the_visitor_has_no_address_yet(client, db: Session):
    tenant_id = _tenant(db)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    q = _quote_meta(_send(client, tenant_id, _llm("Here is your quote.", captured=no_email)))
    assert q["emailed"] is False and q["email_offer"] is True and "emailed_to" not in q


@pytest.mark.parametrize("disable", [_switch_off, _no_reply_to], ids=["switch-off", "no-reply-to"])
def test_card_drops_the_offer_when_no_email_could_ever_be_sent(client, db, monkeypatch, disable):
    tenant_id = _tenant(db)
    disable(db, tenant_id, monkeypatch)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    for captured in (None, no_email):
        q = _quote_meta(
            _send(
                client,
                tenant_id,
                _llm("Here is your quote.", captured=captured),
                user=f"u{captured is None}",
            )
        )
        assert q["emailed"] is False and q["email_offer"] is False


def test_card_drops_the_offer_when_a_cap_stopped_the_send(client, db, monkeypatch):
    monkeypatch.setattr(settings, "QUOTE_VISITOR_EMAIL_TENANT_DAILY_CAP", 0)
    q = _quote_meta(_send(client, _tenant(db), _llm(PROMISE)))
    assert q["emailed"] is False and q["email_offer"] is False


def test_an_email_given_later_marks_the_earlier_card_sent(client, db: Session):
    from app.db.models.conversation import Message

    tenant_id = _tenant(db)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    first = _send(client, tenant_id, _llm("Here is your quote.", captured=no_email))
    quiet = {**_OFFER_QUOTE_SIGNALS, "offer_quote": False}
    second = _send(
        client,
        tenant_id,
        _llm("Thanks Alice!", signals=quiet),
        session_id=first["session_id"],
        message="yes please, alice@example.com",
    )
    assert second["message"]["meta"]["quote_emailed_to"] == "a***@example.com"
    db.expire_all()
    stored = db.get(Message, UUID(first["message"]["id"])).meta["quote"]
    assert stored["emailed"] is True and stored["emailed_to"] == "a***@example.com"
    assert stored["email_offer"] is False and stored["total"] == 62050


# --- the second "Yes, email me": code words it, masked only ------------------------------------

IMPROVISED = (
    "Your Starter quote for ₦5,000 was previously marked as sent to alice@example.com. "
    "Please check your inbox and spam folder. I can't send another email from here."
)


def test_asking_again_never_echoes_the_full_address_and_offers_a_next_step(client, db: Session):
    tenant_id = _tenant(db)
    first = _send(client, tenant_id, _llm("Here is your quote."))
    jobs_before = len(jobs_seen)
    quiet = {**_OFFER_QUOTE_SIGNALS, "offer_quote": False}
    second = _send(
        client,
        tenant_id,
        _llm(IMPROVISED, signals=quiet),
        session_id=first["session_id"],
        message="Yes, email me",
    )
    content = second["message"]["content"]
    assert "alice@example.com" not in content and "lice@" not in content.replace("a***@", "")
    assert "a***@example.com" in content
    assert "can't send another email" not in content
    assert "ask the team to resend" in content
    assert len(_visitor_logs(db, tenant_id)) == 1
    assert len(jobs_seen) == jobs_before  # nothing was queued again
    assert "quote" not in second["message"]["meta"]  # no new card on this turn


@pytest.mark.parametrize("message", ["What does Starter include?", "thanks"])
def test_any_reply_that_repeats_the_address_is_masked(client, db: Session, message):
    tenant_id = _tenant(db)
    first = _send(client, tenant_id, _llm("Here is your quote."))
    quiet = {**_OFFER_QUOTE_SIGNALS, "offer_quote": False}
    out = _send(
        client,
        tenant_id,
        _llm("Noted, we have alice@example.com on file.", signals=quiet),
        session_id=first["session_id"],
        message=message,
    )
    assert out["message"]["content"].startswith("Noted, we have a***@example.com on file.")
    assert "alice@example.com" not in out["message"]["content"]


def test_the_plain_support_path_masks_the_visitors_address_too(client, db: Session):
    tenant_id = _make_tenant(db, sales_enabled=False)
    with (
        _plain_llm("We will write to alice@example.com soon."),
        _retrieval_patch(),
        _automation_patch(),
    ):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "message": "my email is alice@example.com, any news?",
                "tenant_id": str(tenant_id),
                "user_identifier": "plain2",
            },
        )
    assert r.status_code == 200, r.text
    assert "alice@example.com" not in r.json()["message"]["content"]


def test_a_newer_quote_card_takes_over_the_offer_from_the_older_one(client, db: Session):
    from app.db.models.conversation import Message

    tenant_id = _tenant(db)
    no_email = {**_CAPTURED_WITH_TEAM_SIZE, "email": None}
    first = _send(client, tenant_id, _llm("Here is your quote.", captured=no_email))
    assert _quote_meta(first)["email_offer"] is True
    second = _send(
        client, tenant_id, _llm("Updated quote."),  # the model offers a fresh quote again
        session_id=first["session_id"], message="yes, alice@example.com",
    )
    assert _quote_meta(second)["emailed"] is True
    db.expire_all()
    older = db.get(Message, UUID(first["message"]["id"])).meta["quote"]
    assert older["email_offer"] is False and older["emailed"] is False  # not "sent": a different card
    assert older["total"] == 62050
