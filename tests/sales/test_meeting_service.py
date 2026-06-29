from uuid import uuid4
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
                 use_case="Demo", duration_min=30, availability=AVAIL)
    assert m.status == MeetingStatus.SCHEDULED
    assert lead.meeting_id == m.id


def test_double_book_same_slot_raises(db_session):
    t = _tenant(db_session); lead1 = _lead(db_session, t); lead2 = _lead(db_session, t)
    when = _future_slot()
    svc = MeetingService(db_session)
    svc.book(t.id, lead1, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)
    with pytest.raises(SlotUnavailable):
        svc.book(t.id, lead2, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)


def test_rebook_returns_existing_future_meeting(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    when = _future_slot()
    svc = MeetingService(db_session)
    m1 = svc.book(t.id, lead, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)
    m2 = svc.book(t.id, lead, scheduled_at=when + timedelta(hours=2),
                  visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)
    assert m1.id == m2.id  # idempotent — lead already has a future meeting


def test_past_slot_rejected(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    with pytest.raises(SlotUnavailable):
        svc.book(t.id, lead, scheduled_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                 visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)


def test_compute_availability_excludes_booked(db_session):
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    when = _future_slot()
    svc.book(t.id, lead, scheduled_at=when, visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)
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
        svc.book(t.id, lead, scheduled_at=naive, visitor_timezone="UTC", use_case=None, duration_min=30, availability=AVAIL)


# ---------------------------------------------------------------------------
# New tests: slot-membership validation
# ---------------------------------------------------------------------------

# Restricted availability used by the new rejection tests: business hours only,
# Mon–Fri, 30-min slots.  Keeps AVAIL (all-week, 00:00–23:30) for green-path tests.
AVAIL_BUSINESS = {
    "weekdays": [1, 2, 3, 4, 5],  # Mon–Fri only
    "start": "09:00",
    "end": "17:00",
    "slot_minutes": 30,
    "buffer_minutes": 0,
    "tenant_timezone": "UTC",
}


def test_book_outside_hours_raises(db_session):
    """Booking at 17:30 UTC (past the 17:00 end boundary) raises SlotUnavailable."""
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    # Build a datetime tomorrow at 17:30 UTC — one step past the last slot (16:30).
    outside = (
        datetime.now(timezone.utc).replace(hour=17, minute=30, second=0, microsecond=0)
        + timedelta(days=1)
    )
    with pytest.raises(SlotUnavailable, match="Not an available slot"):
        svc.book(t.id, lead, scheduled_at=outside, visitor_timezone="UTC",
                 use_case=None, duration_min=30, availability=AVAIL_BUSINESS)


def test_book_misaligned_minute_raises(db_session):
    """Booking at 09:07 (not on a 30-min boundary) raises SlotUnavailable."""
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    misaligned = (
        datetime.now(timezone.utc).replace(hour=9, minute=7, second=0, microsecond=0)
        + timedelta(days=1)
    )
    # Ensure the day is a weekday for AVAIL_BUSINESS; if it lands on a weekend,
    # push forward until Monday — the misalignment is the point, not the weekday.
    while misaligned.isoweekday() > 5:
        misaligned += timedelta(days=1)
    with pytest.raises(SlotUnavailable, match="Not an available slot"):
        svc.book(t.id, lead, scheduled_at=misaligned, visitor_timezone="UTC",
                 use_case=None, duration_min=30, availability=AVAIL_BUSINESS)


def test_book_outside_weekday_raises(db_session):
    """Booking on a Saturday (weekday 6, outside Mon–Fri) raises SlotUnavailable."""
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    # Start from tomorrow so 10:00 UTC is always in the future, then advance to the
    # next Saturday. (Starting from today at 10:00 UTC fails when today IS Saturday
    # and the run is after 10:00 UTC — book() would raise "Slot is in the past".)
    target = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    while target.isoweekday() != 6:  # 6 = Saturday
        target += timedelta(days=1)
    with pytest.raises(SlotUnavailable, match="Not an available slot"):
        svc.book(t.id, lead, scheduled_at=target, visitor_timezone="UTC",
                 use_case=None, duration_min=30, availability=AVAIL_BUSINESS)


def test_book_valid_offered_slot_succeeds(db_session):
    """A slot that is genuinely in the offered set books without error."""
    t = _tenant(db_session); lead = _lead(db_session, t)
    svc = MeetingService(db_session)
    # _future_slot() → next day, top of hour, UTC.  AVAIL covers all-week 00:00–23:30
    # so this is always a valid slot.
    when = _future_slot()
    m = svc.book(t.id, lead, scheduled_at=when, visitor_timezone="UTC",
                 use_case=None, duration_min=30, availability=AVAIL)
    assert m.status == MeetingStatus.SCHEDULED


# ---------------------------------------------------------------------------
# Regression test for Finding 1 — local-date days-window bug (UTC+12/+13)
# ---------------------------------------------------------------------------

# All-week, full-day availability in Auckland (UTC+12 winter / UTC+13 DST).
# Slots in this config can fall on a LOCAL date that is one calendar day ahead
# of their UTC date — the exact scenario that exposed the UTC-based days bug.
AVAIL_AUCKLAND = {
    "weekdays": [1, 2, 3, 4, 5, 6, 7],
    "start": "00:00",
    "end": "23:30",
    "slot_minutes": 30,
    "buffer_minutes": 0,
    "tenant_timezone": "Pacific/Auckland",
}


def test_positive_tz_next_local_day_slot_accepted(db_session):
    """Regression (Finding 1): a slot whose LOCAL date (Pacific/Auckland) is ahead of
    its UTC date must be accepted by book() — not falsely rejected with 'Not an
    available slot'.

    Strategy: derive a valid slot via compute_availability (which correctly uses local
    calendar dates), then assert book() accepts it.  We prefer a slot on a LOCAL day
    strictly after today_local because those are the ones most likely to share a UTC
    date with 'now' (the triggering condition for the old UTC-based days bug).
    """
    t = _tenant(db_session)
    lead = _lead(db_session, t)
    svc = MeetingService(db_session)

    now = datetime.now(timezone.utc)
    tz = ZoneInfo("Pacific/Auckland")
    today_local = now.astimezone(tz).date()

    # Generate slots over a 5-day LOCAL window — wide enough to guarantee slots
    # on the day(s) following today_local where the UTC/local date mismatch occurs.
    slots = svc.compute_availability(t.id, AVAIL_AUCKLAND, now=now, days=5)
    assert slots, "Expected at least one available slot in 5-day Auckland window"

    # Prefer the first slot that falls on a LOCAL day strictly after today_local.
    # Those slots are the ones whose UTC date can lag one day behind their local
    # date — which is the scenario that previously caused book() to undercount
    # the days window and miss the slot in generate_slots.
    later_slots = [s for s in slots if s.astimezone(tz).date() > today_local]
    slot = later_slots[0] if later_slots else slots[0]

    m = svc.book(
        t.id, lead,
        scheduled_at=slot,
        visitor_timezone="Pacific/Auckland",
        use_case=None,
        duration_min=30,
        availability=AVAIL_AUCKLAND,
    )
    assert m.status == MeetingStatus.SCHEDULED
