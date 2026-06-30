# Stage 3 Slice 5a — Notification Core + Email (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On three events (lead becomes sales-ready, meeting booked, quote generated), enqueue ARQ jobs that send email notifications (rep alerts + visitor meeting-confirmation), gated by `notify_channels`, idempotent via a `NotificationLog`.

**Architecture:** A **synchronous, pure** `NotificationService.dispatch` writes `NotificationLog` rows (idempotent via a unique index) and *returns the jobs to enqueue*; each caller enqueues in its own context (await in the async chat handler; a sync `asyncio.run` helper in the sync booking endpoint). ARQ worker jobs load the entity and send via the existing SMTP `EmailService`. This is the dispatch spine that 5b–5e extend.

**Tech Stack:** FastAPI, **synchronous** SQLAlchemy, Alembic, ARQ (Redis), `EmailService` (SMTP + Jinja templates), pytest.

## Global Constraints

- **Sync** SQLAlchemy (`db.execute(select(...)).scalar_one_or_none()`, `db.add/flush/commit`). ARQ workers get a session via `db = SessionLocal()` … `finally: db.close()` (mirror `app/workers/conversation_auto_resolver.py`).
- Notification work is **non-blocking**: `dispatch` only writes logs + returns jobs; SMTP runs in the worker. Every trigger call site wraps dispatch+enqueue in try/except so a failure never breaks the chat/booking response.
- **Idempotency** is enforced by a unique index on `NotificationLog (tenant_id, event, channel, entity_id, recipient)` — a duplicate dispatch hits the constraint and does NOT enqueue.
- Events: `sales_ready_lead` | `meeting_booked` | `quote_generated`. Channels enum: `email` | `in_app` | `slack` | `crm` (5a emits only `email`). Lowercase enum values (mirror `LeadStatus`).
- sales_ready threshold = `TIER_THRESHOLDS["sales_ready"]` = 86 (`app/sales/scoring.py`). "First crossing" = prior score `< 86` (or None) AND new score `>= 86`.
- Rep recipient resolution: `config.notification_email` → else tenant admin user email (`User.role == UserRole.TENANT_ADMIN`, scoped to tenant — mirror `email_notification_service.py:154`) → else skip.
- Reuse `EmailService.send_templated_email(to_email, subject, template_name, context)` and `render_template`. Templates live under `app/templates/`.
- Register new ARQ jobs in `WorkerSettings.functions` (`app/workers/document_processor.py:1199`).
- No Claude co-author trailer. Branch `feat/stage3-notif-email-backend` stacks on `feat/stage3-quote-backend` (Slice 4) — Quote/Meeting/Lead already exist; build alongside.
- Tests: `poetry run pytest <path> -v`, `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db`. Use `db_session` fixture. Migrations: `poetry run alembic revision -m "..."` + hand-write; apply `poetry run alembic upgrade head` (NOT-NULL columns via nullable-add→backfill→alter; no `:`-literals in `sa.text`).

---

### Task 1: `NotificationLog` model + migration

**Files:**
- Create: `app/db/models/notification_log.py`
- Modify: `app/db/models/__init__.py`
- Create: `alembic/versions/<generated>_add_notification_logs.py`
- Test: `tests/sales/test_notification_log_model.py`

**Interfaces:**
- Produces: `NotificationLog` (table `notification_logs`), `NotifEvent`/`NotifChannel`/`NotifStatus` enums. Unique index `uq_notification_dedupe (tenant_id, event, channel, entity_id, recipient)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_notification_log_model.py
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
    assert NotifEvent.MEETING_BOOKED.value == "meeting_booked"
    assert NotifChannel.EMAIL.value == "email"


def test_unique_dedupe(db_session):
    t = _tenant(db_session)
    eid = uuid4()
    kw = dict(tenant_id=t.id, event=NotifEvent.QUOTE_GENERATED, channel=NotifChannel.EMAIL,
              entity_type="quote", entity_id=eid, recipient="rep@co.com", status=NotifStatus.PENDING)
    db_session.add(NotificationLog(**kw)); db_session.flush()
    db_session.add(NotificationLog(**kw))
    with pytest.raises(IntegrityError):
        db_session.flush()
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_notification_log_model.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the model**

```python
# app/db/models/notification_log.py
"""NotificationLog — per-notification audit + idempotency record (Stage 3 Slice 5a)."""

import enum
import uuid

from sqlalchemy import Enum as SQLEnum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class NotifEvent(str, enum.Enum):
    SALES_READY_LEAD = "sales_ready_lead"
    MEETING_BOOKED = "meeting_booked"
    QUOTE_GENERATED = "quote_generated"


class NotifChannel(str, enum.Enum):
    EMAIL = "email"
    IN_APP = "in_app"
    SLACK = "slack"
    CRM = "crm"


class NotifStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class NotificationLog(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index("uq_notification_dedupe", "tenant_id", "event", "channel", "entity_id", "recipient", unique=True),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    event: Mapped[NotifEvent] = mapped_column(
        SQLEnum(NotifEvent, name="notif_event", values_callable=lambda x: [e.value for e in x]), nullable=False)
    channel: Mapped[NotifChannel] = mapped_column(
        SQLEnum(NotifChannel, name="notif_channel", values_callable=lambda x: [e.value for e in x]), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[NotifStatus] = mapped_column(
        SQLEnum(NotifStatus, name="notif_status", values_callable=lambda x: [e.value for e in x]),
        default=NotifStatus.PENDING, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<NotificationLog(event={self.event}, channel={self.channel}, status={self.status})>"
```

- [ ] **Step 4: Register + migration**

Add `from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus` to `app/db/models/__init__.py` (+ `__all__`). Then `poetry run alembic revision -m "add notification_logs"` and hand-write upgrade (create table with the three pg enums + the unique index + `ix_notification_logs_tenant_id`/`ix_notification_logs_entity_id`; `created_at`/`updated_at` per `TimestampMixin` — read `app/db/base.py`) / downgrade (drop table + 3 enums via `sa.Enum(name=...).drop(op.get_bind(), checkfirst=True)`).

- [ ] **Step 5: Apply + test**

Run: `poetry run alembic upgrade head` then `poetry run pytest tests/sales/test_notification_log_model.py -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db/models/notification_log.py app/db/models/__init__.py alembic/versions/ tests/sales/test_notification_log_model.py
git commit -m "feat(notifications): NotificationLog model + migration"
```

---

### Task 2: `NotificationService.dispatch` (sync, pure) + recipient resolution

**Files:**
- Create: `app/services/notification_service.py`
- Test: `tests/sales/test_notification_dispatch.py`

**Interfaces:**
- Produces: `NotificationService(db)` with `dispatch(tenant_id, event, *, entity_type, entity_id) -> list[tuple[uuid.UUID, str]]` — writes `NotificationLog` rows (status `pending`) for each enabled channel × applicable recipient, idempotently, and returns `[(log_id, arq_job_name)]` for the caller to enqueue. 5a emits only the `email` channel.
- `_rep_recipient(tenant_id, config) -> str | None` (config.notification_email → tenant admin email → None).

**Recipients per event:** `meeting_booked` → rep + visitor; `quote_generated` → rep; `sales_ready_lead` → rep. (Visitor only for meetings; its recipient is the lead email — resolved in the dispatch via the entity.)

**ARQ job name per (channel, recipient-kind):** email+rep → `send_rep_alert`; email+visitor → `send_visitor_meeting_confirmation`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_notification_dispatch.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_notification_dispatch.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# app/services/notification_service.py
"""NotificationService — synchronous dispatch spine (Stage 3 Slice 5a).

Writes NotificationLog rows idempotently and returns the ARQ jobs to enqueue;
the caller enqueues in its own (sync/async) context. SMTP happens in the worker.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifEvent, NotifStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.user import User, UserRole

# (event → does it notify the visitor?)
_VISITOR_EVENTS = {NotifEvent.MEETING_BOOKED}


class NotificationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _rep_recipient(self, tenant_id: uuid.UUID, config: TenantSalesAgentConfig | None) -> str | None:
        if config is not None and config.notification_email:
            return config.notification_email
        admin = self.db.execute(
            select(User).where(User.tenant_id == tenant_id, User.role == UserRole.TENANT_ADMIN)
        ).scalars().first()
        return admin.email if admin is not None else None

    def _visitor_recipient(self, entity_id: uuid.UUID) -> str | None:
        # meeting_booked: entity is a meeting; the lead's email is the visitor
        from app.db.models.meeting import Meeting
        m = self.db.get(Meeting, entity_id)
        if m is None:
            return None
        lead = self.db.get(Lead, m.lead_id)
        return lead.email if lead is not None and lead.email else None

    def _try_log(self, *, tenant_id, event, channel, entity_type, entity_id, recipient) -> uuid.UUID | None:
        """INSERT a pending log; return its id, or None if it already exists (idempotent)."""
        log = NotificationLog(
            tenant_id=tenant_id, event=event, channel=channel,
            entity_type=entity_type, entity_id=entity_id, recipient=recipient,
            status=NotifStatus.PENDING)
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            return None
        return log.id

    def dispatch(self, tenant_id, event, *, entity_type, entity_id) -> list[tuple[uuid.UUID, str]]:
        config = self.db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        channels = (config.notify_channels if config is not None else {}) or {}
        jobs: list[tuple[uuid.UUID, str]] = []

        if channels.get("email"):
            rep = self._rep_recipient(tenant_id, config)
            if rep:
                log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.EMAIL,
                                       entity_type=entity_type, entity_id=entity_id, recipient=rep)
                if log_id is not None:
                    jobs.append((log_id, "send_rep_alert"))
            if event in _VISITOR_EVENTS:
                visitor = self._visitor_recipient(entity_id)
                if visitor:
                    log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.EMAIL,
                                           entity_type=entity_type, entity_id=entity_id, recipient=visitor)
                    if log_id is not None:
                        jobs.append((log_id, "send_visitor_meeting_confirmation"))

        self.db.commit()
        return jobs
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_notification_dispatch.py -v`. Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add app/services/notification_service.py tests/sales/test_notification_dispatch.py
git commit -m "feat(notifications): synchronous dispatch spine + recipient resolution"
```

---

### Task 3: ARQ email jobs + templates + register

**Files:**
- Create: `app/workers/sales_notifications.py`
- Create: `app/templates/sales_rep_alert.html`, `app/templates/sales_meeting_confirmation.html` (match the real template dir/extension — inspect `app/templates/`)
- Modify: `app/workers/document_processor.py` (`WorkerSettings.functions`)
- Test: `tests/workers/test_sales_notifications.py`

**Interfaces:**
- Consumes: `NotificationLog` (Task 1), `EmailService`, `Lead`/`Meeting`/`Quote`.
- Produces: `async def send_rep_alert(ctx, notification_log_id: str)`, `async def send_visitor_meeting_confirmation(ctx, notification_log_id: str)`. Each loads the log via `SessionLocal()`, renders + sends, sets status `sent`/`failed`.

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_sales_notifications.py
from uuid import uuid4
from unittest.mock import patch

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_rep_alert


def _seed_quote_log(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="T", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000, status=QuoteStatus.DRAFT)
    db.add(q); db.flush()
    log = NotificationLog(tenant_id=t.id, event=NotifEvent.QUOTE_GENERATED, channel=NotifChannel.EMAIL,
                          entity_type="quote", entity_id=q.id, recipient="rep@co.com", status=NotifStatus.PENDING)
    db.add(log); db.commit()
    return log.id


async def test_send_rep_alert_marks_sent(db_session, monkeypatch):
    log_id = _seed_quote_log(db_session)
    # Patch SessionLocal used by the worker to return this test session, and EmailService send.
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    with patch("app.services.email_service.EmailService.send_templated_email", return_value=True) as send:
        await send_rep_alert({}, str(log_id))
    send.assert_called_once()
    log = db_session.get(NotificationLog, log_id)
    assert log.status == NotifStatus.SENT
```

> Note: adapt the `SessionLocal` monkeypatch + async-test style to the repo's conventions (check an existing `tests/workers/` test; if none, follow `tests/` async fixtures — `asyncio_mode = auto` is set in pyproject). If patching `db_session` as `SessionLocal` conflicts with the worker calling `.close()`, have the worker use a context the test can inject, or assert via a fresh query.

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/workers/test_sales_notifications.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the jobs**

```python
# app/workers/sales_notifications.py
"""ARQ jobs that deliver sales notification emails (Stage 3 Slice 5a)."""

import logging
import uuid

from app.db.session import SessionLocal
from app.db.models.lead import Lead
from app.db.models.meeting import Meeting
from app.db.models.quote import Quote
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

_REP_SUBJECT = {
    NotifEvent.SALES_READY_LEAD: "New sales-ready lead",
    NotifEvent.MEETING_BOOKED: "New demo booked",
    NotifEvent.QUOTE_GENERATED: "New quote generated",
}


def _entity_context(db, log: NotificationLog) -> dict:
    """Build the template context for the rep alert from the log's entity."""
    if log.entity_type == "lead":
        lead = db.get(Lead, log.entity_id)
        return {"kind": "lead", "name": lead.name, "company": lead.company, "score": lead.lead_score}
    if log.entity_type == "meeting":
        m = db.get(Meeting, log.entity_id)
        lead = db.get(Lead, m.lead_id) if m else None
        return {"kind": "meeting", "scheduled_at": m.scheduled_at if m else None,
                "name": lead.name if lead else None, "company": lead.company if lead else None}
    q = db.get(Quote, log.entity_id)
    lead = db.get(Lead, q.lead_id) if q else None
    return {"kind": "quote", "total": q.total if q else None, "currency": q.currency if q else None,
            "name": lead.name if lead else None, "company": lead.company if lead else None}


async def send_rep_alert(ctx, notification_log_id: str) -> None:
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return
        ctx_data = _entity_context(db, log)
        ok = EmailService().send_templated_email(
            to_email=log.recipient,
            subject=_REP_SUBJECT.get(log.event, "Sales notification"),
            template_name="sales_rep_alert",
            context=ctx_data,
        )
        log.status = NotifStatus.SENT if ok else NotifStatus.FAILED
        if not ok:
            log.error = "send_templated_email returned False"
        db.commit()
    except Exception as exc:  # noqa: BLE001 — mark failed, let ARQ retry
        logger.error(f"send_rep_alert failed: {exc}")
        try:
            log = db.get(NotificationLog, uuid.UUID(notification_log_id))
            if log is not None:
                log.status = NotifStatus.FAILED
                log.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()


async def send_visitor_meeting_confirmation(ctx, notification_log_id: str) -> None:
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return
        m = db.get(Meeting, log.entity_id)
        ok = EmailService().send_templated_email(
            to_email=log.recipient,
            subject="Your demo is booked",
            template_name="sales_meeting_confirmation",
            context={"scheduled_at": m.scheduled_at if m else None,
                     "visitor_timezone": m.visitor_timezone if m else None},
        )
        log.status = NotifStatus.SENT if ok else NotifStatus.FAILED
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"send_visitor_meeting_confirmation failed: {exc}")
        raise
    finally:
        db.close()
```

- [ ] **Step 4: Templates + register**

Create the two templates (inspect `app/templates/` for the existing layout/extension and match it — likely Jinja `.html`). `sales_rep_alert`: heading from event, a summary block using the context keys (`kind`/`name`/`company`/`score`/`scheduled_at`/`total`/`currency`), and a "View in dashboard" link. `sales_meeting_confirmation`: "You're booked" + `scheduled_at`. In `app/workers/document_processor.py`, import the two jobs and add them to `WorkerSettings.functions`.

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/workers/test_sales_notifications.py -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/workers/sales_notifications.py app/templates/ app/workers/document_processor.py tests/workers/test_sales_notifications.py
git commit -m "feat(notifications): ARQ rep-alert + visitor-confirmation email jobs"
```

---

### Task 4: Sales-ready crossing detection in `LeadService`

**Files:**
- Modify: `app/services/lead_service.py`
- Test: `tests/sales/test_sales_ready_crossing.py`

**Interfaces:**
- Produces: `LeadService.crossed_sales_ready(prior_score: int | None, lead: Lead) -> bool` — pure: `(prior_score or 0) < 86 <= (lead.lead_score or 0)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_sales_ready_crossing.py
from uuid import uuid4
from app.db.models.lead import Lead
from app.db.models.tenant import Tenant
from app.services.lead_service import LeadService


def test_crossing(db_session):
    svc = LeadService(db_session)
    lead = Lead(tenant_id=uuid4())
    lead.lead_score = 90
    assert svc.crossed_sales_ready(None, lead) is True   # new lead lands sales-ready
    assert svc.crossed_sales_ready(50, lead) is True     # warm → sales-ready
    assert svc.crossed_sales_ready(88, lead) is False    # already sales-ready
    lead.lead_score = 70
    assert svc.crossed_sales_ready(50, lead) is False    # didn't reach 86
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_sales_ready_crossing.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `app/services/lead_service.py`, add (use the existing `TIER_THRESHOLDS` import — it's already used in this file):

```python
    def crossed_sales_ready(self, prior_score: int | None, lead: Lead) -> bool:
        """True iff the lead's score first reached the sales_ready threshold this update."""
        threshold = TIER_THRESHOLDS["sales_ready"]
        return (prior_score or 0) < threshold <= (lead.lead_score or 0)
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_sales_ready_crossing.py -v`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/lead_service.py tests/sales/test_sales_ready_crossing.py
git commit -m "feat(notifications): LeadService.crossed_sales_ready detection"
```

---

### Task 5: Enqueue helpers + wire the three trigger seams

**Files:**
- Create: `app/services/notification_enqueue.py`
- Modify: `app/api/v1/endpoints/meetings.py` (meeting_booked), `app/api/v1/endpoints/chat.py` (quote_generated + sales_ready_lead)
- Test: `tests/api/v1/test_notification_triggers.py`

**Interfaces:**
- Consumes: `NotificationService.dispatch` (Task 2 — returns `[(log_id, job_name)]`), `crossed_sales_ready` (Task 4).
- Produces: `enqueue_jobs_sync(jobs: list[tuple[uuid.UUID, str]]) -> None` (async-context-safe via `asyncio`); `async def enqueue_jobs_async(jobs) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/v1/test_notification_triggers.py
# Verify each trigger writes exactly one NotificationLog (via dispatch) and that
# sales_ready only fires on the first crossing. Patch the enqueue helpers to no-op
# (no Redis in tests) and assert on NotificationLog rows.
#
# Reuse the meeting-booking seed from tests/api/v1/test_meeting_booking.py and the
# sales-generation mock from tests/api/v1/test_chat_lead_capture.py.
#
# Cases:
#  - book a meeting (notify_channels.email on) → 2 logs (rep + visitor) for that meeting.
#  - chat turn that generates a quote → 1 quote rep-alert log.
#  - chat turns scoring a lead from <86 to >=86 → 1 sales_ready_lead log; a further
#    turn keeping it >=86 → still 1 (no new log).
```

Write these against the REAL fixtures (read `test_meeting_booking.py` + `test_chat_lead_capture.py` first). Patch `app.services.notification_enqueue.enqueue_jobs_sync`/`enqueue_jobs_async` to no-ops so no Redis is needed; assert on `NotificationLog` rows.

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/v1/test_notification_triggers.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement enqueue helpers**

```python
# app/services/notification_enqueue.py
"""Context-aware ARQ enqueue for notification jobs (Stage 3 Slice 5a)."""

import asyncio
import uuid

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings


async def _enqueue(jobs: list[tuple[uuid.UUID, str]]) -> None:
    if not jobs:
        return
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        for log_id, job_name in jobs:
            await redis.enqueue_job(job_name, str(log_id))
    finally:
        await redis.close()


async def enqueue_jobs_async(jobs: list[tuple[uuid.UUID, str]]) -> None:
    await _enqueue(jobs)


def enqueue_jobs_sync(jobs: list[tuple[uuid.UUID, str]]) -> None:
    """Enqueue from a synchronous endpoint (no running event loop in the threadpool)."""
    if not jobs:
        return
    asyncio.run(_enqueue(jobs))
```

(Verify the `settings.REDIS_URL` + `RedisSettings.from_dsn` import path against `app/api/v1/endpoints/tenant_channels.py`'s usage.)

- [ ] **Step 4: Wire meeting_booked (sync endpoint)**

In `app/api/v1/endpoints/meetings.py` `book_meeting`, after the successful `MeetingService.book(...)` returns `meeting`, add (inside a try/except that logs and swallows):

```python
    try:
        from app.services.notification_service import NotificationService
        from app.services.notification_enqueue import enqueue_jobs_sync
        from app.db.models.notification_log import NotifEvent
        jobs = NotificationService(db).dispatch(
            lead.tenant_id, NotifEvent.MEETING_BOOKED, entity_type="meeting", entity_id=meeting.id)
        enqueue_jobs_sync(jobs)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"meeting notification dispatch failed (non-fatal): {exc}")
```

(`lead` is already resolved in `_resolve`; ensure `logger` exists in the module.)

- [ ] **Step 5: Wire quote_generated + sales_ready_lead (async chat handler)**

In `app/api/v1/endpoints/chat.py`:
- **quote_generated** — in the quote block, right after `QuoteService(db).create_quote(...)` returns the quote, capture the quote id and after the meta is set, dispatch + `await enqueue_jobs_async(...)` for `NotifEvent.QUOTE_GENERATED` (entity_type="quote", entity_id=quote.id). Wrap in try/except.
- **sales_ready_lead** — in the lead orchestration block, capture `_prior_score = lead.lead_score` BEFORE `enrich_from_chat`; after enrich, `if LeadService(db).crossed_sales_ready(_prior_score, lead): dispatch(NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id) + await enqueue_jobs_async(...)`. Wrap in try/except.

Both use `await enqueue_jobs_async(jobs)` since the handler is async. Keep all additions inside the existing `_sales_mode` guarded region; do not disturb lead-capture/offer_meeting/quote-meta behavior.

- [ ] **Step 6: Run tests + regression**

Run: `poetry run pytest tests/api/v1/test_notification_triggers.py tests/api/v1/test_chat_quote.py tests/api/v1/test_meeting_booking.py tests/api/v1/test_chat_lead_capture.py -v`
Expected: new triggers PASS; Slices 1–4 chat/meeting/quote tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/notification_enqueue.py app/api/v1/endpoints/meetings.py app/api/v1/endpoints/chat.py tests/api/v1/test_notification_triggers.py
git commit -m "feat(notifications): enqueue helpers + wire meeting/quote/sales-ready triggers"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1:** `poetry run alembic upgrade head` on a clean DB — applies.
- [ ] **Step 2:** `poetry run pytest -q -p no:cacheprovider --no-cov` (DATABASE_URL on 5433). Expected: all green; no regression to Slices 1–4.
- [ ] **Step 3:** Commit fixups if any: `git commit -am "test(notifications): 5a suite green"`.

---

## Self-Review notes
- Spec §2 events/triggers → Tasks 4+5. §4 model → Task 1. §5 dispatch spine → Task 2. §6 ARQ jobs → Task 3. §7 templates → Task 3. §8 invariants (idempotency, non-blocking, sync-enqueue, gating) → Tasks 2/5. §9 testing → per task + Task 6. §11 acceptance covered.
- Names consistent: `NotificationLog`/`NotifEvent`/`NotifChannel`/`NotifStatus`, `NotificationService.dispatch` (returns `[(log_id, job_name)]`), `crossed_sales_ready`, `send_rep_alert`/`send_visitor_meeting_confirmation`, `enqueue_jobs_sync`/`enqueue_jobs_async`.
- Sync SQLAlchemy; dispatch is sync + pure (no Redis) → testable without a worker; enqueue isolated in helpers.
- The implementer MUST: confirm `TimestampMixin` cols (migration), the `app/templates/` layout/extension (Task 3), the `settings.REDIS_URL`/`RedisSettings` import (Task 5 — mirror `tenant_channels.py`), reuse the real meeting/quote seed + sales-mock fixtures (Task 5 tests), and resolve the `SessionLocal` monkeypatch style for the worker test (Task 3). Note: the **auditor lead path** also scores leads — wiring a sales_ready dispatch there is a 5a follow-up (chat is the primary path); flag it, don't silently skip.
