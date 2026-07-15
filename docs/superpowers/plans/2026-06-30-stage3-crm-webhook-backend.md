# Stage 3 Slice 5e — CRM Webhook (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a lead crosses sales-ready, POST its data to the tenant's configured CRM webhook URL via an ARQ job with status-based retry and best-effort `crm_id` write-back.

**Architecture:** Adds a generic outbound CRM webhook as a fourth delivery channel on the existing notification dispatch spine. A new encrypted `crm_webhook_url` config column + a `notify_channels.crm` toggle gate dispatch; `dispatch` writes a `crm` `NotificationLog` row and returns a `sync_lead_to_crm` job — but only on `SALES_READY_LEAD`. The ARQ job mirrors the 5d Slack job exactly (httpx POST, 2xx→SENT, 5xx/network→FAILED+raise for retry, 4xx→FAILED no retry), adding a best-effort `Lead.crm_id` write-back from the 2xx response JSON. The webhook URL and the PII payload are never logged.

**Tech Stack:** FastAPI, SYNC SQLAlchemy, Alembic, ARQ/Redis, httpx, Fernet `EncryptedString`, pytest.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-30-stage3-crm-webhook-backend-design.md`. PRD: decision-log #1 line 1581 ("Webhook-first — covers all CRMs, then add native HubSpot"); §2.3 line 287; §3.6.
- **Generic webhook only** — no native HubSpot/Zoho/Salesforce API client, no action-call engine.
- **Trigger is `NotifEvent.SALES_READY_LEAD` only.** Meeting/quote dispatches must never produce a crm row or job, even when the crm channel is enabled.
- **`crm_webhook_url` stored ENCRYPTED at rest** via the existing `app.core.encryption.EncryptedString` TypeDecorator (Fernet, impl `Text`). It decrypts transparently on read, so `/sales/config` returns plaintext.
- **Gate:** crm row/job only when `event == SALES_READY_LEAD` AND `channels.get("crm")` AND `config.crm_webhook_url` is set. Idempotent (savepoint) + tenant-scoped via the existing dedupe index.
- **NEVER log the webhook URL or the payload** (the payload carries decrypted lead PII). Log status/HTTP-code only.
- **SSRF guard:** before POSTing, validate the URL with `app.utils.network_security.ensure_safe_outbound_url` (reuse — same guard the action-call `flow_engine`/`url_scraper` paths use). `ValueError` → mark `FAILED` "blocked webhook host", return (no raise). Added in Task 3 (security hardening, flagged by automated commit review).
- `NotifChannel.CRM` already exists (`app/db/models/notification_log.py:23`). `Lead.crm_id` already exists (`app/db/models/lead.py:49`, `String(255)`).
- 5a–5d behavior must be unchanged. No Claude co-author trailer on commits.
- Branch `feat/stage3-crm-webhook-backend` stacks on `feat/stage3-slack-notif-backend` (Slice 5d PR #39). Already created; HEAD is the spec commit.
- **Tests run on the test DB:** `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db` (port 5433). If pytest cannot connect, run `docker start luran_test_db` first. `ENCRYPTION_KEY` must be present in the test env (it already is — `Lead.name`/`Lead.email` are encrypted and their tests pass).

---

### Task 1: Encrypted `crm_webhook_url` config column + `notify_channels.crm` default + migration

**Files:**
- Modify: `app/db/models/sales_agent_config.py`
- Create: `alembic/versions/<rev>_add_crm_webhook_url_to_sales_config.py`
- Test: `tests/sales/test_crm_webhook_encrypted.py`

**Interfaces:**
- Consumes: `app.core.encryption.EncryptedString`; `TenantSalesAgentConfig.default_for(tenant_id)`.
- Produces: `TenantSalesAgentConfig.crm_webhook_url: Mapped[str | None]` (EncryptedString); `_DEFAULT_NOTIFY_CHANNELS` gains `"crm": False`.

- [ ] **Step 1: Write the failing encryption round-trip test**

Create `tests/sales/test_crm_webhook_encrypted.py`:

```python
"""crm_webhook_url is encrypted at rest and decrypts transparently (Slice 5e Task 1)."""
from uuid import uuid4

from sqlalchemy import text

from app.db.models.tenant import Tenant
from app.db.models.sales_agent_config import TenantSalesAgentConfig


def _tenant(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    return t


def test_crm_webhook_url_roundtrips_plaintext(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = "https://hooks.zapier.com/hooks/catch/123/abc?token=secret"
    db_session.add(cfg); db_session.commit()
    db_session.expire_all()
    reloaded = db_session.get(TenantSalesAgentConfig, cfg.id)
    assert reloaded.crm_webhook_url == "https://hooks.zapier.com/hooks/catch/123/abc?token=secret"


def test_crm_webhook_url_ciphertext_in_raw_db(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = "https://hooks.zapier.com/PLAINTEXT-MARKER"
    db_session.add(cfg); db_session.commit()
    raw = db_session.execute(
        text("SELECT crm_webhook_url FROM tenant_sales_agent_config WHERE id = :id"),
        {"id": str(cfg.id)},
    ).scalar_one()
    assert raw is not None
    assert "PLAINTEXT-MARKER" not in raw  # stored as Fernet ciphertext


def test_default_for_sets_crm_webhook_url_none_and_channel_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.crm_webhook_url is None
    assert cfg.notify_channels.get("crm") is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/sales/test_crm_webhook_encrypted.py -v`
Expected: FAIL — `crm_webhook_url` attribute / column does not exist (and `notify_channels` has no `"crm"` key).

- [ ] **Step 3: Add the encrypted column, the channel default, and the `default_for` entry**

In `app/db/models/sales_agent_config.py`:

Add the import at the top (next to the existing imports):
```python
from app.core.encryption import EncryptedString
```

Update the notify-channels default (line ~23) to include `crm`:
```python
_DEFAULT_NOTIFY_CHANNELS: dict = {"email": True, "slack": False, "in_app": False, "crm": False}
```

Add the column directly after the existing `slack_webhook_url` column (after line 102):
```python
    crm_webhook_url: Mapped[str | None] = mapped_column(
        EncryptedString,
        nullable=True,
        comment="Per-tenant CRM/Zapier/Make webhook URL (encrypted at rest) for sales-ready lead push",
    )
```

In `default_for` (after the `slack_webhook_url=None,` line ~155), add:
```python
            crm_webhook_url=None,
```

- [ ] **Step 4: Create the Alembic migration**

Create `alembic/versions/<rev>_add_crm_webhook_url_to_sales_config.py` (use a fresh revision id; `down_revision = "6a109f12a6b0"` — the slack migration is the current head). The column is `Text` because `EncryptedString.impl` is `Text`:

```python
"""add crm_webhook_url to sales config

Revision ID: <rev>
Revises: 6a109f12a6b0
Create Date: 2026-06-30

"""
from alembic import op
import sqlalchemy as sa


revision = "<rev>"
down_revision = "6a109f12a6b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_sales_agent_config",
                  sa.Column("crm_webhook_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "crm_webhook_url")
```

Generate the revision id with `poetry run alembic revision -m "add crm_webhook_url to sales config"` (then paste the upgrade/downgrade bodies above into the generated file, keeping its auto-filled `revision`/`down_revision` — confirm `down_revision` is `6a109f12a6b0`), OR hand-author the file with any unique 12-hex revision id. Do NOT use `--autogenerate`.

Note: the test DB on 5433 is migrated; the dev DB on 5432 is bootstrapped via `create_all` (not under alembic). For the test DB to pick up the column, apply the migration: `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db poetry run alembic upgrade head` (or, if the test fixtures `create_all` from models, the column appears automatically — run the tests in Step 5 and only run `alembic upgrade head` if they fail on a missing column).

- [ ] **Step 5: Run the test to verify it passes**

Run: `poetry run pytest tests/sales/test_crm_webhook_encrypted.py -v`
Expected: PASS (all three tests).

- [ ] **Step 6: Commit**

```bash
git add app/db/models/sales_agent_config.py alembic/versions/*crm_webhook_url* tests/sales/test_crm_webhook_encrypted.py
git commit -m "feat(notifications): encrypted crm_webhook_url config column + crm channel default"
```

---

### Task 2: Dispatch — write a `crm` row + return `sync_lead_to_crm` (sales-ready only)

**Files:**
- Modify: `app/services/notification_service.py`
- Test: `tests/sales/test_crm_dispatch.py`

**Interfaces:**
- Consumes: `NotificationService.dispatch(tenant_id, event, *, entity_type, entity_id) -> list[tuple[uuid, str]]`; `_try_log(...)`; `_render_in_app_text(event, entity_type, entity_id)`; `NotifChannel.CRM`; `config.crm_webhook_url` (Task 1).
- Produces: dispatch appends `(log_id, "sync_lead_to_crm")` to its returned jobs when the crm gate passes.

- [ ] **Step 1: Write the failing dispatch tests**

Create `tests/sales/test_crm_dispatch.py`:

```python
"""Tests for CRM channel dispatch (Stage 3 Slice 5e Task 2)."""
from uuid import uuid4

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.services.notification_service import NotificationService


def _cfg(db, *, crm=True, url="https://hooks.zapier.com/hooks/catch/1/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.notify_channels = {"email": False, "slack": False, "in_app": False, "crm": crm}
    cfg.crm_webhook_url = url
    db.add(cfg); db.commit()
    return t


def _lead(db, t):
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), name="Dana", email="d@co.com",
                company="Acme", lead_score=90)
    db.add(lead); db.flush(); db.refresh(lead)
    return lead


def _quote(db, t, lead):
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000,
              status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q


def _crm_rows(db, t):
    return db.query(NotificationLog).filter_by(tenant_id=t.id, channel=NotifChannel.CRM).all()


def test_dispatch_crm_writes_row_and_returns_job_on_sales_ready(db_session):
    t = _cfg(db_session); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" in [j[1] for j in jobs]
    rows = _crm_rows(db_session, t)
    assert len(rows) == 1
    assert rows[0].status == NotifStatus.PENDING
    assert rows[0].recipient == "crm"
    assert rows[0].title and rows[0].body


def test_dispatch_crm_off(db_session):
    t = _cfg(db_session, crm=False); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_no_url(db_session):
    t = _cfg(db_session, url=None); lead = _lead(db_session, t)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_not_sales_ready_event(db_session):
    """quote_generated must NOT produce a crm row/job even with crm enabled."""
    t = _cfg(db_session); lead = _lead(db_session, t); q = _quote(db_session, t, lead)
    jobs = NotificationService(db_session).dispatch(
        t.id, NotifEvent.QUOTE_GENERATED, entity_type="quote", entity_id=q.id)
    assert "sync_lead_to_crm" not in [j[1] for j in jobs]
    assert _crm_rows(db_session, t) == []


def test_dispatch_crm_idempotent(db_session):
    t = _cfg(db_session); lead = _lead(db_session, t)
    svc = NotificationService(db_session)
    svc.dispatch(t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    svc.dispatch(t.id, NotifEvent.SALES_READY_LEAD, entity_type="lead", entity_id=lead.id)
    assert len(_crm_rows(db_session, t)) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/sales/test_crm_dispatch.py -v`
Expected: FAIL — no crm row written, `sync_lead_to_crm` never returned.

- [ ] **Step 3: Add the crm gate to `dispatch`**

In `app/services/notification_service.py`, add this block immediately after the existing `slack` block (after line 130, before the `self.db.commit()` comment block):

```python
        if (event == NotifEvent.SALES_READY_LEAD
                and channels.get("crm")
                and getattr(config, "crm_webhook_url", None)):
            title, body = self._render_in_app_text(event, entity_type, entity_id)
            log_id = self._try_log(tenant_id=tenant_id, event=event, channel=NotifChannel.CRM,
                                   entity_type=entity_type, entity_id=entity_id, recipient="crm",
                                   status=NotifStatus.PENDING, title=title, body=body)
            if log_id is not None:
                jobs.append((log_id, "sync_lead_to_crm"))
```

(The `event == NotifEvent.SALES_READY_LEAD` guard is what scopes the channel to sales-ready only — this is the single behavioral difference from the slack block above it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/sales/test_crm_dispatch.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Run the full dispatch suite to confirm 5a–5d unaffected**

Run: `poetry run pytest tests/sales/test_notification_dispatch.py tests/sales/test_slack_dispatch.py tests/sales/test_inapp_dispatch.py -v`
Expected: PASS (existing behavior unchanged).

- [ ] **Step 6: Commit**

```bash
git add app/services/notification_service.py tests/sales/test_crm_dispatch.py
git commit -m "feat(notifications): dispatch crm webhook row + job on sales-ready"
```

---

### Task 3: `sync_lead_to_crm` ARQ job + registration

**Files:**
- Modify: `app/workers/sales_notifications.py`, `app/workers/document_processor.py`
- Test: `tests/workers/test_crm_sync.py`

**Interfaces:**
- Consumes: `SessionLocal`, `NotificationLog`, `NotifStatus`, `Lead`, `TenantSalesAgentConfig`, `_EmailSendFailed`, `httpx` (all already imported in `sales_notifications.py`); `settings`.
- Produces: `async def sync_lead_to_crm(ctx, notification_log_id: str) -> None`, registered in `WorkerSettings.functions`.

- [ ] **Step 1: Write the failing job tests**

Create `tests/workers/test_crm_sync.py`:

```python
"""Tests for the CRM webhook ARQ job (Stage 3 Slice 5e Task 3)."""
import pytest
from uuid import uuid4
from unittest.mock import patch, MagicMock

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import sync_lead_to_crm


class _NoCloseSession:
    """Proxy that swallows close() so the worker's finally block does not close the test session."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _seed(db, *, url="https://hooks.zapier.com/hooks/catch/1/x"):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    cfg.crm_webhook_url = url
    db.add(cfg)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), name="Dana", email="d@co.com",
                company="Acme", team_size="11-50", industry="SaaS", intent="DEMO_REQUEST",
                lead_score=90, source="chat")
    db.add(lead); db.flush()
    log = NotificationLog(tenant_id=t.id, event=NotifEvent.SALES_READY_LEAD,
                          channel=NotifChannel.CRM, entity_type="lead", entity_id=lead.id,
                          recipient="crm", status=NotifStatus.PENDING,
                          title="New sales-ready lead", body="Dana · Acme · score 90")
    db.add(log); db.commit()
    return log.id, lead.id


def _resp(code, json_body=None):
    r = MagicMock()
    r.status_code = code
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_body
    return r


async def test_crm_2xx_marks_sent_and_posts_lead_payload(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200)) as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_called_once()
    call = post.call_args
    url = call[0][0] if call[0] else call[1]["url"]
    payload = call[1]["json"]
    assert url == "https://hooks.zapier.com/hooks/catch/1/x"
    assert payload["email"] == "d@co.com"
    assert payload["company"] == "Acme"
    assert payload["lead_score"] == 90
    assert payload["lead_id"] == str(lead_id)
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT


async def test_crm_2xx_with_id_writes_back_crm_id(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200, {"id": "crm-789"})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id == "crm-789"


async def test_crm_2xx_without_id_leaves_crm_id_none(db_session, monkeypatch):
    log_id, lead_id = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(200, {"ok": True})):
        await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
    assert db_session.get(Lead, lead_id).crm_id is None


async def test_crm_5xx_raises_for_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(503)):
        with pytest.raises(Exception):
            await sync_lead_to_crm({}, str(log_id))

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_4xx_no_retry(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post", return_value=_resp(404)):
        await sync_lead_to_crm({}, str(log_id))  # must NOT raise

    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_missing_url_marks_failed(db_session, monkeypatch):
    log_id, _ = _seed(db_session, url=None)
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.FAILED


async def test_crm_already_sent_is_noop(db_session, monkeypatch):
    log_id, _ = _seed(db_session)
    db_session.get(NotificationLog, log_id).status = NotifStatus.SENT
    db_session.commit()
    import app.workers.sales_notifications as notif_mod
    monkeypatch.setattr(notif_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    with patch("httpx.Client.post") as post:
        await sync_lead_to_crm({}, str(log_id))

    post.assert_not_called()
    assert db_session.get(NotificationLog, log_id).status == NotifStatus.SENT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/workers/test_crm_sync.py -v`
Expected: FAIL — `cannot import name 'sync_lead_to_crm'`.

- [ ] **Step 3: Implement `sync_lead_to_crm`**

In `app/workers/sales_notifications.py`, append at the end of the file (after `send_slack_notification`). All imports it needs (`httpx`, `select`, `SessionLocal`, `Lead`, `TenantSalesAgentConfig`, `NotificationLog`, `NotifStatus`, `_EmailSendFailed`, `logger`) are already present:

```python
# ---------------------------------------------------------------------------
# CRM webhook delivery (Stage 3 Slice 5e)
# ---------------------------------------------------------------------------

_CRM_ID_KEYS = ("id", "crm_id", "contactId", "vid")


def _crm_payload(lead: Lead) -> dict:
    """Build the outbound lead payload. Contains decrypted PII — never log this."""
    return {
        "lead_id": str(lead.id),
        "tenant_id": str(lead.tenant_id) if lead.tenant_id else None,
        "name": lead.name,
        "email": lead.email,
        "company": lead.company,
        "team_size": lead.team_size,
        "industry": lead.industry,
        "intent": lead.intent,
        "lead_score": lead.lead_score,
        "status": lead.status.value if lead.status else None,
        "source": lead.source,
        "utm": lead.utm,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _extract_crm_id(resp) -> str | None:
    """Best-effort: pull an id-ish value from a 2xx JSON body. Never raises."""
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _CRM_ID_KEYS:
        if data.get(key):
            return str(data[key])
    return None


async def sync_lead_to_crm(ctx, notification_log_id: str) -> None:
    """POST a sales-ready lead payload to the tenant's CRM webhook.

    Idempotent: exits early if the log is missing or already SENT.
    Status-based retry (mirrors send_slack_notification):
      - 2xx  → SENT (best-effort Lead.crm_id write-back from the response JSON).
      - 5xx or network/timeout → FAILED + raise _EmailSendFailed (ARQ retries).
      - 4xx  → FAILED, return (permanent client error, no retry).
      - no webhook URL configured → FAILED, return (no raise).

    The webhook URL and the PII payload are NEVER written to the log.
    """
    db = SessionLocal()
    try:
        log = db.get(NotificationLog, uuid.UUID(notification_log_id))
        if log is None or log.status == NotifStatus.SENT:
            return

        cfg = db.execute(
            select(TenantSalesAgentConfig).where(
                TenantSalesAgentConfig.tenant_id == log.tenant_id
            )
        ).scalar_one_or_none()
        url = cfg.crm_webhook_url if cfg else None

        if not url:
            log.status = NotifStatus.FAILED
            log.error = "no crm webhook configured"
            db.commit()
            return

        lead = db.get(Lead, log.entity_id)
        if lead is None:
            log.status = NotifStatus.FAILED
            log.error = "lead not found"
            db.commit()
            return

        payload = _crm_payload(lead)
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=payload)
        except Exception as exc:  # network / timeout — transient, retry
            log.status = NotifStatus.FAILED
            log.error = f"crm network error: {type(exc).__name__}"
            db.commit()
            raise _EmailSendFailed("crm network error") from exc

        if 200 <= resp.status_code < 300:
            crm_id = _extract_crm_id(resp)
            if crm_id:
                lead.crm_id = crm_id
            log.status = NotifStatus.SENT
            db.commit()
            return

        if resp.status_code >= 500:
            log.status = NotifStatus.FAILED
            log.error = f"crm {resp.status_code}"
            db.commit()
            raise _EmailSendFailed(f"crm {resp.status_code}")  # 5xx → ARQ retry

        # 4xx — permanent client error, mark failed, do not retry
        log.status = NotifStatus.FAILED
        log.error = f"crm {resp.status_code}"
        db.commit()

    except _EmailSendFailed:
        raise  # already persisted FAILED; skip fallback
    except Exception as exc:  # noqa: BLE001
        logger.error(f"sync_lead_to_crm failed: {exc}")
        raise
    finally:
        db.close()
```

(Note the module docstring on line 1 mentions only "emails and Slack messages" — update it to "emails, Slack messages, and CRM webhooks" while you are in the file.)

- [ ] **Step 4: Register the job**

In `app/workers/document_processor.py`, add `sync_lead_to_crm` to the `WorkerSettings.functions` list (alongside `send_slack_notification`). Add it to the import where `send_slack_notification` / `send_rep_alert` are imported from `app.workers.sales_notifications`.

Find the import (grep `from app.workers.sales_notifications import`) and add `sync_lead_to_crm`. Then add the function to the list at line ~1209:
```python
        send_slack_notification,
        sync_lead_to_crm,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `poetry run pytest tests/workers/test_crm_sync.py -v`
Expected: PASS (all seven tests).

- [ ] **Step 6: Verify the job is registered**

Run: `poetry run python -c "from app.workers.document_processor import WorkerSettings; print('sync_lead_to_crm' in [f.__name__ for f in WorkerSettings.functions])"`
Expected: `True`

- [ ] **Step 7: Commit**

```bash
git add app/workers/sales_notifications.py app/workers/document_processor.py tests/workers/test_crm_sync.py
git commit -m "feat(notifications): sync_lead_to_crm ARQ job with retry + crm_id write-back"
```

---

### Task 4: Full-suite verification

- [ ] **Step 1:** Run the whole sales + workers notification suite.

Run: `poetry run pytest tests/sales/ tests/workers/test_crm_sync.py tests/workers/test_slack_notification.py -v`
Expected: PASS — all new 5e tests green AND all 5a–5d tests green (no regressions).

- [ ] **Step 2:** If any pre-existing test fails, investigate before proceeding (5a–5d behavior must be unchanged). Commit any fixups with a clear message.

---

## Self-Review notes

- **Spec coverage:** §2 Config → Task 1. §3 Dispatch (sales-ready-only gate, recipient "crm", idempotent) → Task 2. §4 ARQ job (payload, 2xx/5xx/4xx mapping, crm_id write-back, missing-url, already-sent, never-log) → Task 3. §5 safety invariants → enforced across Tasks 2–3. §6 testing → the three test files + Task 4. §8 acceptance covered.
- **Type/name consistency:** `crm_webhook_url` (EncryptedString), `notify_channels.crm`, `NotifChannel.CRM`, job name `"sync_lead_to_crm"`, recipient sentinel `"crm"`, `Lead.crm_id` — all consistent across tasks and with the slack precedent.
- **The implementer MUST:** (1) keep the `event == NotifEvent.SALES_READY_LEAD` guard in the dispatch gate — it is the only thing scoping crm to sales-ready; (2) NEVER add the URL or payload to `log.error` or any logger call; (3) mirror the slack job's outer-handler structure exactly (re-raise `_EmailSendFailed` first, `finally: db.close()`); (4) confirm `down_revision = "6a109f12a6b0"` in the migration.
