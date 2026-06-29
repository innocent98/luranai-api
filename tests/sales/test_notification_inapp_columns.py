from uuid import uuid4
from datetime import datetime, timezone

from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name="T", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_inapp_fields_persist(db_session):
    t = _tenant(db_session)
    n = NotificationLog(tenant_id=t.id, event=NotifEvent.SALES_READY_LEAD, channel=NotifChannel.IN_APP,
                        entity_type="lead", entity_id=uuid4(), recipient="in_app",
                        status=NotifStatus.SENT, title="New sales-ready lead", body="John · Acme · 90")
    db_session.add(n); db_session.flush(); db_session.refresh(n)
    assert n.title == "New sales-ready lead" and n.body == "John · Acme · 90"
    assert n.read_at is None
    n.read_at = datetime.now(timezone.utc)
    db_session.flush()
    assert n.read_at is not None
