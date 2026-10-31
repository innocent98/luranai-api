"""The ARQ job that emails the quote to the visitor: content, sender, idempotency, failure."""

from unittest.mock import patch
from uuid import uuid4

import pytest

from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.services.email_service import EmailService
from app.workers.sales_notifications import send_visitor_quote_email

LINES = [
    {"label": "Growth", "detail": "11–50 users", "amount": 45000},
    {"label": "Extra seats (×8)", "detail": "₦3,500 / seat", "amount": 28000},
    {"label": "Annual discount", "detail": "−15%", "amount": -10950},
]


class _NoCloseSession:
    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed(db, *, display_name="Acme Ltd", lead_name="Alice", lines=None, status=NotifStatus.PENDING):
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name=display_name,
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notification_email = "rep@example.com"
    db.add(cfg)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), name=lead_name, email="alice@example.com")
    db.add(lead)
    db.flush()
    q = Quote(
        tenant_id=t.id, lead_id=lead.id, currency="NGN", total=62050,
        line_items=lines if lines is not None else LINES, status=QuoteStatus.DRAFT,
    )
    db.add(q)
    db.flush()
    log = NotificationLog(
        tenant_id=t.id, event=NotifEvent.QUOTE_EMAILED_VISITOR, channel=NotifChannel.EMAIL,
        entity_type="quote", entity_id=q.id, recipient="alice@example.com", status=status,
    )
    db.add(log)
    db.commit()
    return log.id


def _run(db, monkeypatch, log_id, *, pdf=b"%PDF-1.4 fake", send_ok=True):
    import app.workers.sales_notifications as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    sent = {}

    def _send(self, **kwargs):
        sent.update(kwargs)
        sent["html"] = self.render_template(kwargs["template_name"], **kwargs["context"])
        return send_ok

    def _render(*a, **k):
        if pdf is None:
            raise RuntimeError("no pdf")
        return pdf

    patches = (
        patch.object(mod, "render_quote_pdf", side_effect=_render),
        patch.object(EmailService, "send_templated_email_with_attachments", _send),
    )
    return sent, patches


async def _deliver(db, monkeypatch, log_id, **kw):
    sent, (p1, p2) = _run(db, monkeypatch, log_id, **kw)
    with p1, p2:
        await send_visitor_quote_email({}, str(log_id))
    return sent


async def test_the_visitor_gets_the_quote_in_the_body_and_as_a_pdf(db_session, monkeypatch):
    log_id = _seed(db_session)
    sent = await _deliver(db_session, monkeypatch, log_id)
    assert sent["to_email"] == "alice@example.com"
    assert sent["subject"] == "Your quote from Acme Ltd"
    assert sent["reply_to"] == "rep@example.com"
    assert sent["from_name"] == "Acme Ltd"
    name, data, mime = sent["attachments"][0]
    assert name.endswith(".pdf") and data.startswith(b"%PDF") and mime == "application/pdf"
    html = sent["html"]
    for expected in ("Acme Ltd", "Alice", "Growth", "Extra seats (×8)", "₦62,050", "−₦10,950"):
        assert expected in html
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_the_email_is_plain_and_carries_nothing_beyond_the_quote(db_session, monkeypatch):
    log_id = _seed(db_session)
    sent = await _deliver(db_session, monkeypatch, log_id)
    html = sent["html"].lower()
    assert "<img" not in html and "href=" not in html and "<script" not in html
    assert "unsubscribe" not in html and "utm_" not in html
    assert "wasn't you" in html or "was not you" in html  # why they got it, and what to do
    assert sent["text_content"] and "Growth" in sent["text_content"]
    assert "₦62,050" in sent["text_content"]


async def test_the_text_part_only_mentions_the_pdf_when_there_is_one(db_session, monkeypatch):
    with_pdf = await _deliver(db_session, monkeypatch, _seed(db_session))
    assert "attached as a PDF" in with_pdf["text_content"]
    without = await _deliver(db_session, monkeypatch, _seed(db_session), pdf=None)
    assert "PDF" not in without["text_content"]


async def test_still_sent_without_the_pdf_when_it_cannot_be_rendered(db_session, monkeypatch):
    log_id = _seed(db_session)
    sent = await _deliver(db_session, monkeypatch, log_id, pdf=None)
    assert sent["attachments"] is None
    assert "₦62,050" in sent["html"]
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_catalog_quote_lines_without_a_detail_render(db_session, monkeypatch):
    lines = [{"label": "Haircut ×2", "amount": 10000}, {"label": "Beard trim", "detail": None, "amount": 2000}]
    log_id = _seed(db_session, lines=lines)
    sent = await _deliver(db_session, monkeypatch, log_id)
    assert "Haircut ×2" in sent["html"] and "Beard trim" in sent["html"]


async def test_names_are_escaped_and_cannot_break_the_subject(db_session, monkeypatch):
    log_id = _seed(db_session, display_name="Acme\r\nBcc: evil@example.com", lead_name="<b>Eve</b>")
    sent = await _deliver(db_session, monkeypatch, log_id)
    assert "\n" not in sent["subject"] and "\r" not in sent["subject"]
    assert "&lt;b&gt;Eve" in sent["html"] and "<b>Eve" not in sent["html"]


async def test_an_already_sent_log_is_not_sent_again(db_session, monkeypatch):
    log_id = _seed(db_session, status=NotifStatus.SENT)
    sent = await _deliver(db_session, monkeypatch, log_id)
    assert sent == {}


async def test_a_failed_send_is_marked_failed_and_raised_for_retry(db_session, monkeypatch):
    log_id = _seed(db_session)
    sent, (p1, p2) = _run(db_session, monkeypatch, log_id, send_ok=False)
    with p1, p2, pytest.raises(RuntimeError):
        await send_visitor_quote_email({}, str(log_id))
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED
    # the retry then succeeds and flips it to sent, still one log row
    sent2, (q1, q2) = _run(db_session, monkeypatch, log_id)
    with q1, q2:
        await send_visitor_quote_email({}, str(log_id))
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.query(NotificationLog).filter_by(entity_type="quote").count() == 1


async def test_a_missing_log_or_quote_is_a_quiet_noop(db_session, monkeypatch):
    sent, (p1, p2) = _run(db_session, monkeypatch, uuid4())
    with p1, p2:
        await send_visitor_quote_email({}, str(uuid4()))
    assert sent == {}


def test_the_job_is_registered_with_the_worker():
    from app.workers import document_processor

    names = {getattr(f, "__name__", None) or getattr(f, "name", None) for f in document_processor.WorkerSettings.functions}
    assert "send_visitor_quote_email" in names
