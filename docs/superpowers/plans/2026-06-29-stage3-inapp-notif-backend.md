# Stage 3 Slice 5c — In-app Notifications (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write `in_app` notification rows on dispatch (with rendered title/body + read-state) and expose a tenant-scoped read API for the dashboard bell/feed.

**Architecture:** Extend `NotificationLog` with `title`/`body`/`read_at`; `NotificationService.dispatch` writes an `in_app` row inline (status `sent`, no ARQ job) when `notify_channels.in_app` is on, rendering short text from the entity; a new authed `notifications` router lists/marks-read those rows.

**Tech Stack:** FastAPI, SYNC SQLAlchemy, Alembic, pytest.

## Global Constraints

- **Sync** SQLAlchemy (`db.execute(select(...))...`, `db.add/flush/commit`). No async ORM.
- In-app rows: `channel=NotifChannel.IN_APP`, `recipient="in_app"` (fixed sentinel → one notification per `(tenant, event, entity)` via the existing `uq_notification_dedupe` index), `status=NotifStatus.SENT` on write, `read_at=NULL`. **No ARQ job** for in-app — the row IS the notification.
- In-app written only when `notify_channels.in_app` truthy; idempotent (reuse `_try_log`'s savepoint pattern). Email-channel behavior + the email-job return value UNCHANGED.
- Read API authed via `get_current_tenant_admin` (mirror `app/api/v1/endpoints/leads.py`), `{success, data}` envelope (use leads.py's `_err` helper pattern), strictly scoped to `tenant_id == user.tenant_id` AND `channel='in_app'`. `read_at` is the only mutable field.
- Lowercase enum values already on `NotifChannel`/`NotifStatus`. No Claude co-author trailer. Branch `feat/stage3-inapp-notif-backend` stacks on `feat/stage3-quote-pdf-backend` (5b) — `NotificationLog`, `NotifChannel.IN_APP`, `NotificationService` already exist.
- Migrations: `poetry run alembic revision -m "..."` + hand-write; apply `poetry run alembic upgrade head`. Tests: `poetry run pytest <path> -v`, `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db` (`docker start luran_test_db` if 5433 refuses). Use `db_session`/`pg_client`.

---

### Task 1: `NotificationLog` columns + migration

**Files:**
- Modify: `app/db/models/notification_log.py`
- Create: `alembic/versions/<generated>_add_notification_inapp_fields.py`
- Test: `tests/sales/test_notification_inapp_columns.py`

**Interfaces:**
- Produces: `NotificationLog.title: str | None`, `.body: str | None`, `.read_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_notification_inapp_columns.py
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_notification_inapp_columns.py -v`
Expected: FAIL (no `title`).

- [ ] **Step 3: Implement the columns**

In `app/db/models/notification_log.py`, add imports `from datetime import datetime` and `DateTime` to the sqlalchemy import, and add columns (after `error`):

```python
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    body: Mapped[str | None] = mapped_column(String(400), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Migration**

`poetry run alembic revision -m "add notification_logs in-app fields"`; hand-write:
```python
def upgrade() -> None:
    op.add_column("notification_logs", sa.Column("title", sa.String(length=160), nullable=True))
    op.add_column("notification_logs", sa.Column("body", sa.String(length=400), nullable=True))
    op.add_column("notification_logs", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column("notification_logs", "read_at")
    op.drop_column("notification_logs", "body")
    op.drop_column("notification_logs", "title")
```

- [ ] **Step 5: Apply + test**

Run: `poetry run alembic upgrade head` then `poetry run pytest tests/sales/test_notification_inapp_columns.py -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db/models/notification_log.py alembic/versions/ tests/sales/test_notification_inapp_columns.py
git commit -m "feat(notifications): NotificationLog in-app fields (title/body/read_at)"
```

---

### Task 2: In-app dispatch (`_render_in_app_text` + write row inline)

**Files:**
- Modify: `app/services/notification_service.py`
- Test: `tests/sales/test_inapp_dispatch.py`

**Interfaces:**
- Consumes: `NotificationLog`/`NotifChannel.IN_APP`/`NotifStatus.SENT` (Task 1).
- Produces: `NotificationService._render_in_app_text(event, entity_type, entity_id) -> tuple[str, str]`; `dispatch` also writes an in-app row when `notify_channels.in_app`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_inapp_dispatch.py
from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, email=False, in_app=True):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": email, "slack": False, "in_app": in_app}
    cfg.notification_email = "rep@co.com"
    db.add(cfg); db.commit()
    return t


def _quote(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 62050}], total=62050, status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _inapp_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.IN_APP).all()


def test_dispatch_writes_inapp_row(db_session):
    t = _cfg(db_session, in_app=True); q = _quote(db_session, t)
    NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    rows = _inapp_rows(db_session, t)
    assert len(rows) == 1
    r = rows[0]
    assert r.status == NotifStatus.SENT and r.read_at is None and r.recipient == "in_app"
    assert r.title and r.body  # non-empty rendered text


def test_dispatch_inapp_idempotent(db_session):
    t = _cfg(db_session, in_app=True); q = _quote(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert len(_inapp_rows(db_session, t)) == 1


def test_dispatch_inapp_off(db_session):
    t = _cfg(db_session, in_app=False); q = _quote(db_session, t)
    NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert _inapp_rows(db_session, t) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_inapp_dispatch.py -v`
Expected: FAIL (no in-app row written).

- [ ] **Step 3: Implement**

Read the current `NotificationService` (`dispatch`, `_try_log`). Add `_render_in_app_text` and extend `_try_log` to accept optional `status`/`title`/`body`, then write the in-app row in `dispatch`.

In `_try_log`, change the signature + row construction to accept extras (default to the current behavior):
```python
    def _try_log(self, *, tenant_id, event, channel, entity_type, entity_id, recipient,
                 status=NotifStatus.PENDING, title=None, body=None) -> uuid.UUID | None:
        log = NotificationLog(
            tenant_id=tenant_id, event=event, channel=channel,
            entity_type=entity_type, entity_id=entity_id, recipient=recipient,
            status=status, title=title, body=body)
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback() if False else None  # keep existing savepoint pattern — see note
            return None
        return log.id
```
(Use the file's EXISTING savepoint handling in `_try_log` — do not regress it; just thread `status`/`title`/`body` through.)

Add the renderer (loads the entity, builds short strings; reuse `app.sales.pricing._currency_symbol` for the quote amount):
```python
    def _render_in_app_text(self, event, entity_type, entity_id) -> tuple[str, str]:
        from app.db.models.meeting import Meeting
        from app.db.models.quote import Quote
        if event == NotifEvent.SALES_READY_LEAD:
            lead = self.db.get(Lead, entity_id)
            name = (lead.name if lead else None) or "A lead"
            company = (lead.company if lead else None) or "—"
            score = lead.lead_score if lead else "—"
            return "New sales-ready lead", f"{name} · {company} · score {score}"
        if event == NotifEvent.MEETING_BOOKED:
            m = self.db.get(Meeting, entity_id)
            lead = self.db.get(Lead, m.lead_id) if m else None
            who = (lead.name if lead else None) or (lead.company if lead else None) or "a prospect"
            when = m.scheduled_at.strftime("%a %d %b · %H:%M UTC") if m and m.scheduled_at else ""
            return "Demo booked", f"{who} · {when}".strip(" ·")
        # quote_generated
        from app.sales.pricing import _currency_symbol
        q = self.db.get(Quote, entity_id)
        lead = self.db.get(Lead, q.lead_id) if q else None
        company = (lead.company if lead else None) or "a prospect"
        total = f"{_currency_symbol(q.currency)}{int(q.total or 0):,}" if q else ""
        return "New quote", f"{total} · {company}".strip(" ·")
```

In `dispatch`, after the email block (still inside the method, before the final `self.db.commit()`), add:
```python
        if channels.get("in_app"):
            title, body = self._render_in_app_text(event, entity_type, entity_id)
            self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.IN_APP,
                          entity_type=entity_type, entity_id=entity_id, recipient="in_app",
                          status=NotifStatus.SENT, title=title, body=body)
```
(`dispatch` still returns only the email `jobs` list; the in-app row needs no enqueue. Import `Lead` if not already imported.)

- [ ] **Step 4: Run tests + regression**

Run: `poetry run pytest tests/sales/test_inapp_dispatch.py tests/sales/test_notification_dispatch.py -v`
Expected: new PASS; the 5a dispatch tests (email + idempotency) still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/notification_service.py tests/sales/test_inapp_dispatch.py
git commit -m "feat(notifications): write in-app notification rows on dispatch"
```

---

### Task 3: Read API (list / unread-count / mark-read / mark-all-read)

**Files:**
- Create: `app/api/v1/endpoints/notifications.py`, `app/schemas/notification.py`
- Modify: `app/api/v1/api.py`
- Test: `tests/api/v1/test_notifications_api.py`

**Interfaces:**
- Consumes: `NotificationLog` (in-app rows). Produces the four endpoints under `/notifications`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/v1/test_notifications_api.py
# Authed tenant-admin endpoints. Reuse the auth fixture from existing leads/sales API tests
# (a tenant-admin token + the pg_client/db wiring). Seed in-app NotificationLog rows directly.
#
# Cases (assert on the {success,data} envelope):
#  - GET /notifications → only this tenant's channel='in_app' rows, newest-first.
#  - GET /notifications?unread_only=true → only read_at IS NULL.
#  - GET /notifications/unread-count → count of unread.
#  - PATCH /notifications/{id}/read → read_at set; unread-count decremented; 404 for another tenant's id.
#  - PATCH /notifications/read-all → all unread marked; unread-count → 0.
#  - cross-tenant isolation: tenant B cannot see/mark tenant A's rows.
```

Read an existing authed API test (e.g. `tests/api/v1/test_leads_*` or how `get_current_tenant_admin` is overridden) FIRST and mirror its auth/seed wiring; write the concrete assertions against that.

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/v1/test_notifications_api.py -v`
Expected: FAIL (routes 404).

- [ ] **Step 3: Implement schemas + endpoints**

```python
# app/schemas/notification.py
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel

class NotificationItem(BaseModel):
    id: UUID
    event: str
    title: str | None
    body: str | None
    read_at: datetime | None
    created_at: datetime

class NotificationList(BaseModel):
    items: list[NotificationItem]
    total: int
    limit: int
    offset: int
```

```python
# app/api/v1/endpoints/notifications.py
from uuid import UUID
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.db.models.notification_log import NotificationLog, NotifChannel, NotifStatus
from app.schemas.notification import NotificationItem, NotificationList
from app.schemas.user import UserResponse

router = APIRouter()


def _err(code, message, http):
    return JSONResponse(status_code=http, content={"success": False, "error": {"code": code, "message": message}})


def _base(tenant_id):
    return select(NotificationLog).where(
        NotificationLog.tenant_id == tenant_id, NotificationLog.channel == NotifChannel.IN_APP)


@router.get("")
def list_notifications(unread_only: bool = Query(default=False),
                       limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
                       db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    stmt = _base(user.tenant_id)
    if unread_only:
        stmt = stmt.where(NotificationLog.read_at.is_(None))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(NotificationLog.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    items = [NotificationItem(id=r.id, event=r.event.value, title=r.title, body=r.body,
                              read_at=r.read_at, created_at=r.created_at) for r in rows]
    return {"success": True, "data": NotificationList(items=items, total=total, limit=limit, offset=offset).model_dump()}


@router.get("/unread-count")
def unread_count(db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    stmt = _base(user.tenant_id).where(NotificationLog.read_at.is_(None))
    count = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    return {"success": True, "data": {"count": count}}


@router.patch("/read-all")
def mark_all_read(db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    res = db.execute(update(NotificationLog)
                     .where(NotificationLog.tenant_id == user.tenant_id,
                            NotificationLog.channel == NotifChannel.IN_APP,
                            NotificationLog.read_at.is_(None))
                     .values(read_at=datetime.now(timezone.utc)))
    db.commit()
    return {"success": True, "data": {"updated": res.rowcount or 0}}


@router.patch("/{notification_id}/read")
def mark_read(notification_id: UUID, db: Session = Depends(get_db),
              user: UserResponse = Depends(get_current_tenant_admin)):
    row = db.execute(_base(user.tenant_id).where(NotificationLog.id == notification_id)).scalars().first()
    if row is None:
        return _err("NOTIFICATION_NOT_FOUND", "Not found", 404)
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        db.commit()
    return {"success": True, "data": {"id": str(row.id), "read_at": row.read_at.isoformat() if row.read_at else None}}
```
(Register `/{notification_id}/read` AFTER `/read-all` so the literal route wins — FastAPI matches in declaration order.)

- [ ] **Step 4: Mount the router**

In `app/api/v1/api.py`, mirror the leads include: `from app.api.v1.endpoints import notifications` and `api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])` (use the actual router var name).

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/api/v1/test_notifications_api.py -v`
Expected: PASS (incl. cross-tenant isolation + the 404 case).

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/notifications.py app/schemas/notification.py app/api/v1/api.py tests/api/v1/test_notifications_api.py
git commit -m "feat(notifications): in-app read API (list/unread-count/mark-read)"
```

---

### Task 4: Full-suite verification

- [ ] **Step 1:** `docker start luran_test_db`; `poetry run alembic upgrade head`.
- [ ] **Step 2:** `poetry run pytest -q -p no:cacheprovider --no-cov`. Expected: all green; 5a/5b unaffected.
- [ ] **Step 3:** Commit fixups if any: `git commit -am "test(notifications): in-app suite green"`.

---

## Self-Review notes
- Spec §2 columns → Task 1. §3 in-app dispatch → Task 2. §4 read API → Task 3. §5 invariants (in_app-only, idempotent, tenant scope, read_at-only mutation) → Tasks 2/3. §6 testing → per task + Task 4. §8 acceptance covered.
- Names consistent: `title`/`body`/`read_at`, `recipient="in_app"`, `_render_in_app_text`, the four endpoints, `NotificationItem`/`NotificationList`.
- The implementer MUST: preserve `_try_log`'s EXISTING savepoint/IntegrityError handling while threading `status`/`title`/`body` (Task 2 — do NOT regress to a bare rollback); reuse the real tenant-admin auth fixture + envelope helper from existing API tests (Task 3); confirm the `api.py` router var; register `/read-all` before `/{id}/read`.
