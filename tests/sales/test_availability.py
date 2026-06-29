from datetime import datetime, timedelta, timezone

from app.sales.availability import generate_slots

AVAIL = {
    "weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "11:00",
    "slot_minutes": 30, "buffer_minutes": 15, "tenant_timezone": "UTC",
}


def test_generates_utc_slots_inside_window():
    # Wed 2026-07-01 08:00 UTC "now" → same-day slots 09:00,09:30,10:00,10:30
    now = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    slots = generate_slots(AVAIL, [], now=now, days=1)
    assert slots[0] == datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    assert datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc) in slots
    assert datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc) not in slots  # end exclusive


def test_excludes_past_slots():
    now = datetime(2026, 7, 1, 9, 45, tzinfo=timezone.utc)
    slots = generate_slots(AVAIL, [], now=now, days=1)
    assert all(s > now for s in slots)
    assert datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc) not in slots


def test_excludes_booked_within_buffer():
    now = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    booked = [datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)]
    slots = generate_slots(AVAIL, booked, now=now, days=1)
    # 09:30 booked; buffer 15m removes 09:30 itself (and any within 15m)
    assert datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc) not in slots
    assert datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc) in slots


def test_respects_tenant_timezone():
    avail = {**AVAIL, "tenant_timezone": "Africa/Lagos"}  # UTC+1
    now = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    slots = generate_slots(avail, [], now=now, days=1)
    # 09:00 Lagos == 08:00 UTC
    assert slots[0] == datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)


def test_skips_non_available_weekdays():
    now = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)  # Sat
    slots = generate_slots(AVAIL, [], now=now, days=1)
    assert slots == []
