from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, email=False, in_app=True):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": email, "slack": False, "in_app": in_app}
    cfg.notification_email = "rep@co.com"
    db.add(cfg); db.commit()
    return t


def _quote(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 62050}], total=62050, status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _inapp_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.IN_APP).all()


def test_dispatch_writes_inapp_row(db_session):
    t = _cfg(db_session, in_app=True); q = _quote(db_session, t)
    NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    rows = _inapp_rows(db_session, t)
    assert len(rows) == 1
    r = rows[0]
    assert r.status == NotifStatus.SENT and r.read_at is None and r.recipient == "in_app"
    assert r.title and r.body  # non-empty rendered text


def test_dispatch_inapp_idempotent(db_session):
    t = _cfg(db_session, in_app=True); q = _quote(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert len(_inapp_rows(db_session, t)) == 1


def test_dispatch_inapp_off(db_session):
    t = _cfg(db_session, in_app=False); q = _quote(db_session, t)
    NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert _inapp_rows(db_session, t) == []
