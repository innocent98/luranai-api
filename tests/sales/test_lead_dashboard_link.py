"""Rep alerts (email + Slack) deep-link to the lead in the dashboard, never to '#'."""

import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.sales.links import lead_dashboard_url
from app.services.email_service import EmailService
from app.workers.sales_notifications import send_rep_alert, send_slack_notification


class _NoCloseSession:
    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def test_link_is_built_from_the_base_url_and_lead_id(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com/")
    lead_id = uuid4()
    assert lead_dashboard_url(lead_id) == f"https://app.example.com/dashboard/grow/leads/{lead_id}"


@pytest.mark.parametrize("base", ["", "   ", None])
def test_missing_base_url_falls_back_to_no_link(monkeypatch, base):
    monkeypatch.setattr(settings, "FRONTEND_URL", base)
    assert lead_dashboard_url(uuid4()) is None


def test_missing_lead_id_falls_back_to_the_leads_list(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    assert lead_dashboard_url(None) == "https://app.example.com/dashboard/grow/leads"


def test_template_renders_the_real_link_and_hides_the_button_without_one():
    svc = EmailService()
    html = svc.render_template(
        "sales_rep_alert.html", kind="lead", name="Ada", company="Acme", score=100,
        dashboard_url="https://app.example.com/dashboard/grow/leads/abc",
    )
    assert 'href="https://app.example.com/dashboard/grow/leads/abc"' in html
    bare = svc.render_template("sales_rep_alert.html", kind="lead", name="Ada", company="Acme",
                               score=100, dashboard_url=None)
    assert 'href="#"' not in bare and "View in Dashboard" not in bare


def _seed(db, *, event, entity, channel=NotifChannel.EMAIL):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t)
    db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    db.add(cfg)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead)
    db.flush()
    if entity == "lead":
        entity_id = lead.id
    elif entity == "quote":
        q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN", total=1,
                  line_items=[{"label": "A", "detail": "", "amount": 1}], status=QuoteStatus.DRAFT)
        db.add(q)
        db.flush()
        entity_id = q.id
    else:
        from datetime import UTC, datetime, timedelta

        m = Meeting(tenant_id=t.id, lead_id=lead.id, conversation_id=lead.conversation_id,
                    scheduled_at=datetime.now(UTC) + timedelta(days=1), duration_min=30)
        db.add(m)
        db.flush()
        entity_id = m.id
    log = NotificationLog(tenant_id=t.id, event=event, channel=channel, entity_type=entity,
                          entity_id=entity_id, recipient="rep@co.com", status=NotifStatus.PENDING,
                          title="t", body="b")
    db.add(log)
    db.commit()
    return log.id, lead.id


@pytest.mark.parametrize(
    "event,entity",
    [(NotifEvent.SALES_READY_LEAD, "lead"), (NotifEvent.MEETING_BOOKED, "meeting"),
     (NotifEvent.QUOTE_GENERATED, "quote")],
)
async def test_rep_alert_context_carries_the_lead_deep_link(db_session, monkeypatch, event, entity):
    log_id, lead_id = _seed(db_session, event=event, entity=entity)
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    import app.workers.sales_notifications as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    with patch.object(mod, "render_quote_pdf", side_effect=RuntimeError("skip pdf")):
        with patch("app.services.email_service.EmailService.send_templated_email",
                   return_value=True) as send:
            await send_rep_alert({}, str(log_id))
    context = send.call_args.kwargs["context"]
    assert context["dashboard_url"] == f"https://app.example.com/dashboard/grow/leads/{lead_id}"


async def test_slack_link_is_the_same_deep_link(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session, event=NotifEvent.SALES_READY_LEAD, entity="lead",
                            channel=NotifChannel.SLACK)
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    import app.workers.sales_notifications as mod

    monkeypatch.setattr(mod, "SessionLocal", lambda: _NoCloseSession(db_session))
    with patch("httpx.AsyncClient.send", return_value=httpx.Response(200)) as post:
        await send_slack_notification({}, str(log_id))
    payload = json.loads(post.call_args[0][0].content)
    text = [b for b in payload["blocks"] if b["type"] == "context"][0]["elements"][0]["text"]
    assert f"https://app.example.com/dashboard/grow/leads/{lead_id}" in text
