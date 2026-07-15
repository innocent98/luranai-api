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


# ---------------------------------------------------------------------------
# MEETING_BOOKED path — rep + visitor dual-job dispatch
# ---------------------------------------------------------------------------

def test_dispatch_meeting_booked_rep_and_visitor(db_session):
    from datetime import datetime, timezone
    from app.db.models.lead import Lead
    from app.db.models.meeting import Meeting, MeetingStatus

    t = _tenant_cfg(db_session)
    lead = Lead(tenant_id=t.id, email="visitor@example.com")
    db_session.add(lead)
    db_session.flush()
    db_session.refresh(lead)

    meeting = Meeting(
        tenant_id=t.id, lead_id=lead.id,
        scheduled_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        duration_min=30, status=MeetingStatus.SCHEDULED,
    )
    db_session.add(meeting)
    db_session.flush()
    db_session.refresh(meeting)

    svc = NotificationService(db_session)
    jobs = svc.dispatch(t.id, NotifEvent.MEETING_BOOKED, entity_type="meeting", entity_id=meeting.id)

    assert len(jobs) == 2
    job_names = [j[1] for j in jobs]
    assert "send_rep_alert" in job_names
    assert "send_visitor_meeting_confirmation" in job_names

    rows = db_session.query(NotificationLog).filter_by(tenant_id=t.id, entity_id=meeting.id).all()
    assert len(rows) == 2
    recipients = {r.recipient for r in rows}
    assert "rep@co.com" in recipients
    assert "visitor@example.com" in recipients


def test_dispatch_meeting_visitor_skipped_when_lead_has_no_email(db_session):
    from datetime import datetime, timezone
    from app.db.models.lead import Lead
    from app.db.models.meeting import Meeting, MeetingStatus

    t = _tenant_cfg(db_session)
    lead = Lead(tenant_id=t.id, email=None)
    db_session.add(lead)
    db_session.flush()
    db_session.refresh(lead)

    meeting = Meeting(
        tenant_id=t.id, lead_id=lead.id,
        scheduled_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        duration_min=30, status=MeetingStatus.SCHEDULED,
    )
    db_session.add(meeting)
    db_session.flush()
    db_session.refresh(meeting)

    svc = NotificationService(db_session)
    jobs = svc.dispatch(t.id, NotifEvent.MEETING_BOOKED, entity_type="meeting", entity_id=meeting.id)

    assert len(jobs) == 1
    assert jobs[0][1] == "send_rep_alert"

    rows = db_session.query(NotificationLog).filter_by(tenant_id=t.id, entity_id=meeting.id).all()
    assert len(rows) == 1
    assert rows[0].recipient == "rep@co.com"
