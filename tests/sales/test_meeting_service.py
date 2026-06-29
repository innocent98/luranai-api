from uuid import uuid4
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.lead import Lead
from app.db.models.meeting import Meeting, MeetingStatus
from app.db.models.tenant import Tenant
from app.services.meeting_service import MeetingService, SlotUnavailable

AVAIL = {"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "00:00", "end": "23:30",
         "slot_minutes": 30, "buffer_minutes": 0, "tenant_timezone": "UTC"}


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def _lead(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="a@b.com")
    db.add(lead); db.flush(); db.refresh(lead)
    return lead


def _future_slot():
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return base + timedelta(days=1)


def test_book_creates_meeting_and_links_lead(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    when = _future_slot()
    svc = MeetingService(db_session)
    m = svc.book(t.id, lead, scheduled_at=when, visitor_timezone="Africa/Lagos",
                 use_case="Demo", duration_min=30)
    assert m.status == MeetingStatus.SCHEDULED
    assert lead.meeting_id == m.id


def test_double_book_same_slot_raises(db_session):
    t = _tenant(db_session); lead1 = _lead(db_session, t); lead2 = _lead(db_session, t)
    when = _future_slot()
    svc = MeetingService(db_session)
    svc.book(t.id, lead1, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30)
    with pytest.raises(SlotUnavailable):
        svc.book(t.id, lead2, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30)


def test_rebook_returns_existing_future_meeting(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    when = _future_slot()
    svc = MeetingService(db_session)
    m1 = svc.book(t.id, lead, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30)
    m2 = svc.book(t.id, lead, scheduled_at=when + timedelta(hours=2),
                  visitor_timezone="UTC", use_case=None, duration_min=30)
    assert m1.id == m2.id  # idempotent — lead already has a future meeting


def test_past_slot_rejected(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    with pytest.raises(SlotUnavailable):
        svc.book(t.id, lead, scheduled_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                 visitor_timezone="UTC", use_case=None, duration_min=30)


def test_compute_availability_excludes_booked(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    when = _future_slot()
    svc.book(t.id, lead, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30)
    now = datetime.now(timezone.utc)
    slots = svc.compute_availability(t.id, AVAIL, now=now, days=3)
    assert when not in slots
