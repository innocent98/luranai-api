from uuid import uuid4
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

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


def test_partial_unique_index_blocks_duplicate_scheduled_slot(db_session):
    # Directly inserts two SCHEDULED meetings at the same (tenant_id, scheduled_at)
    # via the ORM, bypassing book()'s application-level pre-check SELECT.
    # This exercises the partial unique index `uq_meeting_tenant_slot_scheduled`
    # (WHERE status = 'scheduled') — the DB backstop that book()'s IntegrityError
    # catch relies on under real concurrency.
    t = _tenant(db_session); lead1 = _lead(db_session, t); lead2 = _lead(db_session, t)
    when = _future_slot()

    db_session.add(Meeting(
        tenant_id=t.id, lead_id=lead1.id, conversation_id=lead1.conversation_id,
        scheduled_at=when, duration_min=30, status=MeetingStatus.SCHEDULED,
    ))
    db_session.flush()  # first row lands — index entry created

    db_session.add(Meeting(
        tenant_id=t.id, lead_id=lead2.id, conversation_id=lead2.conversation_id,
        scheduled_at=when, duration_min=30, status=MeetingStatus.SCHEDULED,
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()  # second SCHEDULED row at same slot must violate the index

    db_session.rollback()  # restore savepoint so subsequent tests are unaffected

    # Optional: a CANCELLED meeting at the same slot must NOT conflict with a
    # SCHEDULED one (index predicate is `status = 'scheduled'`).
    t2 = _tenant(db_session); lead3 = _lead(db_session, t2); lead4 = _lead(db_session, t2)
    when2 = _future_slot()
    db_session.add(Meeting(
        tenant_id=t2.id, lead_id=lead3.id, conversation_id=lead3.conversation_id,
        scheduled_at=when2, duration_min=30, status=MeetingStatus.CANCELLED,
    ))
    db_session.flush()
    db_session.add(Meeting(
        tenant_id=t2.id, lead_id=lead4.id, conversation_id=lead4.conversation_id,
        scheduled_at=when2, duration_min=30, status=MeetingStatus.SCHEDULED,
    ))
    db_session.flush()  # CANCELLED + SCHEDULED at same slot is allowed — no error


def test_naive_datetime_raises_slot_unavailable(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    naive = datetime(2099, 1, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(SlotUnavailable, match="timezone-aware"):
        svc.book(t.id, lead, scheduled_at=naive, visitor_timezone="UTC", use_case=None, duration_min=30)
