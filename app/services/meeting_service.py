"""MeetingService — availability lookup + transactional booking (Stage 3 Slice 3)."""

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.meeting import Meeting, MeetingStatus
from app.sales.availability import generate_slots


class SlotUnavailable(Exception):
    """Raised when a requested slot is in the past or already booked."""


class MeetingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _booked_slots(self, tenant_id: UUID) -> list[datetime]:
        rows = self.db.execute(
            select(Meeting.scheduled_at).where(
                Meeting.tenant_id == tenant_id,
                Meeting.status == MeetingStatus.SCHEDULED,
            )
        ).scalars().all()
        return list(rows)

    def compute_availability(
        self, tenant_id: UUID, availability: dict, *, now: datetime, days: int
    ) -> list[datetime]:
        return generate_slots(availability, self._booked_slots(tenant_id), now=now, days=days)

    def _existing_future_meeting(self, tenant_id: UUID, lead_id: UUID, now: datetime) -> Meeting | None:
        return self.db.execute(
            select(Meeting).where(
                Meeting.tenant_id == tenant_id,
                Meeting.lead_id == lead_id,
                Meeting.status == MeetingStatus.SCHEDULED,
                Meeting.scheduled_at > now,
            )
        ).scalars().first()

    def book(
        self, tenant_id: UUID, lead: Lead, *,
        scheduled_at: datetime, visitor_timezone: str | None,
        use_case: str | None, duration_min: int,
        availability: dict,
    ) -> Meeting:
        """Book a meeting slot for the given lead.

        Raises SlotUnavailable if the slot is in the past, already taken, not
        within the tenant's availability windows, not aligned to a slot boundary,
        or within buffer_minutes of an existing meeting. scheduled_at must be
        timezone-aware.

        NOTE: calls db.commit() on success — this commits any other pending
        work in the session, so callers should not hold unrelated dirty state.
        """
        now = datetime.now(timezone.utc)
        if scheduled_at.tzinfo is None:
            raise SlotUnavailable("scheduled_at must be timezone-aware")
        scheduled_at = scheduled_at.astimezone(timezone.utc)
        if scheduled_at <= now:
            raise SlotUnavailable("Slot is in the past")

        # Idempotency: a lead keeps one live future meeting (defends double-submit).
        existing = self._existing_future_meeting(tenant_id, lead.id, now)
        if existing is not None:
            return existing

        # Slot membership guard: scheduled_at must be one of the tenant's computed
        # valid slots — this enforces weekday, hours, alignment, past-exclusion, AND
        # inter-meeting buffer in one shot.  The days window is computed using the
        # tenant's LOCAL calendar dates (matching what generate_slots iterates) so
        # that tenants at UTC+12..+14 don't lose slots that sit on the next local
        # day while sharing the same UTC calendar date as today.
        tz_key = availability.get("tenant_timezone") or "UTC"
        today_local = now.astimezone(ZoneInfo(tz_key)).date()
        scheduled_local = scheduled_at.astimezone(ZoneInfo(tz_key)).date()
        days = max(1, (scheduled_local - today_local).days + 1)
        valid_slots = set(self.compute_availability(tenant_id, availability, now=now, days=days))
        if scheduled_at not in valid_slots:
            raise SlotUnavailable("Not an available slot")

        # Transactional double-booking guard: re-check then insert; the unique
        # partial index is the final arbiter under concurrency.
        taken = self.db.execute(
            select(Meeting.id).where(
                Meeting.tenant_id == tenant_id,
                Meeting.status == MeetingStatus.SCHEDULED,
                Meeting.scheduled_at == scheduled_at,
            )
        ).first()
        if taken is not None:
            raise SlotUnavailable("Slot just taken")

        meeting = Meeting(
            tenant_id=tenant_id, lead_id=lead.id, conversation_id=lead.conversation_id,
            scheduled_at=scheduled_at, duration_min=duration_min,
            visitor_timezone=visitor_timezone, use_case=use_case,
            status=MeetingStatus.SCHEDULED,
        )
        self.db.add(meeting)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise SlotUnavailable("Slot just taken") from exc

        lead.meeting_id = meeting.id
        self.db.commit()
        self.db.refresh(meeting)
        return meeting
