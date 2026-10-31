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
        self, prompt, config, *, model_name=None, known_facts=None, company_website=None,
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
    async def _fake(self, prompt, max_tokens=800, model_name=None, system_message=None,
                    image_urls=None):
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
    return db.execute(
        select(NotificationLog).where(
            NotificationLog.tenant_id == tenant_id,
            NotificationLog.event == NotifEvent.QUOTE_EMAILED_VISITOR,
        )
    ).scalars().all()


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
        client, tenant_id, _llm("Anything else?", signals={**_OFFER_QUOTE_SIGNALS, "offer_quote": False}),
        session_id=first["session_id"], message="thanks",
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
        client, tenant_id, _llm("Thanks Alice!", signals=quiet),
        session_id=first["session_id"], message="yes please, alice@example.com",
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
def test_a_visitor_without_an_email_is_not_promised_one_by_the_model(client, db, monkeypatch, promise):
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
