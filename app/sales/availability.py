"""Pure availability-slot generation for the meeting picker (Stage 3 Slice 3).

No DB access — takes config + already-booked datetimes, returns UTC slot starts.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def generate_slots(
    availability: dict,
    booked: list[datetime],
    *,
    now: datetime,
    days: int,
) -> list[datetime]:
    """Return future UTC slot starts within the tenant's windows over `days`.

    Excludes past slots and any slot within `buffer_minutes` of a booked meeting.
    """
    tz = ZoneInfo(availability.get("tenant_timezone") or "UTC")
    weekdays = set(availability.get("weekdays") or [])
    start_t = _parse_hhmm(availability.get("start", "09:00"))
    end_t = _parse_hhmm(availability.get("end", "17:00"))
    step = int(availability.get("slot_minutes", 30))
    buffer = int(availability.get("buffer_minutes", 0))

    now_utc = now.astimezone(timezone.utc)
    today_local = now.astimezone(tz).date()
    booked_utc = [b.astimezone(timezone.utc) for b in booked]
    out: list[datetime] = []

    for offset in range(days):
        day = today_local + timedelta(days=offset)
        if day.isoweekday() not in weekdays:
            continue
        cursor = datetime.combine(day, start_t, tzinfo=tz)
        end = datetime.combine(day, end_t, tzinfo=tz)
        while cursor < end:
            slot_utc = cursor.astimezone(timezone.utc)
            if slot_utc > now_utc and not _within_buffer(slot_utc, booked_utc, buffer):
                out.append(slot_utc)
            cursor += timedelta(minutes=step)
    return out


def _within_buffer(slot: datetime, booked: list[datetime], buffer_min: int) -> bool:
    window = timedelta(minutes=buffer_min)
    return any(abs((slot - b)) <= window for b in booked)
