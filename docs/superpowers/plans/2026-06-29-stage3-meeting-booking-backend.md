# Stage 3 Slice 3 — Meeting Booking (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the chat agent offer an inline meeting picker on demo intent and book a native `Meeting` (linked to the Slice 1 lead) from tenant-defined availability windows — no external calendar.

**Architecture:** A new `Meeting` model + Alembic migration; an `availability` JSON column on `TenantSalesAgentConfig`; a `MeetingService` with a pure slot-generation function and a transactional booking method; two public widget endpoints (`GET /chat/meetings/availability`, `POST /chat/meetings`) anchored on the lead (resolved by `conversation_id`); and an `offer_meeting` signal piggybacked on the existing Slice 2 structured output.

**Tech Stack:** FastAPI, **synchronous** SQLAlchemy ORM (`sqlalchemy.orm.Session`, no `await` on `db`), Pydantic v2, Alembic, pytest, OpenAI structured output.

## Global Constraints

- Backend uses **sync** SQLAlchemy: `db.execute(select(...)).scalar_one_or_none()`, `db.add()`, `db.flush()`, `db.commit()`, `db.refresh()`. Do NOT introduce `await`/`AsyncSession`.
- Services follow `LeadService`: `class XService:` with `def __init__(self, db: Session)`.
- All times stored as timezone-aware UTC. Tenant windows are interpreted in `availability.tenant_timezone` (IANA) via `zoneinfo.ZoneInfo`.
- Tenant isolation: every query filters by `tenant_id`.
- Sales-mode-off / non-demo path is unchanged. No emails/notifications in this slice (Slice 5 owns them).
- Status strings serialize **lowercase** (mirror `LeadStatus`).
- Commit messages MUST NOT include a Claude co-author trailer.
- Migrations: `poetry run alembic revision -m "..."` (generates correct `down_revision` from current head), hand-write `upgrade`/`downgrade`. Run with `poetry run alembic upgrade head`.
- Tests: `poetry run pytest <path> -v`. Use the `db_session` fixture + a local `_make_tenant` / config / lead helper (mirror `tests/sales/test_lead_service.py`).

---

### Task 1: `Meeting` model + `availability` config column + migration

**Files:**
- Create: `app/db/models/meeting.py`
- Modify: `app/db/models/sales_agent_config.py`
- Create: `alembic/versions/<generated>_add_meetings_and_availability.py`
- Test: `tests/sales/test_meeting_model.py`

**Interfaces:**
- Produces: `Meeting` (table `meetings`) with `id, tenant_id, lead_id, conversation_id, scheduled_at, duration_min, visitor_timezone, use_case, status, created_at`; `MeetingStatus` enum (`scheduled`/`cancelled`). `TenantSalesAgentConfig.availability: dict | None` + `_DEFAULT_AVAILABILITY`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_meeting_model.py
from uuid import uuid4
from datetime import datetime, timezone

from app.db.models.meeting import Meeting, MeetingStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_meeting_persists_with_lowercase_status(db_session):
    t = _tenant(db_session)
    m = Meeting(
        tenant_id=t.id, lead_id=uuid4(), conversation_id=uuid4(),
        scheduled_at=datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc),
        duration_min=30, visitor_timezone="Africa/Lagos",
        status=MeetingStatus.SCHEDULED,
    )
    db_session.add(m); db_session.flush(); db_session.refresh(m)
    assert m.id is not None
    assert m.status == MeetingStatus.SCHEDULED
    assert MeetingStatus.SCHEDULED.value == "scheduled"


def test_config_availability_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.availability["slot_minutes"] == 30
    assert cfg.availability["weekdays"] == [1, 2, 3, 4, 5]
    assert cfg.availability["tenant_timezone"] == "UTC"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_meeting_model.py -v`
Expected: FAIL (`ModuleNotFoundError: app.db.models.meeting` / no `availability`).

- [ ] **Step 3: Implement the model**

```python
# app/db/models/meeting.py
"""Meeting — a native booking made via the chat-widget meeting picker (Stage 3 Slice 3)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SQLEnum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from app.db.base import Base, TimestampMixin, UUIDMixin


class MeetingStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class Meeting(Base, UUIDMixin, TimestampMixin):
    """A booked meeting slot, owned by a lead. No external calendar in this slice."""

    __tablename__ = "meetings"
    __table_args__ = (
        # Defence-in-depth against double-booking a live slot for a tenant.
        Index(
            "uq_meeting_tenant_slot_scheduled",
            "tenant_id",
            "scheduled_at",
            unique=True,
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    visitor_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    use_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        SQLEnum(MeetingStatus, name="meeting_status", values_callable=lambda x: [e.value for e in x]),
        default=MeetingStatus.SCHEDULED,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Meeting(id={self.id}, tenant_id={self.tenant_id}, scheduled_at={self.scheduled_at})>"
```

- [ ] **Step 4: Add `availability` to the config model**

In `app/db/models/sales_agent_config.py`, add the default constant near the others:

```python
_DEFAULT_AVAILABILITY: dict = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "09:00",
    "end": "17:00",
    "slot_minutes": 30,
    "buffer_minutes": 15,
    "tenant_timezone": "UTC",
}
```

Add the column inside the class (after `currency`):

```python
    availability: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: dict(_DEFAULT_AVAILABILITY),
        comment="Bookable-window rules for the meeting picker",
    )
```

And in `default_for(...)` add `availability=dict(_DEFAULT_AVAILABILITY),` to the constructor kwargs.

- [ ] **Step 5: Register the model + create the migration**

Ensure `Meeting` is imported where models are registered (mirror how `Lead` is imported — check `app/db/models/__init__.py` or `app/db/base`; add `from app.db.models.meeting import Meeting, MeetingStatus` alongside the existing model imports). Then:

```bash
poetry run alembic revision -m "add meetings table and availability config"
```

Hand-write the generated file's `upgrade()`/`downgrade()` (mirror the style in `alembic/versions/z3a4b5c6d7e8_*.py`):

```python
def upgrade() -> None:
    op.create_table(
        "meetings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("visitor_timezone", sa.String(length=64), nullable=True),
        sa.Column("use_case", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("scheduled", "cancelled", name="meeting_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_meetings_tenant_id", "meetings", ["tenant_id"])
    op.create_index("ix_meetings_lead_id", "meetings", ["lead_id"])
    op.create_index("ix_meetings_conversation_id", "meetings", ["conversation_id"])
    op.create_index(
        "uq_meeting_tenant_slot_scheduled", "meetings", ["tenant_id", "scheduled_at"],
        unique=True, postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.add_column(
        "tenant_sales_agent_config",
        sa.Column("availability", sa.JSON(), nullable=False,
                  server_default=sa.text("""'{"weekdays":[1,2,3,4,5],"start":"09:00","end":"17:00","slot_minutes":30,"buffer_minutes":15,"tenant_timezone":"UTC"}'""")),
    )
    op.alter_column("tenant_sales_agent_config", "availability", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "availability")
    op.drop_index("uq_meeting_tenant_slot_scheduled", table_name="meetings")
    op.drop_table("meetings")
    sa.Enum(name="meeting_status").drop(op.get_bind(), checkfirst=True)
```

Verify the timestamp column names/types match `TimestampMixin` (read `app/db/base.py`; adjust `created_at`/`updated_at` defs to match exactly).

- [ ] **Step 6: Apply migration + run tests**

Run: `poetry run alembic upgrade head` then `poetry run pytest tests/sales/test_meeting_model.py -v`
Expected: migration applies; tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/db/models/meeting.py app/db/models/sales_agent_config.py alembic/versions/ tests/sales/test_meeting_model.py app/db/models/__init__.py
git commit -m "feat(sales): Meeting model + availability config column + migration"
```

---

### Task 2: Availability slot generation

**Files:**
- Create: `app/sales/availability.py`
- Test: `tests/sales/test_availability.py`

**Interfaces:**
- Produces: `generate_slots(availability: dict, booked: list[datetime], *, now: datetime, days: int) -> list[datetime]` — returns future tz-aware **UTC** slot datetimes inside the tenant's windows over the next `days`, excluding past slots and any slot within `buffer_minutes` of a booked meeting. Pure function (no DB), so it is unit-testable in isolation.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_availability.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_availability.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# app/sales/availability.py
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
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_availability.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add app/sales/availability.py tests/sales/test_availability.py
git commit -m "feat(sales): pure availability slot generation"
```

---

### Task 3: `MeetingService` — availability + transactional booking

**Files:**
- Create: `app/services/meeting_service.py`
- Test: `tests/sales/test_meeting_service.py`

**Interfaces:**
- Consumes: `Meeting`, `MeetingStatus` (Task 1), `generate_slots` (Task 2), `Lead`.
- Produces: `MeetingService(db)` with `compute_availability(tenant_id, availability, *, now, days) -> list[datetime]` and `book(tenant_id, lead, *, scheduled_at, visitor_timezone, use_case, duration_min) -> Meeting`. Raises `SlotUnavailable` on a taken/invalid slot. Idempotent: re-booking when the lead already has a future scheduled meeting returns the existing one.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_meeting_service.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_meeting_service.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# app/services/meeting_service.py
"""MeetingService — availability lookup + transactional booking (Stage 3 Slice 3)."""

from datetime import datetime, timezone
from uuid import UUID

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
    ) -> Meeting:
        now = datetime.now(timezone.utc)
        scheduled_at = scheduled_at.astimezone(timezone.utc)
        if scheduled_at <= now:
            raise SlotUnavailable("Slot is in the past")

        # Idempotency: a lead keeps one live future meeting (defends double-submit).
        existing = self._existing_future_meeting(tenant_id, lead.id, now)
        if existing is not None:
            return existing

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
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_meeting_service.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add app/services/meeting_service.py tests/sales/test_meeting_service.py
git commit -m "feat(sales): MeetingService availability + transactional booking"
```

---

### Task 4: Public widget endpoints + schemas

**Files:**
- Create: `app/schemas/meeting.py`
- Create: `app/api/v1/endpoints/meetings.py`
- Modify: `app/api/v1/api.py` (mount the router)
- Test: `tests/api/v1/test_meeting_booking.py`

**Interfaces:**
- Consumes: `MeetingService`, `SlotUnavailable`, `Lead`, `TenantSalesAgentConfig`.
- Produces: `GET /chat/meetings/availability?conversation_id=&days=` and `POST /chat/meetings`. Envelope `{success, data}` / `{success:false, error}` (mirror `leads.py` `_err`). Tenant + lead resolved by `conversation_id` (lead carries `tenant_id`); gated by `meeting_booking_enabled`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/v1/test_meeting_booking.py
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.db.models.lead import Lead
from app.db.models.tenant import Tenant
from app.db.models.sales_agent_config import TenantSalesAgentConfig

client = TestClient(app)


def _seed(db, *, booking_enabled=True):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.meeting_booking_enabled = booking_enabled
    cfg.availability = {"weekdays": [1,2,3,4,5,6,7], "start": "00:00", "end": "23:30",
                        "slot_minutes": 30, "buffer_minutes": 0, "tenant_timezone": "UTC"}
    db.add(cfg)
    conv = uuid4()
    lead = Lead(tenant_id=t.id, conversation_id=conv, email="a@b.com")
    db.add(lead); db.commit()
    return t, conv, lead


def test_availability_returns_slots(client_db):  # client_db = fixture wiring TestClient to db_session
    db = client_db
    t, conv, lead = _seed(db)
    r = client.get(f"/api/v1/chat/meetings/availability?conversation_id={conv}&days=2")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert len(body["data"]["slots"]) > 0
    assert "start_utc" in body["data"]["slots"][0]


def test_availability_disabled_returns_empty(client_db):
    db = client_db
    t, conv, lead = _seed(db, booking_enabled=False)
    r = client.get(f"/api/v1/chat/meetings/availability?conversation_id={conv}")
    assert r.json()["data"]["slots"] == []


def test_book_creates_and_confirms(client_db):
    db = client_db
    t, conv, lead = _seed(db)
    when = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=1))
    r = client.post("/api/v1/chat/meetings", json={
        "conversation_id": str(conv), "scheduled_at": when.isoformat(),
        "visitor_timezone": "Africa/Lagos", "use_case": "Demo",
    })
    assert r.status_code == 200
    assert r.json()["data"]["meeting"]["duration_min"] == 30


def test_double_book_conflicts(client_db):
    db = client_db
    t, conv, lead = _seed(db)
    when = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=1))
    payload = {"conversation_id": str(conv), "scheduled_at": when.isoformat(), "visitor_timezone": "UTC"}
    client.post("/api/v1/chat/meetings", json=payload)
    # second lead/conversation, same tenant + slot
    conv2 = uuid4()
    db.add(Lead(tenant_id=t.id, conversation_id=conv2, email="c@d.com")); db.commit()
    payload2 = {**payload, "conversation_id": str(conv2)}
    r = client.post("/api/v1/chat/meetings", json=payload2)
    assert r.status_code == 409
```

> Note: if no `client_db` fixture exists that binds `TestClient` to the rolled-back `db_session`, follow the existing API test setup in `tests/api/v1/` (e.g. how `test_chat_lead_capture.py` overrides `get_db`). Reuse that exact override fixture; do not invent a new DB wiring.

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/v1/test_meeting_booking.py -v`
Expected: FAIL (404 / route missing).

- [ ] **Step 3: Implement schemas**

```python
# app/schemas/meeting.py
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AvailabilitySlot(BaseModel):
    start_utc: datetime


class AvailabilityResponse(BaseModel):
    slots: list[AvailabilitySlot]


class BookMeetingRequest(BaseModel):
    conversation_id: UUID
    scheduled_at: datetime
    visitor_timezone: str | None = None
    use_case: str | None = None


class BookedMeeting(BaseModel):
    scheduled_at: datetime
    duration_min: int


class BookMeetingResponse(BaseModel):
    meeting: BookedMeeting
```

- [ ] **Step 4: Implement endpoints**

```python
# app/api/v1/endpoints/meetings.py
"""Public widget endpoints for the inline meeting picker (Stage 3 Slice 3).

Tenant is resolved from the lead (looked up by conversation_id); the picker is
only offered after a lead exists, so the lead is the natural, tenant-scoped anchor.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.schemas.meeting import AvailabilityResponse, AvailabilitySlot, BookMeetingRequest, BookedMeeting, BookMeetingResponse
from app.services.meeting_service import MeetingService, SlotUnavailable

router = APIRouter()


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(status_code=http, content={"success": False, "error": {"code": code, "message": message}})


def _resolve(db: Session, conversation_id: UUID) -> tuple[Lead, TenantSalesAgentConfig] | None:
    lead = db.execute(select(Lead).where(Lead.conversation_id == conversation_id)).scalars().first()
    if lead is None or lead.tenant_id is None:
        return None
    cfg = db.execute(
        select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == lead.tenant_id)
    ).scalar_one_or_none()
    return (lead, cfg) if cfg is not None else None


@router.get("/meetings/availability")
def get_availability(
    conversation_id: UUID = Query(...),
    days: int = Query(default=14, ge=1, le=60),
    db: Session = Depends(get_db),
):
    resolved = _resolve(db, conversation_id)
    if resolved is None:
        return {"success": True, "data": {"slots": []}}
    lead, cfg = resolved
    if not cfg.meeting_booking_enabled:
        return {"success": True, "data": {"slots": []}}
    slots = MeetingService(db).compute_availability(
        lead.tenant_id, cfg.availability, now=datetime.now(timezone.utc), days=days
    )
    payload = AvailabilityResponse(slots=[AvailabilitySlot(start_utc=s) for s in slots])
    return {"success": True, "data": payload.model_dump()}


@router.post("/meetings")
def book_meeting(payload: BookMeetingRequest, db: Session = Depends(get_db)):
    resolved = _resolve(db, payload.conversation_id)
    if resolved is None:
        return _err("LEAD_NOT_FOUND", "No lead for this conversation", 404)
    lead, cfg = resolved
    if not cfg.meeting_booking_enabled:
        return _err("BOOKING_DISABLED", "Meeting booking is not enabled", 403)
    try:
        meeting = MeetingService(db).book(
            lead.tenant_id, lead,
            scheduled_at=payload.scheduled_at,
            visitor_timezone=payload.visitor_timezone,
            use_case=payload.use_case,
            duration_min=int(cfg.availability.get("slot_minutes", 30)),
        )
    except SlotUnavailable as exc:
        return _err("SLOT_UNAVAILABLE", str(exc), 409)
    resp = BookMeetingResponse(meeting=BookedMeeting(scheduled_at=meeting.scheduled_at, duration_min=meeting.duration_min))
    return {"success": True, "data": resp.model_dump()}
```

- [ ] **Step 5: Mount the router**

In `app/api/v1/api.py`, mirror how the chat router is included. Add:

```python
from app.api.v1.endpoints import meetings  # noqa
api_router.include_router(meetings.router, prefix="/chat", tags=["meetings"])
```

(Use the actual router variable name in that file — check whether it's `api_router` or `router`.)

- [ ] **Step 6: Run tests**

Run: `poetry run pytest tests/api/v1/test_meeting_booking.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/meeting.py app/api/v1/endpoints/meetings.py app/api/v1/api.py tests/api/v1/test_meeting_booking.py
git commit -m "feat(sales): public meeting availability + booking endpoints"
```

---

### Task 5: Piggyback `offer_meeting` signal + surface `meta.offer_meeting`

**Files:**
- Modify: `app/sales/intent.py`
- Modify: `app/api/v1/endpoints/chat.py`
- Modify: `app/schemas/conversation.py`
- Test: `tests/sales/test_offer_meeting_signal.py`

**Interfaces:**
- Consumes: existing `SALES_RESPONSE_SCHEMA`, `parse_sales_output`, the chat sales-mode block.
- Produces: `signals.offer_meeting: bool` in the schema; `meta.offer_meeting: bool` set on the chat response when `signals.offer_meeting and config.meeting_booking_enabled`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_offer_meeting_signal.py
from app.sales.intent import SALES_RESPONSE_SCHEMA, parse_sales_output
import json


def test_schema_includes_offer_meeting():
    sig = SALES_RESPONSE_SCHEMA["properties"]["signals"]
    assert "offer_meeting" in sig["properties"]
    assert "offer_meeting" in sig["required"]
    assert sig["additionalProperties"] is False


def test_parse_passes_offer_meeting_through():
    raw = json.dumps({
        "reply": "Sure!", "intent": "DEMO_REQUEST",
        "signals": {"asked_features": False, "requested_demo": True,
                    "mentioned_competitor": False, "ready_to_buy": False, "offer_meeting": True},
        "captured": {"name": None, "email": None, "company": None, "team_size": None, "industry": None},
    })
    out = parse_sales_output(raw)
    assert out["signals"]["offer_meeting"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_offer_meeting_signal.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement schema + directive**

In `app/sales/intent.py`, inside `SALES_RESPONSE_SCHEMA["properties"]["signals"]`, add `"offer_meeting": {"type": "boolean"}` to `properties` and `"offer_meeting"` to the `required` list. In `build_sales_directive`, append a line:

```
"6. Set signals.offer_meeting to true ONLY when the prospect wants a live "
"demo or call (e.g. DEMO_REQUEST) and scheduling is the natural next step; "
"otherwise false.\n"
```

- [ ] **Step 4: Surface `meta.offer_meeting` in chat.py**

In `app/api/v1/endpoints/chat.py`, where `_message_meta` is assembled (around line 2147) and after the `_lead_captured_meta` block, add:

```python
        if _sales_mode and _sales_meta is not None:
            _signals = _sales_meta.get("signals") or {}
            if bool(_signals.get("offer_meeting")) and getattr(sales_agent_config, "meeting_booking_enabled", False):
                _message_meta["offer_meeting"] = True
```

(Place it alongside the existing `if _lead_captured_meta is not None:` block. Server-side gate is authoritative — the flag never appears when `meeting_booking_enabled` is false.)

- [ ] **Step 5: Document the meta field**

In `app/schemas/conversation.py`, extend the `meta` field description on `MessageResponse` to note `'offer_meeting' (bool) when the agent offers the inline meeting picker`.

- [ ] **Step 6: Run tests + regression**

Run: `poetry run pytest tests/sales/ tests/api/v1/test_chat_lead_capture.py -v`
Expected: new tests PASS; Slice 2 lead-capture tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add app/sales/intent.py app/api/v1/endpoints/chat.py app/schemas/conversation.py tests/sales/test_offer_meeting_signal.py
git commit -m "feat(sales): offer_meeting signal + meta.offer_meeting gating"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1:** Run `poetry run alembic upgrade head` on a clean DB; confirm it applies.
- [ ] **Step 2:** Run `poetry run pytest -q`. Expected: all green, no regression to chat/support/Slice 1–2 suites.
- [ ] **Step 3:** Commit any fixups: `git commit -am "test(sales): meeting-booking suite green"`.

---

## Self-Review notes
- Spec §2 model → Task 1. §3 availability config → Task 1. §4 piggyback → Task 5. §5 endpoints → Task 4 (slot math Task 2, service Task 3). §6 safety (UTC, double-book, gate, tenant scope) → Tasks 2/3/4/5. §7 testing → per-task. §9 acceptance covered across Tasks 4–6.
- Names consistent across tasks: `Meeting`, `MeetingStatus.SCHEDULED`, `generate_slots`, `MeetingService.compute_availability`/`.book`, `SlotUnavailable`, `meta.offer_meeting`, `signals.offer_meeting`, `availability` config column.
- Sync SQLAlchemy throughout; no `await` on db.
- The implementer MUST confirm: the `api.py` router variable name, the `TimestampMixin` column defs (for the migration), the existing API-test DB-override fixture (Task 4 Step 1 note), and that `Meeting` is registered in the models package so Alembic + relationships resolve.
