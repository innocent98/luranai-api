from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel
from app.services.notification_service import NotificationService


def _tenant_cfg(db, *, email_on=True, notif_email="rep@co.com"):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": email_on, "slack": False, "in_app": False}
    cfg.notification_email = notif_email
    db.add(cfg); db.commit()
    return t


def test_dispatch_writes_log_and_returns_job(db_session):
    t = _tenant_cfg(db_session)
    svc = NotificationService(db_session)
    qid = uuid4()
    jobs = svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=qid)
    assert len(jobs) == 1 and jobs[0][1] == "send_rep_alert"
    rows = db_session.query(NotificationLog).filter_by(tenant_id=t.id, entity_id=qid).all()
    assert len(rows) == 1 and rows[0].channel == NotifChannel.EMAIL


def test_dispatch_idempotent(db_session):
    t = _tenant_cfg(db_session)
    svc = NotificationService(db_session)
    qid = uuid4()
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=qid)
    jobs2 = svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=qid)
    assert jobs2 == []  # already dispatched → no new job
    assert db_session.query(NotificationLog).filter_by(entity_id=qid).count() == 1


def test_dispatch_email_off(db_session):
    t = _tenant_cfg(db_session, email_on=False)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=uuid4())
    assert jobs == []


def test_dispatch_no_recipient(db_session):
    t = _tenant_cfg(db_session, notif_email=None)  # and no admin user
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=uuid4())
    assert jobs == []
