"""Tests for CRM channel dispatch (Stage 3 Slice 5e Task 2)."""
from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, crm=True, url="https://hooks.zapier.com/hooks/catch/1/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": False, "slack": False, "in_app": False, "crm": crm}
    cfg.crm_webhook_url = url
    db.add(cfg); db.commit()
    return t


def _lead(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), name="Dana", email="d@co.com",
                company="Acme", lead_score=90)
    db.add(lead); db.flush(); db.refresh(lead)
    return lead


def _quote(db, t, lead):
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000,
              status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _crm_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.CRM).all()


def test_dispatch_crm_writes_row_and_returns_job_on_sales_ready(db_session):
    t = _cfg(db_session); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" in [j[1] for j in jobs]
    rows = _crm_rows(db_session, t)
    assert len(rows) == 1
    assert rows[0].status == NotifStatus.PENDING
    assert rows[0].recipient == "crm"
    assert rows[0].title and rows[0].body


def test_dispatch_crm_off(db_session):
    t = _cfg(db_session, crm=False); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_no_url(db_session):
    t = _cfg(db_session, url=None); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_not_sales_ready_event(db_session):
    """quote_generated must NOT produce a crm row/job even with crm enabled."""
    t = _cfg(db_session); lead = _lead(db_session, t); q = _quote(db_session, t, lead)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_idempotent(db_session):
    t = _cfg(db_session); lead = _lead(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    svc.dispatch(t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert len(_crm_rows(db_session, t)) == 1
