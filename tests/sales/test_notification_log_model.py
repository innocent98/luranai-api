from uuid import uuid4
import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_persists_lowercase(db_session):
    t = _tenant(db_session)
    n = NotificationLog(tenant_id=t.id, event=NotifEvent.MEETING_BOOKED, channel=NotifChannel.EMAIL,
                        entity_type="meeting", entity_id=uuid4(), recipient="rep@co.com",
                        status=NotifStatus.PENDING)
    db_session.add(n); db_session.flush(); db_session.refresh(n)
    assert n.event == NotifEvent.MEETING_BOOKED
    assert n.channel == NotifChannel.EMAIL


def test_unique_dedupe(db_session):
    t = _tenant(db_session)
    eid = uuid4()
    kw = dict(tenant_id=t.id, event=NotifEvent.QUOTE_GENERATED, channel=NotifChannel.EMAIL,
              entity_type="quote", entity_id=eid, recipient="rep@co.com", status=NotifStatus.PENDING)
    db_session.add(NotificationLog(**kw)); db_session.flush()
    db_session.add(NotificationLog(**kw))
    with pytest.raises(IntegrityError):
        db_session.flush()
