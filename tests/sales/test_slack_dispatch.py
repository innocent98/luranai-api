"""Tests for Slack channel dispatch (Stage 3 Slice 5d Task 2)."""
from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, slack=True, url="https://hooks.slack.com/services/T/B/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": False, "slack": slack, "in_app": False}
    cfg.slack_webhook_url = url
    db.add(cfg); db.commit()
    return t


def _quote(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000, status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _slack_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.SLACK).all()


def test_dispatch_slack_writes_row_and_returns_job(db_session):
    t = _cfg(db_session); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert ("send_slack_notification" in [j[1] for j in jobs])
    rows = _slack_rows(db_session, t)
    assert len(rows) == 1 and rows[0].status == NotifStatus.PENDING and rows[0].recipient == "slack"
    assert rows[0].title and rows[0].body


def test_dispatch_slack_off(db_session):
    t = _cfg(db_session, slack=False); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "send_slack_notification" not in [j[1] for j in jobs]
    assert _slack_rows(db_session, t) == []


def test_dispatch_slack_no_url(db_session):
    t = _cfg(db_session, url=None); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "send_slack_notification" not in [j[1] for j in jobs]
    assert _slack_rows(db_session, t) == []


def test_dispatch_slack_idempotent(db_session):
    t = _cfg(db_session); q = _quote(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert len(_slack_rows(db_session, t)) == 1
