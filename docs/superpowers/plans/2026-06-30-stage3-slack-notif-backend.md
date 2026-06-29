# Stage 3 Slice 5d — Slack Notifications (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post the three rep-alert events to a tenant's Slack via their Incoming Webhook URL (gated by `notify_channels.slack`), delivered through an ARQ job with status-based retry.

**Architecture:** Add `slack_webhook_url` to the config; `dispatch` writes a `slack` `NotificationLog` row + returns a `send_slack_notification` job when enabled+configured (Slack is a network call → ARQ, like email); the job POSTs a Block Kit message via httpx, retrying 5xx/timeout and giving up on 4xx.

**Tech Stack:** FastAPI, SYNC SQLAlchemy, Alembic, ARQ, httpx, pytest.

## Global Constraints

- **Sync** SQLAlchemy. ARQ worker DB via `SessionLocal()` … `finally: db.close()` (mirror existing jobs).
- Slack row/job only when `notify_channels.slack` truthy AND `config.slack_webhook_url` set. `channel=NotifChannel.SLACK`, `recipient="slack"` sentinel (one per `(tenant, event, entity)` via the dedupe index), `status=PENDING`, rendered `title`/`body` (reuse `_render_in_app_text`). Idempotent via the existing `_try_log` savepoint (do NOT regress it).
- Slack delivered via ARQ: `dispatch` APPENDS `(log_id, "send_slack_notification")` to its returned `jobs` (alongside any email jobs). In-app inline write + email job-return UNCHANGED.
- ARQ job retry semantics: 2xx → `sent`; 5xx/timeout/network → `failed` + raise (`_EmailSendFailed` sentinel — reuse it so the outer handler re-raises without double-commit) → ARQ retries; 4xx (dead webhook) → `failed`, NO raise. Already-`SENT` → no-op. Missing URL at send time → `failed` "no webhook".
- Never log the webhook URL or full message body — log status/HTTP code only.
- Lowercase enums. No Claude co-author trailer. Branch `feat/stage3-slack-notif-backend` stacks on `feat/stage3-inapp-notif-backend` (5c) — `NotificationLog`/`NotifChannel.SLACK`/`_render_in_app_text`/`_try_log`/`_EmailSendFailed` already exist.
- Migrations: `poetry run alembic revision -m "..."` + hand-write; `poetry run alembic upgrade head`. Tests: `poetry run pytest <path> -v`, `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db` (`docker start luran_test_db` if 5433 refuses).

---

### Task 1: `slack_webhook_url` config column + migration

**Files:**
- Modify: `app/db/models/sales_agent_config.py`
- Create: `alembic/versions/<generated>_add_slack_webhook_url.py`
- Test: `tests/sales/test_slack_config_column.py`

**Interfaces:**
- Produces: `TenantSalesAgentConfig.slack_webhook_url: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_slack_config_column.py
from uuid import uuid4
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name="T", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_slack_webhook_url_persists_and_defaults_none(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.slack_webhook_url is None
    cfg.slack_webhook_url = "https://hooks.slack.com/services/T/B/x"
    db_session.add(cfg); db_session.flush(); db_session.refresh(cfg)
    assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T/B/x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_slack_config_column.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `app/db/models/sales_agent_config.py`, add the column (after `notification_email` or near the integration fields):
```python
    slack_webhook_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Per-tenant Slack Incoming Webhook URL for sales notifications")
```
Add `slack_webhook_url=None,` to `default_for(...)`'s kwargs. (`String` is already imported.)

- [ ] **Step 4: Migration**

`poetry run alembic revision -m "add slack_webhook_url to sales config"`:
```python
def upgrade() -> None:
    op.add_column("tenant_sales_agent_config",
                  sa.Column("slack_webhook_url", sa.String(length=512), nullable=True))

def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "slack_webhook_url")
```

- [ ] **Step 5: Apply + test**

Run: `poetry run alembic upgrade head` then `poetry run pytest tests/sales/test_slack_config_column.py -v`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db/models/sales_agent_config.py alembic/versions/ tests/sales/test_slack_config_column.py
git commit -m "feat(notifications): slack_webhook_url config column"
```

---

### Task 2: Dispatch — write slack row + return job

**Files:**
- Modify: `app/services/notification_service.py`
- Test: `tests/sales/test_slack_dispatch.py`

**Interfaces:**
- Consumes: `_render_in_app_text`, `_try_log` (extended in 5c with status/title/body), `NotifChannel.SLACK`.
- Produces: `dispatch` appends `(log_id, "send_slack_notification")` when slack enabled+configured.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_slack_dispatch.py
from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, slack=True, url="https://hooks.slack.com/services/T/B/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": False, "slack": slack, "in_app": False}
    cfg.slack_webhook_url = url
    db.add(cfg); db.commit()
    return t


def _quote(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000, status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _slack_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.SLACK).all()


def test_dispatch_slack_writes_row_and_returns_job(db_session):
    t = _cfg(db_session); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert ("send_slack_notification" in [j[1] for j in jobs])
    rows = _slack_rows(db_session, t)
    assert len(rows) == 1 and rows[0].status == NotifStatus.PENDING and rows[0].recipient == "slack"
    assert rows[0].title and rows[0].body


def test_dispatch_slack_off(db_session):
    t = _cfg(db_session, slack=False); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "send_slack_notification" not in [j[1] for j in jobs]
    assert _slack_rows(db_session, t) == []


def test_dispatch_slack_no_url(db_session):
    t = _cfg(db_session, url=None); q = _quote(db_session, t)
    jobs = NotificationService(db_session).dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "send_slack_notification" not in [j[1] for j in jobs]
    assert _slack_rows(db_session, t) == []


def test_dispatch_slack_idempotent(db_session):
    t = _cfg(db_session); q = _quote(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    svc.dispatch(t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert len(_slack_rows(db_session, t)) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_slack_dispatch.py -v`
Expected: FAIL (no slack row/job).

- [ ] **Step 3: Implement**

In `dispatch` (after the in-app block, before the final `commit`), add:
```python
        if channels.get("slack") and getattr(config, "slack_webhook_url", None):
            title, body = self._render_in_app_text(event, entity_type, entity_id)
            log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.SLACK,
                                   entity_type=entity_type, entity_id=entity_id, recipient="slack",
                                   status=NotifStatus.PENDING, title=title, body=body)
            if log_id is not None:
                jobs.append((log_id, "send_slack_notification"))
```
(`config` is already loaded at the top of `dispatch`. The in-app block renders title/body too — if you want to avoid rendering twice when both in_app and slack are on, render once and reuse; not required.)

- [ ] **Step 4: Run tests + regression**

Run: `poetry run pytest tests/sales/test_slack_dispatch.py tests/sales/test_inapp_dispatch.py tests/sales/test_notification_dispatch.py -v`
Expected: new PASS; in-app + 5a dispatch tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/notification_service.py tests/sales/test_slack_dispatch.py
git commit -m "feat(notifications): dispatch Slack notification job"
```

---

### Task 3: Slack ARQ job (httpx POST + status-based retry)

**Files:**
- Modify: `app/workers/sales_notifications.py`, `app/workers/document_processor.py` (register)
- Test: `tests/workers/test_slack_notification.py`

**Interfaces:**
- Produces: `async def send_slack_notification(ctx, notification_log_id: str)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_slack_notification.py
from uuid import uuid4
from unittest.mock import patch, MagicMock

import pytest

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_slack_notification


def _seed(db, *, url="https://hooks.slack.com/services/T/B/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id); cfg.slack_webhook_url = url
    db.add(cfg)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme"); db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000, status=QuoteStatus.DRAFT)
    db.add(q); db.flush()
    log = NotificationLog(tenant_id=t.id, event=NotifEvent.QUOTE_GENERATED, channel=NotifChannel.SLACK,
                          entity_type="quote", entity_id=q.id, recipient="slack", status=NotifStatus.PENDING,
                          title="New quote", body="₦45,000 · Acme")
    db.add(log); db.commit()
    return log.id


def _resp(code):
    r = MagicMock(); r.status_code = code; return r


async def test_slack_2xx_marks_sent(db_session, monkeypatch):
    log_id = _seed(db_session)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    with patch("httpx.Client.post", return_value=_resp(200)) as post:
        await send_slack_notification({}, str(log_id))
    post.assert_called_once()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_slack_5xx_raises_for_retry(db_session, monkeypatch):
    log_id = _seed(db_session)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    with patch("httpx.Client.post", return_value=_resp(503)):
        with pytest.raises(Exception):
            await send_slack_notification({}, str(log_id))
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_slack_4xx_no_retry(db_session, monkeypatch):
    log_id = _seed(db_session)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    with patch("httpx.Client.post", return_value=_resp(404)):
        await send_slack_notification({}, str(log_id))   # must NOT raise
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_slack_missing_url_marks_failed(db_session, monkeypatch):
    log_id = _seed(db_session, url=None)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    await send_slack_notification({}, str(log_id))
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED
```

(Adapt the `SessionLocal` monkeypatch + async style to the existing `tests/workers/test_sales_notifications.py` conventions.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/workers/test_slack_notification.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# app/workers/sales_notifications.py  (add)
import httpx
from app.db.models.sales_agent_config import TenantSalesAgentConfig


def _slack_blocks(title: str | None, body: str | None) -> dict:
    return {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": (title or "Sales notification")[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (body or "")[:2000] or "—"}},
    ]}


async def send_slack_notification(ctx, notification_log_id: str) -> None:
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return
        cfg = db.execute(
            select(TenantSalesAgentConfig).where(TenantSalesAgentConfig.tenant_id == log.tenant_id)
        ).scalar_one_or_none()
        url = cfg.slack_webhook_url if cfg else None
        if not url:
            log.status = NotifStatus.FAILED; log.error = "no slack webhook"; db.commit(); return
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=_slack_blocks(log.title, log.body))
        except Exception as exc:  # network/timeout → transient → retry
            log.status = NotifStatus.FAILED; log.error = f"slack network: {type(exc).__name__}"; db.commit()
            raise _EmailSendFailed("slack network error") from exc
        if 200 <= resp.status_code < 300:
            log.status = NotifStatus.SENT; db.commit(); return
        if resp.status_code >= 500:
            log.status = NotifStatus.FAILED; log.error = f"slack {resp.status_code}"; db.commit()
            raise _EmailSendFailed(f"slack {resp.status_code}")          # 5xx → retry
        log.status = NotifStatus.FAILED; log.error = f"slack {resp.status_code}"; db.commit()  # 4xx → no retry
    except _EmailSendFailed:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(f"send_slack_notification failed: {exc}")
        raise
    finally:
        db.close()
```
(Add `from sqlalchemy import select` if not already imported in the file. `_EmailSendFailed` + `logger` already exist in this module from 5a/5b. Do NOT log `url`.)

- [ ] **Step 4: Register + run tests**

In `app/workers/document_processor.py`, import `send_slack_notification` and add it to `WorkerSettings.functions`. Run:
`poetry run pytest tests/workers/test_slack_notification.py tests/workers/test_sales_notifications.py -v`
Expected: new PASS; existing worker tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/workers/sales_notifications.py app/workers/document_processor.py tests/workers/test_slack_notification.py
git commit -m "feat(notifications): Slack ARQ job (Block Kit + status-based retry)"
```

---

### Task 4: Full-suite verification

- [ ] **Step 1:** `docker start luran_test_db`; `poetry run alembic upgrade head`.
- [ ] **Step 2:** `poetry run pytest -q -p no:cacheprovider --no-cov`. Expected: all green; 5a/5b/5c unaffected.
- [ ] **Step 3:** Commit fixups if any: `git commit -am "test(notifications): slack suite green"`.

---

## Self-Review notes
- Spec §2 config → Task 1. §3 dispatch → Task 2. §4 ARQ job → Task 3. §5 invariants (gated, idempotent, no-url-skip, status-based retry, no-url-logging) → Tasks 2/3. §6 testing → per task + Task 4. §8 acceptance covered.
- Names consistent: `slack_webhook_url`, `recipient="slack"`, `send_slack_notification`, `_slack_blocks`, `_EmailSendFailed` retry.
- The implementer MUST: preserve `_try_log`'s savepoint (Task 2); reuse the `_EmailSendFailed` sentinel + the existing outer-handler pattern so the 5xx-retry path doesn't double-commit (Task 3); adapt the worker-test monkeypatch to the existing `test_sales_notifications.py` style; NEVER log the webhook URL.
