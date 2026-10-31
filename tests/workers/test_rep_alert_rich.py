"""Rep alert carries who the visitor is and how to reach them (quote, lead and meeting alerts)."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.db.models.visitor import Visitor, VisitorSource
from app.services.email_service import EmailService
from app.workers.sales_notifications import send_rep_alert, send_slack_notification

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


def _seed(
    db,
    *,
    event,
    entity,
    channel=NotifChannel.EMAIL,
    name="Ada Obi",
    company="Northwind",
    email="ada@example.com",
    phone="+2348012345678",
    source="whatsapp",
):
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Acme",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    db.add(cfg)
    visitor = Visitor(
        tenant_id=t.id,
        public_token=uuid4().hex,
        phone=phone,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        created_from=VisitorSource.WIDGET,
    )
    db.add(visitor)
    db.flush()
    session = ConversationSession(
        tenant_id=t.id,
        user_identifier=f"v-{uuid4().hex[:6]}",
        status=SessionStatus.ACTIVE,
        visitor_id=visitor.id,
        source=source,
    )
    db.add(session)
    db.flush()
    lead = Lead(
        tenant_id=t.id,
        conversation_id=session.id,
        name=name,
        email=email,
        company=company,
    )
    db.add(lead)
    db.flush()
    q = Quote(
        tenant_id=t.id,
        lead_id=lead.id,
        currency="NGN",
        total=62050,
        line_items=LINES,
        status=QuoteStatus.DRAFT,
    )
    db.add(q)
    db.flush()
    lead.quote_id = q.id
    if entity == "lead":
        entity_id = lead.id
    elif entity == "quote":
        entity_id = q.id
    else:
        m = Meeting(
            tenant_id=t.id,
            lead_id=lead.id,
            conversation_id=lead.conversation_id,
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
            duration_min=30,
        )
        db.add(m)
        db.flush()
        entity_id = m.id
    log = NotificationLog(
        tenant_id=t.id,
        event=event,
        channel=channel,
        entity_type=entity,
        entity_id=entity_id,
        recipient="rep@example.com",
        status=NotifStatus.PENDING,
        title="t",
        body="Ada Obi · Northwind",
    )
    db.add(log)
    db.commit()
    return log.id


CASES = [
    (NotifEvent.QUOTE_GENERATED, "quote"),
    (NotifEvent.SALES_READY_LEAD, "lead"),
    (NotifEvent.MEETING_BOOKED, "meeting"),
]


async def _rendered_alert(db, monkeypatch, log_id, *, pdf_fails=False):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    import app.workers.sales_notifications as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db))
    sent = {}

    def _capture(self, *, to_email, subject, template_name, context, attachments=None):
        sent["html"] = self.render_template(template_name, **context)
        sent["context"] = context
        sent["attachments"] = attachments
        return True

    def _pdf(*a, **k):
        if pdf_fails:
            raise RuntimeError("no pdf")
        return b"%PDF-1.4 fake"

    with patch.object(mod, "render_quote_pdf", side_effect=_pdf):
        with patch.object(EmailService, "send_templated_email_with_attachments", _capture):
            with patch.object(
                EmailService,
                "send_templated_email",
                lambda self, *, to_email, subject, template_name, context: _capture(
                    self,
                    to_email=to_email,
                    subject=subject,
                    template_name=template_name,
                    context=context,
                ),
            ):
                await send_rep_alert({}, str(log_id))
    return sent


@pytest.mark.parametrize("event,entity", CASES)
async def test_alert_names_the_visitor_and_how_to_reach_them(db_session, monkeypatch, event, entity):
    log_id = _seed(db_session, event=event, entity=entity)
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    html = sent["html"]
    for expected in ("Ada Obi", "Northwind", "ada@example.com", "+2348012345678", "WhatsApp"):
        assert expected in html, expected
    assert f"https://app.example.com/dashboard/grow/leads/" in html  # deep link kept


async def test_quote_alert_shows_the_total_with_the_pdf_formatting_and_the_wanted_items(
    db_session, monkeypatch
):
    log_id = _seed(db_session, event=NotifEvent.QUOTE_GENERATED, entity="quote")
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    assert "₦62,050" in sent["html"]
    for label in ("Growth", "Extra seats (×8)", "Annual discount"):
        assert label in sent["html"]
    assert "−₦10,950" in sent["html"]  # a discount line keeps its sign, like the PDF
    assert sent["context"]["total"] == 62050  # existing keys stay
    assert sent["context"]["currency"] == "NGN"


async def test_lead_alert_lists_the_items_of_the_quote_the_lead_asked_for(db_session, monkeypatch):
    log_id = _seed(db_session, event=NotifEvent.SALES_READY_LEAD, entity="lead")
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    assert "Growth" in sent["html"]


async def test_missing_contact_details_are_left_out_not_printed_as_none(db_session, monkeypatch):
    log_id = _seed(
        db_session,
        event=NotifEvent.QUOTE_GENERATED,
        entity="quote",
        email=None,
        phone=None,
        source=None,
    )
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    assert "None" not in sent["html"]
    assert "Phone" not in sent["html"] and "Email" not in sent["html"]


@pytest.mark.parametrize("event,entity", CASES)
async def test_visitor_text_is_html_escaped(db_session, monkeypatch, event, entity):
    log_id = _seed(
        db_session,
        event=event,
        entity=entity,
        name="<script>alert(1)</script>",
        company='"><img src=x onerror=alert(2)>',
    )
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    assert "<script>alert(1)</script>" not in sent["html"]
    assert "<img src=x" not in sent["html"]
    assert "&lt;script&gt;" in sent["html"]


async def test_the_alert_is_still_sent_when_the_pdf_cannot_be_rendered(db_session, monkeypatch):
    log_id = _seed(db_session, event=NotifEvent.QUOTE_GENERATED, entity="quote")
    sent = await _rendered_alert(db_session, monkeypatch, log_id, pdf_fails=True)
    assert sent["attachments"] is None
    assert "ada@example.com" in sent["html"]
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_slack_message_is_rich_and_escaped(db_session, monkeypatch):
    log_id = _seed(
        db_session,
        event=NotifEvent.QUOTE_GENERATED,
        entity="quote",
        channel=NotifChannel.SLACK,
        name="Ada <!channel> & co",
    )
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    import app.workers.sales_notifications as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    with patch("httpx.AsyncClient.send", return_value=httpx.Response(200)) as post:
        await send_slack_notification({}, str(log_id))
    payload = json.loads(post.call_args[0][0].content)
    section = [b for b in payload["blocks"] if b["type"] == "section"][0]["text"]["text"]
    assert "ada@example.com" in section and "₦62,050" in section and "WhatsApp" in section
    assert "<!channel>" not in section  # no mention injection
    assert "&lt;!channel&gt; &amp; co" in section
    assert "https://app.example.com/dashboard/grow/leads/" in json.dumps(payload)  # link kept


async def test_an_unknown_conversation_source_is_not_shown_as_a_channel(db_session, monkeypatch):
    log_id = _seed(db_session, event=NotifEvent.QUOTE_GENERATED, entity="quote", source="unknown")
    sent = await _rendered_alert(db_session, monkeypatch, log_id)
    assert "Channel" not in sent["html"]
