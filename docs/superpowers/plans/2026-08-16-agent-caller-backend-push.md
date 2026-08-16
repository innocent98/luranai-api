# Agent Caller — Backend Escalation Push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Luran's AI escalates a conversation, ring every available agent's phone within seconds and let the first to tap "Take Over" claim the live text conversation — supplementing the existing email escalation.

**Architecture:** Bolt a push layer onto the existing escalation path. A new `agent_devices` table holds FCM registration tokens. Escalation enqueues two idempotent ARQ jobs: `alert_escalation` (ring the broadcast tier now) and `alert_escalation_fallback` (deferred by `AGENT_ALERT_WAIT_SECONDS`, rings supervisors only if still unclaimed). Claiming is a single atomic conditional `UPDATE` on `tickets.assigned_to` — the unique write *is* the coordination. A new `/api/v1/agent/*` router serves the mobile app. The live takeover reuses the existing `/api/v1/ws/agent/{session_id}` WebSocket unchanged.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 + Alembic, ARQ (Redis), FCM HTTP v1 over `httpx`, OAuth2 JWT-bearer assertion signed with `python-jose` (no new dependency).

**Spec:** `docs/superpowers/specs/2026-08-15-agent-caller-backend-escalation-push-design.md` (on branch `docs/agent-caller-spec`; read it with `git show docs/agent-caller-spec:docs/superpowers/specs/2026-08-15-agent-caller-backend-escalation-push-design.md`). It is the **source of truth for the shared contract** that the Flutter app consumes.

**Branch:** `feat/agent-caller-push`, cut from `develop`.

---

## Global Constraints

- **API version:** additive only — everything stays on `/api/v1`. New router mounted at `/api/v1/agent`. No breaking changes to existing endpoints.
- **Multi-tenant isolation:** every `/agent/*` query is filtered by `current_agent.tenant_id`. A ticket id belonging to another tenant returns **404**, never 403 (no existence leak).
- **Agent identity:** an "agent" is a `User` with role `TENANT_ADMIN` or `SUPPORT_AGENT`. Auth is the existing JWT from `POST /api/v1/auth/login`, accepted via the `Authorization: Bearer` header (`app/api/deps.py:extract_token` already falls back to the header when no cookie is present).
- **Respect silent mode.** No Apple Critical Alerts, no CallKit/PushKit. Notifications honor the device ringer and DND.
- **Reuse `Ticket`** as the escalation record. Do **not** introduce a parallel `Escalation` table.
- **Kill switch:** `AGENT_ALERT_ENABLED: bool = True`. When false, no push job is ever enqueued and the email backstop is unaffected.
- **Commits:** conventional-commit messages, **no Claude co-author trailer**.
- **Formatting:** `black` and `ruff` both at `line-length = 100`. Lint gate is `ruff check app tests` **and** `black --check app tests`.
- **Enum storage convention (critical):** `TicketStatus` is mapped as `SQLEnum(TicketStatus, name="ticket_status")` **without** `values_callable`, so Postgres stores the member **NAMES** (`OPEN`, `IN_PROGRESS`, …). `NotifChannel` / `NotifEvent` **do** use `values_callable`, so they store lowercase **values**. Migrations must match each case exactly.
- **Timestamps:** use `app.utils.datetime.utcnow_naive()` for ticket-side timestamps, matching the existing `resolved_at` convention in `app/services/ticket_service.py:275`.

---

## Contract Addendum (authoritative — the Flutter plan consumes this verbatim)

The spec's frozen contract (§4) has two gaps the app cannot fill from existing endpoints. These are **additive** and become part of the contract:

**A1 — `GET /api/v1/agent/me`.** The Home screen needs the tenant's display name and the agent's current availability; `GET /api/v1/auth/me` returns neither. Response:
```json
{
  "user_id": "uuid", "email": "sam@acme.com", "full_name": "Sam R.",
  "role": "support_agent", "tenant_id": "uuid", "tenant_name": "Acme Insurance",
  "availability_status": "available"
}
```

**A2 — `ws_url` shape.** `acknowledge` returns an absolute URL derived from `settings.SERVER_HOST` with the scheme swapped (`https`→`wss`, `http`→`ws`):
`wss://api.luranai.com/api/v1/ws/agent/<session_id>`. The app appends `?token=<jwt>` itself.

**A3 — internal `NotifEvent` values** (never on the wire, for `NotificationLog` observability + dedupe): `escalation_alert`, `escalation_fallback`, `escalation_missed`. Three distinct values are required, not one: the dedupe index is `(tenant_id, event, channel, entity_id, recipient)`, and an available `TENANT_ADMIN` is in **both** the broadcast tier and the supervisor tier — a single event value would silently swallow their fallback ring.

**A4 — `priority` mapping.** `TicketPriority` → wire `priority`: `URGENT` → `critical`, `HIGH` → `important`, `MEDIUM`/`LOW` → `normal`.

**A5 — `status` derivation** for `GET /agent/escalations`: `assigned_to IS NULL and status == OPEN` → `pending`; `status == IN_PROGRESS` → `in_progress`; `status in (RESOLVED, CLOSED)` → `resolved`; `status == MISSED` → `missed`.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/db/models/agent_device.py` (create) | `AgentDevice` model + `DevicePlatform` enum |
| `app/db/models/user.py` (modify) | `AvailabilityStatus` enum + `users.availability_status` column |
| `app/db/models/conversation.py` (modify) | `TicketStatus.MISSED` + `Ticket.acknowledged_at` |
| `app/db/models/notification_log.py` (modify) | `NotifChannel.PUSH` + three escalation `NotifEvent` values |
| `app/db/models/__init__.py` (modify) | Export the new model/enums |
| `alembic/versions/ac30_agent_caller_push.py` (create) | One migration for all schema changes |
| `app/core/config.py` (modify) | `FIREBASE_CREDENTIALS_JSON`, `AGENT_ALERT_*` settings |
| `app/services/fcm_client.py` (create) | FCM HTTP v1 transport: token minting, one-message send, `UNREGISTERED` detection |
| `app/services/agent_alert_service.py` (create) | Recipient tiers, payload building, `NotificationLog` writes, stale-token pruning |
| `app/services/notification_enqueue.py` (modify) | ARQ enqueue helpers for the ring, the deferred fallback, and the clear |
| `app/workers/agent_alerts.py` (create) | ARQ jobs `alert_escalation`, `alert_escalation_fallback`, `clear_agent_alert` |
| `app/workers/document_processor.py` (modify) | Register the three jobs in `WorkerSettings` |
| `app/schemas/agent_app.py` (create) | Request/response models for the agent router |
| `app/api/deps.py` (modify) | `get_current_agent` dependency |
| `app/api/v1/endpoints/agent_app.py` (create) | The `/api/v1/agent` router |
| `app/api/v1/api.py` (modify) | Mount the router |
| `app/api/v1/endpoints/chat.py` (modify) | Fire the ring at the six escalation ticket-creation sites |
| `tests/agent_caller/*` (create) | All tests for this feature |

`tests/agent_caller/` is a new package (`__init__.py` required — sibling test packages all have one).

---

### Task 1: Data model + migration

**Files:**
- Create: `app/db/models/agent_device.py`
- Modify: `app/db/models/user.py`, `app/db/models/conversation.py`, `app/db/models/notification_log.py`, `app/db/models/__init__.py`
- Create: `alembic/versions/ac30_agent_caller_push.py`
- Create: `tests/agent_caller/__init__.py`, `tests/agent_caller/test_models.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `AgentDevice`, `DevicePlatform.ANDROID|IOS`, `AvailabilityStatus.AVAILABLE|AWAY`, `User.availability_status`, `Ticket.acknowledged_at`, `TicketStatus.MISSED`, `NotifChannel.PUSH`, `NotifEvent.ESCALATION_ALERT|ESCALATION_FALLBACK|ESCALATION_MISSED`.

- [ ] **Step 1: Create the branch**

```bash
cd /Users/adebayovictor/Documents/dev/luran/luran
git fetch origin
git checkout -b feat/agent-caller-push origin/develop
```

- [ ] **Step 2: Write the failing test**

Create `tests/agent_caller/__init__.py` (empty file) and `tests/agent_caller/test_models.py`:

```python
"""Schema-level tests for the Agent Caller data model."""

import uuid

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import TicketStatus
from app.db.models.notification_log import NotifChannel, NotifEvent
from app.db.models.user import AvailabilityStatus, User, UserRole


def _user(db, tenant_id, email="agent@example.com", role=UserRole.SUPPORT_AGENT):
    user = User(
        email=email,
        hashed_password="x",
        full_name="Agent One",
        role=role,
        tenant_id=tenant_id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_new_enum_values_exist():
    assert TicketStatus.MISSED.value == "missed"
    assert NotifChannel.PUSH.value == "push"
    assert NotifEvent.ESCALATION_ALERT.value == "escalation_alert"
    assert NotifEvent.ESCALATION_FALLBACK.value == "escalation_fallback"
    assert NotifEvent.ESCALATION_MISSED.value == "escalation_missed"


def test_user_defaults_to_available(db):
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    assert user.availability_status == AvailabilityStatus.AVAILABLE


def test_agent_device_round_trips(db):
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    device = AgentDevice(
        user_id=user.id,
        tenant_id=tenant_id,
        device_token="tok-abc",
        platform=DevicePlatform.ANDROID,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    assert device.platform is DevicePlatform.ANDROID
    assert device.device_token == "tok-abc"


def test_duplicate_user_token_is_rejected(db):
    from sqlalchemy.exc import IntegrityError

    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id)
    db.add(
        AgentDevice(
            user_id=user.id,
            tenant_id=tenant_id,
            device_token="dupe",
            platform=DevicePlatform.IOS,
        )
    )
    db.commit()
    db.add(
        AgentDevice(
            user_id=user.id,
            tenant_id=tenant_id,
            device_token="dupe",
            platform=DevicePlatform.IOS,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ticket_acknowledged_at_column_exists():
    from app.db.models.conversation import Ticket

    assert "acknowledged_at" in Ticket.__table__.columns
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_models.py -v --no-cov
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db.models.agent_device'`.

- [ ] **Step 4: Create the `AgentDevice` model**

Create `app/db/models/agent_device.py`:

```python
"""AgentDevice — one row per (agent, mobile device) for escalation push delivery."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class DevicePlatform(str, enum.Enum):
    """Mobile platform a registered device runs."""

    ANDROID = "android"
    IOS = "ios"


class AgentDevice(Base, UUIDMixin, TimestampMixin):
    """An agent's registered push target.

    An agent may have several devices; every one of them rings. Rows are removed
    on logout and pruned lazily when FCM reports the token as UNREGISTERED.
    """

    __tablename__ = "agent_devices"
    __table_args__ = (
        UniqueConstraint("user_id", "device_token", name="uq_agent_device_user_token"),
        Index("ix_agent_devices_tenant_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Agent this device belongs to",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="Tenant of the owning agent (denormalised for tenant-scoped fanout)",
    )
    device_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="FCM registration token — never returned by any API or written to logs",
    )
    platform: Mapped[DevicePlatform] = mapped_column(
        SQLEnum(
            DevicePlatform,
            name="device_platform",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time the app re-registered this token",
    )

    def __repr__(self) -> str:
        return f"<AgentDevice(user_id={self.user_id}, platform={self.platform})>"
```

- [ ] **Step 5: Add availability to `User`**

In `app/db/models/user.py`, add this enum directly below the existing `UserRole` class:

```python
class AvailabilityStatus(str, enum.Enum):
    """Agent availability for escalation broadcast (manual toggle only)."""

    AVAILABLE = "available"
    AWAY = "away"
```

And add this column to `User`, immediately after the `must_change_password` column:

```python
    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        SQLEnum(
            AvailabilityStatus,
            name="availability_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=AvailabilityStatus.AVAILABLE,
        server_default="available",
        nullable=False,
        comment="Whether this agent is rung in the escalation broadcast tier",
    )
```

- [ ] **Step 6: Add `MISSED` + `acknowledged_at` to the ticket model**

In `app/db/models/conversation.py`, add to `TicketStatus` (after `CLOSED`):

```python
    MISSED = "missed"
```

And add this column to `Ticket`, immediately after the `resolved_at` column:

```python
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When an agent claimed this escalation from the mobile app",
    )
```

- [ ] **Step 7: Add the push channel and escalation events**

In `app/db/models/notification_log.py`, add to `NotifEvent`:

```python
    ESCALATION_ALERT = "escalation_alert"
    ESCALATION_FALLBACK = "escalation_fallback"
    ESCALATION_MISSED = "escalation_missed"
```

And add to `NotifChannel`:

```python
    PUSH = "push"
```

- [ ] **Step 8: Export the new model**

In `app/db/models/__init__.py`, add this import next to the other model imports (keep alphabetical placement — it sorts right after the `api_key` import):

```python
from app.db.models.agent_device import AgentDevice, DevicePlatform
```

If the file has an `__all__` list, add `"AgentDevice"` and `"DevicePlatform"` to it. Also add `"AvailabilityStatus"` alongside the existing `UserRole` export and import it from `app.db.models.user`.

- [ ] **Step 9: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_models.py -v --no-cov
```
Expected: 5 passed.

- [ ] **Step 10: Write the Alembic migration**

Create `alembic/versions/ac30_agent_caller_push.py`:

```python
"""agent caller: agent_devices, availability, ticket acknowledgement, push channel

Revision ID: ac30agentcaller
Revises: ab20blockedquality
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "ac30agentcaller"
down_revision = "ab20blockedquality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- additive enum values -------------------------------------------------
    # Postgres 15 permits ADD VALUE inside a transaction as long as the value is
    # not *used* in the same transaction (it is not here).
    # ticket_status is mapped without values_callable, so it stores member NAMES.
    op.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'MISSED'")
    # notif_channel / notif_event use values_callable, so they store lowercase values.
    op.execute("ALTER TYPE notif_channel ADD VALUE IF NOT EXISTS 'push'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_alert'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_fallback'")
    op.execute("ALTER TYPE notif_event ADD VALUE IF NOT EXISTS 'escalation_missed'")

    # --- users.availability_status -------------------------------------------
    availability = sa.Enum("available", "away", name="availability_status")
    availability.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column(
            "availability_status",
            availability,
            nullable=False,
            server_default="available",
        ),
    )

    # --- tickets.acknowledged_at ---------------------------------------------
    op.add_column(
        "tickets",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- agent_devices --------------------------------------------------------
    platform = sa.Enum("android", "ios", name="device_platform")
    platform.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_devices",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_token", sa.Text(), nullable=False),
        sa.Column("platform", platform, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "device_token", name="uq_agent_device_user_token"),
    )
    op.create_index("ix_agent_devices_id", "agent_devices", ["id"])
    op.create_index("ix_agent_devices_user_id", "agent_devices", ["user_id"])
    op.create_index("ix_agent_devices_tenant_id", "agent_devices", ["tenant_id"])
    op.create_index("ix_agent_devices_tenant_user", "agent_devices", ["tenant_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_devices_tenant_user", table_name="agent_devices")
    op.drop_index("ix_agent_devices_tenant_id", table_name="agent_devices")
    op.drop_index("ix_agent_devices_user_id", table_name="agent_devices")
    op.drop_index("ix_agent_devices_id", table_name="agent_devices")
    op.drop_table("agent_devices")
    sa.Enum(name="device_platform").drop(op.get_bind(), checkfirst=True)
    op.drop_column("tickets", "acknowledged_at")
    op.drop_column("users", "availability_status")
    sa.Enum(name="availability_status").drop(op.get_bind(), checkfirst=True)
    # Postgres cannot drop enum values without recreating the type; the extra
    # ticket_status / notif_* values are inert when unused, so they stay.
```

- [ ] **Step 11: Verify the migration applies against the dev database**

```bash
docker compose up -d db && poetry run alembic upgrade head && poetry run alembic current
```
Expected: `alembic current` reports `ac30agentcaller (head)`. If the dev DB is not stamped, stamp it to `ab20blockedquality` first (`poetry run alembic stamp ab20blockedquality`) — per `project_auditor_spa_crawl`, the dev DB on port 5432 has already been stamped once.

- [ ] **Step 12: Verify the migration round-trips**

```bash
poetry run alembic downgrade -1 && poetry run alembic upgrade head
```
Expected: both commands exit 0.

- [ ] **Step 13: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/db/models tests/agent_caller alembic/versions/ac30_agent_caller_push.py
git commit -m "feat(agent-caller): agent_devices, availability, ticket acknowledgement, push channel"
```

---

### Task 2: Config settings + FCM HTTP v1 client

**Files:**
- Modify: `app/core/config.py`
- Create: `app/services/fcm_client.py`
- Create: `tests/agent_caller/test_fcm_client.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `settings.FIREBASE_CREDENTIALS_JSON: str`, `settings.AGENT_ALERT_ENABLED: bool`, `settings.AGENT_ALERT_WAIT_SECONDS: int`, `settings.AGENT_ALERT_ANDROID_CHANNEL_ID: str`
  - `app.services.fcm_client.FCMResult(token: str, ok: bool, unregistered: bool, error: str | None)`
  - `async def send(device_token: str, data: dict[str, str], *, notification: dict[str, str] | None = None) -> FCMResult`
  - `FCMConfigError(RuntimeError)`

**Design note (stated assumption):** Google's own `google-auth` library is *not* a project dependency and adding it would force a `poetry lock` + Docker image rebuild. The OAuth2 JWT-bearer grant it performs is ~30 lines and `python-jose[cryptography]` (already a dependency) signs RS256. We implement it inline with a process-local token cache. If token minting ever proves flaky in production, swapping in `google-auth` is a contained change behind `_access_token()`.

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_fcm_client.py`:

```python
"""Unit tests for the FCM HTTP v1 transport."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import fcm_client
from app.services.fcm_client import FCMConfigError

_FAKE_CREDS = json.dumps(
    {
        "client_email": "svc@luran.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----FAKE-----END PRIVATE KEY-----",
        "project_id": "luran-test",
    }
)


class _Resp:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)


@pytest.fixture(autouse=True)
def _clear_token_cache():
    fcm_client._token_cache.clear()
    yield
    fcm_client._token_cache.clear()


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", "")
    with pytest.raises(FCMConfigError):
        fcm_client._credentials()


def test_malformed_credentials_raises(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", "{not json")
    with pytest.raises(FCMConfigError):
        fcm_client._credentials()


@pytest.mark.asyncio
async def test_send_success(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(return_value="ya29.fake")),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=_Resp(200))),
    ):
        result = await fcm_client.send("tok-1", {"type": "escalation_alert"})
    assert result.ok is True
    assert result.unregistered is False


@pytest.mark.asyncio
async def test_send_unregistered_token_is_flagged(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    body = '{"error":{"status":"NOT_FOUND","details":[{"errorCode":"UNREGISTERED"}]}}'
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(return_value="ya29.fake")),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=_Resp(404, body))),
    ):
        result = await fcm_client.send("tok-dead", {"type": "escalation_alert"})
    assert result.ok is False
    assert result.unregistered is True


@pytest.mark.asyncio
async def test_send_transport_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        result = await fcm_client.send("tok-1", {"type": "escalation_alert"})
    assert result.ok is False
    assert result.unregistered is False
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_access_token_is_cached(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    post = AsyncMock(return_value=_Resp(200, '{"access_token":"ya29.a","expires_in":3600}'))
    with (
        patch("app.services.fcm_client.jwt.encode", return_value="signed-assertion"),
        patch("httpx.AsyncClient.post", post),
    ):
        first = await fcm_client._access_token()
        second = await fcm_client._access_token()
    assert first == second == "ya29.a"
    assert post.await_count == 1


def test_data_payload_values_are_all_strings():
    payload = fcm_client.stringify_data({"wait_minutes": 4, "reason": None, "type": "x"})
    assert payload == {"wait_minutes": "4", "type": "x"}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_fcm_client.py -v --no-cov
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.fcm_client'`.

- [ ] **Step 3: Add the settings**

In `app/core/config.py`, add these fields inside `Settings`, directly below the `REDIS_URL: str` field (line ~125):

```python
    # Agent Caller — escalation push (FCM HTTP v1)
    FIREBASE_CREDENTIALS_JSON: str = ""
    AGENT_ALERT_ENABLED: bool = True
    AGENT_ALERT_WAIT_SECONDS: int = 90
    AGENT_ALERT_ANDROID_CHANNEL_ID: str = "luran_escalation"
```

Then add the same four keys to `.env.example` under a new `# --- Agent Caller (escalation push) ---` heading, with empty/default values and a comment that `FIREBASE_CREDENTIALS_JSON` is the raw service-account JSON on a single line and lives only in the encrypted `.env.{stage}` bundles managed by `scripts/env.sh`.

- [ ] **Step 4: Write the FCM client**

Create `app/services/fcm_client.py`:

```python
"""FCM HTTP v1 transport for Agent Caller escalation pushes.

Mints an OAuth2 access token from the Firebase service-account JSON using the
JWT-bearer grant (signed with python-jose, already a project dependency) and
sends one message per device token. Per-token delivery failures never raise —
they come back on the FCMResult so the caller can log and prune.
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx
from jose import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_TIMEOUT = 10.0

# client_email -> (access_token, expires_at_epoch)
_token_cache: dict[str, tuple[str, float]] = {}


class FCMConfigError(RuntimeError):
    """FIREBASE_CREDENTIALS_JSON is missing or malformed."""


@dataclass
class FCMResult:
    """Outcome of a single-device send."""

    token: str
    ok: bool
    unregistered: bool = False
    error: str | None = None


def stringify_data(data: dict) -> dict[str, str]:
    """FCM `data` values must all be strings; drop Nones, stringify the rest."""
    return {k: str(v) for k, v in data.items() if v is not None}


def _credentials() -> dict:
    raw = settings.FIREBASE_CREDENTIALS_JSON
    if not raw:
        raise FCMConfigError("FIREBASE_CREDENTIALS_JSON is not configured")
    try:
        creds = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FCMConfigError("FIREBASE_CREDENTIALS_JSON is not valid JSON") from exc
    for key in ("client_email", "private_key", "project_id"):
        if not creds.get(key):
            raise FCMConfigError(f"FIREBASE_CREDENTIALS_JSON is missing '{key}'")
    return creds


async def _access_token() -> str:
    """Return a cached or freshly minted OAuth2 access token for FCM."""
    creds = _credentials()
    now = time.time()
    cached = _token_cache.get(creds["client_email"])
    if cached is not None and cached[1] > now + 60:
        return cached[0]

    assertion = jwt.encode(
        {
            "iss": creds["client_email"],
            "scope": _SCOPE,
            "aud": _TOKEN_URL,
            "iat": int(now),
            "exp": int(now) + 3600,
        },
        creds["private_key"],
        algorithm="RS256",
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    _token_cache[creds["client_email"]] = (token, now + int(payload.get("expires_in", 3600)))
    return token


def _envelope(
    device_token: str,
    data: dict[str, str],
    notification: dict[str, str] | None,
) -> dict:
    """Build the HTTP v1 `message` body.

    A `notification` block makes the push user-visible (the ring). Without it the
    message is data-only and silent — that is how `clear_alert` reaches phones
    without disturbing the agent.
    """
    visible = notification is not None
    message: dict = {"token": device_token, "data": data}
    android: dict = {"priority": "high"}
    apns_payload: dict = {"aps": {}}

    if visible:
        message["notification"] = notification
        android["notification"] = {
            "channel_id": settings.AGENT_ALERT_ANDROID_CHANNEL_ID,
            # The app builds its own full-screen-intent notification from `data`;
            # this block is the OS fallback if the app cannot run.
            "default_sound": True,
        }
        apns_headers = {"apns-priority": "10"}
    else:
        apns_payload["aps"]["content-available"] = 1
        apns_headers = {"apns-priority": "5", "apns-push-type": "background"}

    message["android"] = android
    message["apns"] = {"headers": apns_headers, "payload": apns_payload}
    return message


async def send(
    device_token: str,
    data: dict[str, str],
    *,
    notification: dict[str, str] | None = None,
) -> FCMResult:
    """Send one FCM message. Never raises on delivery failure."""
    try:
        creds = _credentials()
        access = await _access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _SEND_URL.format(project_id=creds["project_id"]),
                headers={"Authorization": f"Bearer {access}"},
                json={"message": _envelope(device_token, data, notification)},
            )
    except Exception as exc:  # noqa: BLE001 — transport errors are data, not crashes
        logger.warning("FCM send failed before dispatch: %s", exc)
        return FCMResult(token=device_token, ok=False, error=str(exc))

    if resp.status_code == 200:
        return FCMResult(token=device_token, ok=True)

    body = resp.text or ""
    unregistered = resp.status_code == 404 or "UNREGISTERED" in body or "InvalidRegistration" in body
    logger.warning("FCM send rejected (%s, unregistered=%s)", resp.status_code, unregistered)
    return FCMResult(
        token=device_token,
        ok=False,
        unregistered=unregistered,
        error=f"{resp.status_code}: {body[:200]}",
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_fcm_client.py -v --no-cov
```
Expected: 7 passed.

- [ ] **Step 6: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/core/config.py .env.example app/services/fcm_client.py tests/agent_caller/test_fcm_client.py
git commit -m "feat(agent-caller): FCM HTTP v1 client + alert settings"
```

---

### Task 3: AgentAlertService — recipient tiers, payloads, logging

**Files:**
- Create: `app/services/agent_alert_service.py`
- Modify: `app/services/notification_enqueue.py`
- Create: `tests/agent_caller/test_agent_alert_service.py`

**Interfaces:**
- Consumes: Task 1 models (`AgentDevice`, `AvailabilityStatus`, `NotifEvent.ESCALATION_*`, `NotifChannel.PUSH`); Task 2 settings.
- Produces:
  - `AgentAlertService(db)` with methods:
    - `broadcast_recipients(tenant_id: UUID) -> list[User]`
    - `supervisor_recipients(tenant_id: UUID) -> list[User]`
    - `devices_for(user_ids: list[UUID]) -> list[AgentDevice]`
    - `build_alert_payload(ticket: Ticket, session: ConversationSession) -> dict[str, str]`
    - `build_clear_payload(ticket_id: UUID, handled_by: str) -> dict[str, str]`
    - `build_missed_payload(ticket_id: UUID, handled_by: str) -> dict[str, str]`
    - `claim_log(tenant_id, event, ticket_id, user_id) -> UUID | None` (dedupe gate; `None` == already sent)
    - `mark_log(log_id: UUID, *, sent: bool, error: str | None = None) -> None`
    - `prune_token(device_token: str) -> None`
    - `rung_user_ids(ticket_id: UUID) -> list[UUID]`
  - `app.services.notification_enqueue.enqueue_agent_alerts_async(ticket_id, wait_seconds)` and `enqueue_clear_alert_async(ticket_id, winner_user_id)`

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_agent_alert_service.py`:

```python
"""Unit tests for AgentAlertService recipient tiers, payloads and log dedupe."""

import uuid
from datetime import timedelta

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.notification_log import NotifEvent
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.services.agent_alert_service import AgentAlertService
from app.utils.datetime import utcnow_naive


def _make_user(db, tenant_id, email, role, availability=AvailabilityStatus.AVAILABLE):
    user = User(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0].title(),
        role=role,
        tenant_id=tenant_id,
        is_active=True,
        availability_status=availability,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_device(db, user, token="tok"):
    device = AgentDevice(
        user_id=user.id,
        tenant_id=user.tenant_id,
        device_token=token,
        platform=DevicePlatform.ANDROID,
    )
    db.add(device)
    db.commit()
    return device


def _make_escalation(db, tenant_id, *, priority=TicketPriority.HIGH, reason="Refund request"):
    session = ConversationSession(
        tenant_id=tenant_id,
        status=SessionStatus.ESCALATED,
        escalation_reason=reason,
        escalated_at=utcnow_naive() - timedelta(minutes=4),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-TEST-{uuid.uuid4().hex[:6]}",
        summary="Customer wants a refund",
        priority=priority,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket, session


def test_broadcast_excludes_away_and_deviceless(db):
    tenant_id = uuid.uuid4()
    ready = _make_user(db, tenant_id, "ready@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, ready, "tok-ready")
    away = _make_user(db, tenant_id, "away@x.com", UserRole.SUPPORT_AGENT, AvailabilityStatus.AWAY)
    _make_device(db, away, "tok-away")
    _make_user(db, tenant_id, "nodevice@x.com", UserRole.SUPPORT_AGENT)
    _make_user(db, tenant_id, "viewer@x.com", UserRole.VIEWER)

    ids = {u.id for u in AgentAlertService(db).broadcast_recipients(tenant_id)}
    assert ids == {ready.id}


def test_broadcast_is_tenant_scoped(db):
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    a = _make_user(db, mine, "a@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, a, "tok-a")
    b = _make_user(db, theirs, "b@y.com", UserRole.SUPPORT_AGENT)
    _make_device(db, b, "tok-b")

    ids = {u.id for u in AgentAlertService(db).broadcast_recipients(mine)}
    assert ids == {a.id}


def test_supervisors_are_admins_regardless_of_availability(db):
    tenant_id = uuid.uuid4()
    admin = _make_user(
        db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, AvailabilityStatus.AWAY
    )
    _make_device(db, admin, "tok-boss")
    agent = _make_user(db, tenant_id, "agent@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, agent, "tok-agent")

    ids = {u.id for u in AgentAlertService(db).supervisor_recipients(tenant_id)}
    assert ids == {admin.id}


def test_alert_payload_shape(db):
    tenant_id = uuid.uuid4()
    ticket, session = _make_escalation(db, tenant_id, priority=TicketPriority.URGENT)
    payload = AgentAlertService(db).build_alert_payload(ticket, session)

    assert payload["type"] == "escalation_alert"
    assert payload["escalation_id"] == str(ticket.id)
    assert payload["session_id"] == str(session.id)
    assert payload["priority"] == "critical"
    assert payload["reason"] == "Refund request"
    assert payload["customer_name"] == "Customer"
    assert int(payload["wait_minutes"]) >= 4
    assert all(isinstance(v, str) for v in payload.values())


@pytest.mark.parametrize(
    "priority,expected",
    [
        (TicketPriority.URGENT, "critical"),
        (TicketPriority.HIGH, "important"),
        (TicketPriority.MEDIUM, "normal"),
        (TicketPriority.LOW, "normal"),
    ],
)
def test_priority_mapping(db, priority, expected):
    tenant_id = uuid.uuid4()
    ticket, session = _make_escalation(db, tenant_id, priority=priority)
    assert AgentAlertService(db).build_alert_payload(ticket, session)["priority"] == expected


def test_claim_log_is_idempotent_per_tier(db):
    tenant_id = uuid.uuid4()
    ticket, _ = _make_escalation(db, tenant_id)
    user = _make_user(db, tenant_id, "a@x.com", UserRole.TENANT_ADMIN)
    svc = AgentAlertService(db)

    first = svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, user.id)
    second = svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, user.id)
    other_tier = svc.claim_log(tenant_id, NotifEvent.ESCALATION_FALLBACK, ticket.id, user.id)

    assert first is not None
    assert second is None, "same tier must dedupe"
    assert other_tier is not None, "fallback tier must NOT be deduped by the tier-1 row"


def test_rung_user_ids_covers_both_tiers(db):
    tenant_id = uuid.uuid4()
    ticket, _ = _make_escalation(db, tenant_id)
    a = _make_user(db, tenant_id, "a@x.com", UserRole.SUPPORT_AGENT)
    b = _make_user(db, tenant_id, "b@x.com", UserRole.TENANT_ADMIN)
    svc = AgentAlertService(db)
    svc.claim_log(tenant_id, NotifEvent.ESCALATION_ALERT, ticket.id, a.id)
    svc.claim_log(tenant_id, NotifEvent.ESCALATION_FALLBACK, ticket.id, b.id)

    assert set(svc.rung_user_ids(ticket.id)) == {a.id, b.id}


def test_prune_token_removes_the_device(db):
    tenant_id = uuid.uuid4()
    user = _make_user(db, tenant_id, "a@x.com", UserRole.SUPPORT_AGENT)
    _make_device(db, user, "tok-dead")
    AgentAlertService(db).prune_token("tok-dead")
    assert AgentAlertService(db).devices_for([user.id]) == []
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_agent_alert_service.py -v --no-cov
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.agent_alert_service'`.

- [ ] **Step 3: Write the service**

Create `app/services/agent_alert_service.py`:

```python
"""AgentAlertService — recipient tiers, push payloads and delivery bookkeeping.

Recipient resolution deliberately happens at *send* time (inside the ARQ jobs),
not at escalation time: the supervisor-fallback fires 90 seconds later and
availability may have changed in between.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.agent_device import AgentDevice
from app.db.models.conversation import ConversationSession, Ticket, TicketPriority
from app.db.models.notification_log import (
    NotificationLog,
    NotifChannel,
    NotifEvent,
    NotifStatus,
)
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.db.models.visitor import Visitor
from app.services.fcm_client import stringify_data
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

_PRIORITY_WIRE = {
    TicketPriority.URGENT: "critical",
    TicketPriority.HIGH: "important",
    TicketPriority.MEDIUM: "normal",
    TicketPriority.LOW: "normal",
}

_ENTITY_TYPE = "ticket"


class AgentAlertService:
    """Everything the escalation-push ARQ jobs need from the database."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- recipients ------------------------------------------------------

    def broadcast_recipients(self, tenant_id: uuid.UUID) -> list[User]:
        """Tier 1: active, *available* agents of this tenant that have a device."""
        stmt = (
            select(User)
            .join(AgentDevice, AgentDevice.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                User.availability_status == AvailabilityStatus.AVAILABLE,
            )
            .distinct()
        )
        return list(self.db.execute(stmt).scalars().all())

    def supervisor_recipients(self, tenant_id: uuid.UUID) -> list[User]:
        """Fallback tier: tenant admins with a device, availability ignored."""
        stmt = (
            select(User)
            .join(AgentDevice, AgentDevice.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role == UserRole.TENANT_ADMIN,
            )
            .distinct()
        )
        return list(self.db.execute(stmt).scalars().all())

    def devices_for(self, user_ids: list[uuid.UUID]) -> list[AgentDevice]:
        if not user_ids:
            return []
        stmt = select(AgentDevice).where(AgentDevice.user_id.in_(user_ids))
        return list(self.db.execute(stmt).scalars().all())

    # ---- payloads --------------------------------------------------------

    def _customer_name(self, session: ConversationSession) -> str:
        if session.visitor_id is None:
            return "Customer"
        visitor = self.db.get(Visitor, session.visitor_id)
        return (visitor.name if visitor and visitor.name else None) or "Customer"

    def build_alert_payload(
        self, ticket: Ticket, session: ConversationSession
    ) -> dict[str, str]:
        """The ring payload — contract §4.1 `escalation_alert`."""
        started = session.escalated_at or ticket.created_at or utcnow_naive()
        if started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        wait_minutes = max(0, int((utcnow_naive() - started) / timedelta(minutes=1)))
        return stringify_data(
            {
                "type": "escalation_alert",
                "escalation_id": str(ticket.id),
                "session_id": str(session.id),
                "priority": _PRIORITY_WIRE.get(ticket.priority, "normal"),
                "customer_name": self._customer_name(session),
                "wait_minutes": wait_minutes,
                "reason": session.escalation_reason or ticket.summary or "Human help requested",
            }
        )

    def build_clear_payload(self, ticket_id: uuid.UUID, handled_by: str) -> dict[str, str]:
        """Silent data-only payload — contract §4.1 `clear_alert`."""
        return stringify_data(
            {
                "type": "clear_alert",
                "escalation_id": str(ticket_id),
                "handled_by": handled_by,
            }
        )

    def build_missed_payload(self, ticket_id: uuid.UUID, handled_by: str) -> dict[str, str]:
        """Low-priority payload — contract §4.1 `missed_escalation`."""
        return stringify_data(
            {
                "type": "missed_escalation",
                "escalation_id": str(ticket_id),
                "handled_by": handled_by,
            }
        )

    # ---- delivery bookkeeping -------------------------------------------

    def claim_log(
        self,
        tenant_id: uuid.UUID,
        event: NotifEvent,
        ticket_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Reserve the right to push (tenant, event, push, ticket, user).

        Returns the log id, or None when a row already exists — the unique index
        `uq_notification_dedupe` is what makes the ARQ jobs safe to retry. The
        recipient is the *user id*, never the device token (tokens must not be
        written to logs).
        """
        nested = self.db.begin_nested()
        log = NotificationLog(
            tenant_id=tenant_id,
            event=event,
            channel=NotifChannel.PUSH,
            entity_type=_ENTITY_TYPE,
            entity_id=ticket_id,
            recipient=str(user_id),
            status=NotifStatus.PENDING,
        )
        self.db.add(log)
        try:
            self.db.flush()
        except IntegrityError:
            nested.rollback()
            return None
        nested.commit()
        self.db.commit()
        return log.id

    def mark_log(self, log_id: uuid.UUID, *, sent: bool, error: str | None = None) -> None:
        log = self.db.get(NotificationLog, log_id)
        if log is None:
            return
        log.status = NotifStatus.SENT if sent else NotifStatus.FAILED
        log.error = None if sent else (error or "")[:500]
        self.db.commit()

    def rung_user_ids(self, ticket_id: uuid.UUID) -> list[uuid.UUID]:
        """Every user who was pushed for this escalation, across both tiers."""
        stmt = select(NotificationLog.recipient).where(
            NotificationLog.entity_id == ticket_id,
            NotificationLog.channel == NotifChannel.PUSH,
            NotificationLog.event.in_(
                [NotifEvent.ESCALATION_ALERT, NotifEvent.ESCALATION_FALLBACK]
            ),
        )
        out: list[uuid.UUID] = []
        for recipient in self.db.execute(stmt).scalars().all():
            try:
                out.append(uuid.UUID(recipient))
            except ValueError:
                continue
        return list(dict.fromkeys(out))

    def prune_token(self, device_token: str) -> None:
        """Drop a device FCM has reported as UNREGISTERED."""
        self.db.execute(delete(AgentDevice).where(AgentDevice.device_token == device_token))
        self.db.commit()
        logger.info("Pruned unregistered agent device token")
```

- [ ] **Step 4: Add the enqueue helpers**

In `app/services/notification_enqueue.py`, add `from datetime import timedelta` to the imports and append:

```python
async def enqueue_agent_alerts_async(ticket_id: uuid.UUID, wait_seconds: int) -> None:
    """Enqueue the escalation ring plus its deferred supervisor-fallback timer.

    Both jobs carry a deterministic `_job_id`, so a duplicate escalation for the
    same ticket cannot double-ring: ARQ rejects the second enqueue outright.
    """
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        await redis.enqueue_job(
            "alert_escalation",
            str(ticket_id),
            _job_id=f"alert_escalation:{ticket_id}",
        )
        await redis.enqueue_job(
            "alert_escalation_fallback",
            str(ticket_id),
            _job_id=f"alert_escalation_fallback:{ticket_id}",
            _defer_by=timedelta(seconds=wait_seconds),
        )
    finally:
        await redis.close()


async def enqueue_clear_alert_async(ticket_id: uuid.UUID, winner_user_id: uuid.UUID) -> None:
    """Enqueue the silent clear that stops the ring on every losing device."""
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        await redis.enqueue_job(
            "clear_agent_alert",
            str(ticket_id),
            str(winner_user_id),
            _job_id=f"clear_agent_alert:{ticket_id}",
        )
    finally:
        await redis.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_agent_alert_service.py -v --no-cov
```
Expected: 11 passed (7 tests + 4 parametrised priority cases).

- [ ] **Step 6: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services tests/agent_caller/test_agent_alert_service.py
git commit -m "feat(agent-caller): alert service for recipient tiers, payloads and dedupe"
```

---

### Task 4: ARQ jobs — ring, supervisor fallback, clear

**Files:**
- Create: `app/workers/agent_alerts.py`
- Modify: `app/workers/document_processor.py` (`WorkerSettings`, lines 1203-1240)
- Create: `tests/agent_caller/test_agent_alert_jobs.py`

**Interfaces:**
- Consumes: `AgentAlertService` (Task 3), `fcm_client.send` / `FCMResult` (Task 2), Task 1 models.
- Produces: `alert_escalation(ctx, ticket_id: str)`, `alert_escalation_fallback(ctx, ticket_id: str)`, `clear_agent_alert(ctx, ticket_id: str, winner_user_id: str)` — all registered in `WorkerSettings.functions`.

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_agent_alert_jobs.py`:

```python
"""Behaviour tests for the escalation ARQ jobs."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifStatus
from app.db.models.user import AvailabilityStatus, User, UserRole
from app.services.fcm_client import FCMResult
from app.utils.datetime import utcnow_naive
from app.workers import agent_alerts


@pytest.fixture
def patched_session(db, monkeypatch):
    """Make the jobs use the test session instead of opening their own."""
    monkeypatch.setattr(agent_alerts, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    return db


def _user(db, tenant_id, email, role, availability=AvailabilityStatus.AVAILABLE, token=None):
    user = User(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0].title(),
        role=role,
        tenant_id=tenant_id,
        is_active=True,
        availability_status=availability,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if token:
        db.add(
            AgentDevice(
                user_id=user.id,
                tenant_id=tenant_id,
                device_token=token,
                platform=DevicePlatform.ANDROID,
            )
        )
        db.commit()
    return user


def _escalation(db, tenant_id):
    session = ConversationSession(
        tenant_id=tenant_id,
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive() - timedelta(minutes=2),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket, session


@pytest.mark.asyncio
async def test_alert_escalation_rings_available_agents_only(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    _user(db, tenant_id, "away@x.com", UserRole.SUPPORT_AGENT, AvailabilityStatus.AWAY, "tok-away")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    rung = {call.args[0] for call in sent.await_args_list}
    assert rung == {"tok-up"}


@pytest.mark.asyncio
async def test_alert_escalation_is_a_noop_when_disabled(patched_session, monkeypatch):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)
    monkeypatch.setattr(agent_alerts.settings, "AGENT_ALERT_ENABLED", False)

    sent = AsyncMock()
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_rerunning_the_job_does_not_double_push(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation({}, str(ticket.id))

    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_unregistered_token_is_pruned(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    user = _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-dead")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(
        side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=False, unregistered=True)
    )
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    assert db.query(AgentDevice).filter(AgentDevice.user_id == user.id).count() == 0


@pytest.mark.asyncio
async def test_fallback_noops_when_already_claimed(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    admin = _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, _ = _escalation(db, tenant_id)
    ticket.assigned_to = admin.id
    ticket.status = TicketStatus.IN_PROGRESS
    db.commit()

    sent = AsyncMock()
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_rings_supervisors_and_misses_tier_one(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    agent = _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    by_token = {call.args[0]: call.args[1] for call in sent.await_args_list}
    assert by_token["tok-boss"]["type"] == "escalation_alert"
    assert by_token["tok-up"]["type"] == "missed_escalation"
    assert (
        db.query(NotificationLog)
        .filter(NotificationLog.event == NotifEvent.ESCALATION_FALLBACK)
        .count()
        == 1
    )


@pytest.mark.asyncio
async def test_fallback_marks_the_ticket_missed_when_nobody_answers(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))

    db.refresh(ticket)
    assert ticket.status == TicketStatus.MISSED


@pytest.mark.asyncio
async def test_clear_skips_the_winner(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    winner = _user(db, tenant_id, "win@x.com", UserRole.SUPPORT_AGENT, token="tok-win")
    _user(db, tenant_id, "lose@x.com", UserRole.SUPPORT_AGENT, token="tok-lose")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))
        sent.reset_mock()
        await agent_alerts.clear_agent_alert({}, str(ticket.id), str(winner.id))

    rung = {call.args[0] for call in sent.await_args_list}
    assert rung == {"tok-lose"}
    assert all(call.args[1]["type"] == "clear_alert" for call in sent.await_args_list)


@pytest.mark.asyncio
async def test_failed_send_marks_the_log_failed(patched_session):
    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, _ = _escalation(db, tenant_id)

    sent = AsyncMock(
        side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=False, error="503")
    )
    with patch.object(agent_alerts.fcm_client, "send", sent):
        await agent_alerts.alert_escalation({}, str(ticket.id))

    log = db.query(NotificationLog).one()
    assert log.status == NotifStatus.FAILED
    assert "503" in (log.error or "")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_agent_alert_jobs.py -v --no-cov
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.agent_alerts'`.

- [ ] **Step 3: Write the jobs**

Create `app/workers/agent_alerts.py`:

```python
"""ARQ jobs that ring agents' phones for an escalation.

Three jobs, all idempotent:
  * ``alert_escalation``          — ring tier 1 (available agents) immediately.
  * ``alert_escalation_fallback`` — deferred; ring supervisors only if still
    unclaimed, tell tier 1 they missed it, and mark the ticket MISSED.
  * ``clear_agent_alert``         — silent data push that stops the ring on the
    devices that lost the race.

Idempotency comes from the ``uq_notification_dedupe`` unique index, claimed via
``AgentAlertService.claim_log`` before each send. The fallback needs no
cancellation: it simply re-reads state and no-ops when the ticket is claimed.
"""

import logging
import uuid

from app.core.config import settings
from app.db.models.conversation import ConversationSession, Ticket, TicketStatus
from app.db.models.notification_log import NotifEvent
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services import fcm_client
from app.services.agent_alert_service import AgentAlertService

logger = logging.getLogger(__name__)

_RING_NOTIFICATION = {
    "title": "Customer needs you",
    "body": "A conversation was escalated to a human agent.",
}


def _load(db, ticket_id: uuid.UUID) -> tuple[Ticket | None, ConversationSession | None]:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return None, None
    return ticket, db.get(ConversationSession, ticket.session_id)


async def _push_tier(
    svc: AgentAlertService,
    *,
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    event: NotifEvent,
    recipients: list[User],
    payload: dict[str, str],
    visible: bool,
) -> int:
    """Push `payload` to every device of every recipient. Returns devices rung."""
    rung = 0
    for user in recipients:
        log_id = svc.claim_log(tenant_id, event, ticket_id, user.id)
        if log_id is None:
            continue  # already pushed for this (ticket, tier, user)
        devices = svc.devices_for([user.id])
        ok_any = False
        last_error: str | None = None
        for device in devices:
            result = await fcm_client.send(
                device.device_token,
                payload,
                notification=_RING_NOTIFICATION if visible else None,
            )
            if result.ok:
                ok_any = True
                rung += 1
            else:
                last_error = result.error
                if result.unregistered:
                    svc.prune_token(device.device_token)
        svc.mark_log(log_id, sent=ok_any, error=last_error)
    return rung


async def alert_escalation(ctx, ticket_id: str) -> None:
    """Ring every available agent of the tenant (broadcast tier)."""
    if not settings.AGENT_ALERT_ENABLED:
        logger.info("AGENT_ALERT_ENABLED is off — skipping escalation ring")
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        ticket, session = _load(db, tid)
        if ticket is None or session is None:
            logger.warning("alert_escalation: ticket %s no longer exists", ticket_id)
            return
        if ticket.assigned_to is not None:
            return  # already claimed before the job even ran

        svc = AgentAlertService(db)
        recipients = svc.broadcast_recipients(ticket.tenant_id)
        if not recipients:
            logger.info("No available agents with devices for tenant %s", ticket.tenant_id)
            return

        rung = await _push_tier(
            svc,
            tenant_id=ticket.tenant_id,
            ticket_id=tid,
            event=NotifEvent.ESCALATION_ALERT,
            recipients=recipients,
            payload=svc.build_alert_payload(ticket, session),
            visible=True,
        )
        logger.info("Escalation %s rang %s device(s)", ticket_id, rung)
    finally:
        db.close()


async def alert_escalation_fallback(ctx, ticket_id: str) -> None:
    """After the wait window: escalate to supervisors if still unclaimed."""
    if not settings.AGENT_ALERT_ENABLED:
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        ticket, session = _load(db, tid)
        if ticket is None or session is None:
            return
        if ticket.assigned_to is not None or ticket.status != TicketStatus.OPEN:
            return  # claimed or otherwise resolved — idempotent no-op

        svc = AgentAlertService(db)
        tier_one_ids = svc.rung_user_ids(tid)

        supervisors = svc.supervisor_recipients(ticket.tenant_id)
        if supervisors:
            await _push_tier(
                svc,
                tenant_id=ticket.tenant_id,
                ticket_id=tid,
                event=NotifEvent.ESCALATION_FALLBACK,
                recipients=supervisors,
                payload=svc.build_alert_payload(ticket, session),
                visible=True,
            )

        missed_targets = [
            user
            for user in (db.get(User, uid) for uid in tier_one_ids)
            if user is not None
        ]
        if missed_targets:
            await _push_tier(
                svc,
                tenant_id=ticket.tenant_id,
                ticket_id=tid,
                event=NotifEvent.ESCALATION_MISSED,
                recipients=missed_targets,
                payload=svc.build_missed_payload(tid, "your supervisor"),
                visible=False,
            )

        # Re-read: a supervisor may have claimed it while we were pushing.
        db.refresh(ticket)
        if ticket.assigned_to is None and ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.MISSED
            db.commit()
        logger.info("Escalation %s fell back to %s supervisor(s)", ticket_id, len(supervisors))
    finally:
        db.close()


async def clear_agent_alert(ctx, ticket_id: str, winner_user_id: str) -> None:
    """Stop the ring on every device except the winner's."""
    if not settings.AGENT_ALERT_ENABLED:
        return

    db = SessionLocal()
    try:
        tid = uuid.UUID(ticket_id)
        winner_id = uuid.UUID(winner_user_id)
        ticket = db.get(Ticket, tid)
        if ticket is None:
            return

        svc = AgentAlertService(db)
        winner = db.get(User, winner_id)
        payload = svc.build_clear_payload(tid, winner.full_name if winner else "another agent")

        loser_ids = [uid for uid in svc.rung_user_ids(tid) if uid != winner_id]
        for device in svc.devices_for(loser_ids):
            result = await fcm_client.send(device.device_token, payload, notification=None)
            if result.unregistered:
                svc.prune_token(device.device_token)
    finally:
        db.close()
```

- [ ] **Step 4: Register the jobs in `WorkerSettings`**

In `app/workers/document_processor.py`, inside `class WorkerSettings` add this import alongside the existing `from app.workers.sales_notifications import (...)` block:

```python
    from app.workers.agent_alerts import (  # type: ignore[misc]
        alert_escalation,
        alert_escalation_fallback,
        clear_agent_alert,
    )
```

and add the three names to the `functions = [...]` list, right after `sync_lead_to_crm`:

```python
        alert_escalation,
        alert_escalation_fallback,
        clear_agent_alert,
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_agent_alert_jobs.py -v --no-cov
```
Expected: 9 passed.

- [ ] **Step 6: Verify the worker registers the jobs**

```bash
poetry run python -c "from app.workers.document_processor import WorkerSettings; names={f.__name__ for f in WorkerSettings.functions}; assert {'alert_escalation','alert_escalation_fallback','clear_agent_alert'} <= names, names; print('registered:', sorted(names))"
```
Expected: prints the function list including the three new jobs.

- [ ] **Step 7: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/workers tests/agent_caller/test_agent_alert_jobs.py
git commit -m "feat(agent-caller): ARQ ring, supervisor fallback and clear jobs"
```

---

### Task 5: Agent router foundation — auth, `/agent/me`, devices, availability

**Files:**
- Create: `app/schemas/agent_app.py`
- Modify: `app/api/deps.py`
- Create: `app/api/v1/endpoints/agent_app.py`
- Modify: `app/api/v1/api.py`
- Create: `tests/agent_caller/conftest.py`, `tests/agent_caller/test_agent_devices_api.py`

**Interfaces:**
- Consumes: Task 1 models, Task 2 settings.
- Produces:
  - `app.api.deps.get_current_agent` FastAPI dependency returning `UserResponse`
  - Schemas `DeviceRegisterRequest`, `DeviceUnregisterRequest`, `AvailabilityUpdateRequest`, `AvailabilityResponse`, `AgentMeResponse`
  - Endpoints `POST /api/v1/agent/devices` (204), `DELETE /api/v1/agent/devices` (204), `PUT /api/v1/agent/availability` (200), `GET /api/v1/agent/me` (200)
  - `tests/agent_caller/conftest.py` fixtures `agent_tenant`, `agent_user`, `admin_user`, `agent_client`, `auth_headers`

- [ ] **Step 1: Write the shared test fixtures**

Create `tests/agent_caller/conftest.py`:

```python
"""Fixtures for the Agent Caller API tests."""

import uuid

import pytest

from app.core.security import create_access_token
from app.db.models.tenant import Tenant
from app.db.models.user import AvailabilityStatus, User, UserRole


@pytest.fixture
def agent_tenant(db):
    tenant = Tenant(
        name=f"acme-{uuid.uuid4().hex[:6]}",
        display_name="Acme Insurance",
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_user(db, tenant, email, role, availability=AvailabilityStatus.AVAILABLE):
    user = User(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0].title(),
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        availability_status=availability,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def agent_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "sam@acme.com", UserRole.SUPPORT_AGENT)


@pytest.fixture
def admin_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "boss@acme.com", UserRole.TENANT_ADMIN)


@pytest.fixture
def viewer_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "view@acme.com", UserRole.VIEWER)


@pytest.fixture
def auth_headers(agent_user):
    """Authorization header for the support agent (mobile app sends the header,
    not the cookie — deps.extract_token falls back to it)."""
    token = create_access_token(
        subject=str(agent_user.id),
        role=agent_user.role.value,
        tenant_id=str(agent_user.tenant_id),
    )
    return {"Authorization": f"Bearer {token}"}
```

**Before writing this file, confirm the exact `create_access_token` signature** in `app/core/security.py` (or wherever `AuthService.login` mints tokens — grep `def create_access_token`) and adjust the call to match. If the helper takes a claims dict instead of keyword args, build the dict with the same claims `AuthService` uses (`sub`, `role`, `tenant_id`).

Also confirm the exact required columns on `Tenant` — run `poetry run python -c "from app.db.models.tenant import Tenant; print([c.name for c in Tenant.__table__.columns if not c.nullable and c.default is None and c.server_default is None])"` and add any missing non-nullable fields to the fixture.

- [ ] **Step 2: Write the failing test**

Create `tests/agent_caller/test_agent_devices_api.py`:

```python
"""API tests for agent identity, device registration and availability."""

from app.db.models.agent_device import AgentDevice, DevicePlatform
from app.db.models.user import AvailabilityStatus


def test_me_returns_identity_and_tenant_name(client, auth_headers, agent_user, agent_tenant):
    resp = client.get("/api/v1/agent/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(agent_user.id)
    assert body["tenant_name"] == "Acme Insurance"
    assert body["role"] == "support_agent"
    assert body["availability_status"] == "available"


def test_me_requires_auth(client):
    assert client.get("/api/v1/agent/me").status_code == 401


def test_viewer_is_rejected(client, db, viewer_user):
    from app.core.security import create_access_token

    token = create_access_token(
        subject=str(viewer_user.id),
        role=viewer_user.role.value,
        tenant_id=str(viewer_user.tenant_id),
    )
    resp = client.get("/api/v1/agent/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_register_device_creates_a_row(client, db, auth_headers, agent_user):
    resp = client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "tok-1", "platform": "android"},
    )
    assert resp.status_code == 204
    device = db.query(AgentDevice).filter(AgentDevice.user_id == agent_user.id).one()
    assert device.device_token == "tok-1"
    assert device.platform is DevicePlatform.ANDROID
    assert device.tenant_id == agent_user.tenant_id


def test_register_device_is_an_upsert(client, db, auth_headers, agent_user):
    for _ in range(2):
        resp = client.post(
            "/api/v1/agent/devices",
            headers=auth_headers,
            json={"device_token": "tok-1", "platform": "android"},
        )
        assert resp.status_code == 204
    assert db.query(AgentDevice).filter(AgentDevice.user_id == agent_user.id).count() == 1


def test_register_device_updates_last_seen(client, db, auth_headers, agent_user):
    client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "tok-1", "platform": "android"},
    )
    device = db.query(AgentDevice).filter(AgentDevice.user_id == agent_user.id).one()
    assert device.last_seen_at is not None


def test_unregister_device_deletes_only_own_row(client, db, auth_headers, agent_user, admin_user):
    db.add(
        AgentDevice(
            user_id=admin_user.id,
            tenant_id=admin_user.tenant_id,
            device_token="tok-shared",
            platform=DevicePlatform.IOS,
        )
    )
    db.commit()
    client.post(
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "tok-shared", "platform": "android"},
    )

    resp = client.request(
        "DELETE",
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "tok-shared"},
    )
    assert resp.status_code == 204
    assert db.query(AgentDevice).filter(AgentDevice.user_id == agent_user.id).count() == 0
    assert db.query(AgentDevice).filter(AgentDevice.user_id == admin_user.id).count() == 1


def test_unregister_unknown_token_is_still_204(client, auth_headers):
    resp = client.request(
        "DELETE",
        "/api/v1/agent/devices",
        headers=auth_headers,
        json={"device_token": "never-seen"},
    )
    assert resp.status_code == 204


def test_set_availability(client, db, auth_headers, agent_user):
    resp = client.put(
        "/api/v1/agent/availability", headers=auth_headers, json={"status": "away"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "away"}
    db.refresh(agent_user)
    assert agent_user.availability_status is AvailabilityStatus.AWAY


def test_invalid_availability_is_rejected(client, auth_headers):
    resp = client.put(
        "/api/v1/agent/availability", headers=auth_headers, json={"status": "lunch"}
    )
    assert resp.status_code == 422
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_agent_devices_api.py -v --no-cov
```
Expected: FAIL — every request returns 404 (router not mounted).

- [ ] **Step 4: Add the `get_current_agent` dependency**

In `app/api/deps.py`, add directly below `get_current_tenant_admin`:

```python
async def get_current_agent(
    current_user: UserResponse = Depends(get_current_user),
) -> UserResponse:
    """Verify the caller is an escalation-capable agent (Agent Caller app)."""
    if current_user.role not in (UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Agent access required.",
        )
    return current_user
```

- [ ] **Step 5: Write the schemas**

Create `app/schemas/agent_app.py`:

```python
"""Request/response models for the Agent Caller mobile app (`/api/v1/agent`)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.agent_device import DevicePlatform
from app.db.models.user import AvailabilityStatus, UserRole


class DeviceRegisterRequest(BaseModel):
    """Upsert this device's FCM registration token."""

    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(..., min_length=8, max_length=4096)
    platform: DevicePlatform


class DeviceUnregisterRequest(BaseModel):
    """Remove a device on logout or token rotation."""

    model_config = ConfigDict(extra="forbid")

    device_token: str = Field(..., min_length=8, max_length=4096)


class AvailabilityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AvailabilityStatus


class AvailabilityResponse(BaseModel):
    status: AvailabilityStatus


class AgentMeResponse(BaseModel):
    """Contract addendum A1 — identity + tenant name + availability in one call."""

    user_id: UUID
    email: str
    full_name: str
    role: UserRole
    tenant_id: UUID
    tenant_name: str
    availability_status: AvailabilityStatus


class EscalationSummary(BaseModel):
    """One row of `GET /agent/escalations` (contract §4.2)."""

    escalation_id: UUID
    session_id: UUID
    customer_name: str
    reason: str
    priority: str
    status: str
    created_at: datetime


class TranscriptMessage(BaseModel):
    role: str
    content: str
    created_at: datetime


class EscalationDetail(EscalationSummary):
    """Detail view: summary fields plus the conversation transcript."""

    handled_by: str | None = None
    acknowledged_at: datetime | None = None
    transcript: list[TranscriptMessage]


class AcknowledgeWon(BaseModel):
    won: bool = True
    session_id: UUID
    ws_url: str


class AcknowledgeLost(BaseModel):
    won: bool = False
    handled_by: str
```

- [ ] **Step 6: Write the router**

Create `app/api/v1/endpoints/agent_app.py`:

```python
"""Agent Caller mobile-app API (`/api/v1/agent`).

Every endpoint requires an agent JWT and is scoped to `current_agent.tenant_id`.
A resource belonging to another tenant returns 404, never 403 — matching the
auditor's `_owned_report` pattern, so the API never confirms existence
cross-tenant.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent
from app.db.models.agent_device import AgentDevice
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.agent_app import (
    AgentMeResponse,
    AvailabilityResponse,
    AvailabilityUpdateRequest,
    DeviceRegisterRequest,
    DeviceUnregisterRequest,
)
from app.schemas.user import UserResponse
from app.utils.datetime import utcnow_naive

router = APIRouter()


@router.get("/me", response_model=AgentMeResponse)
def get_agent_me(
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Identity, tenant display name and availability in one call (addendum A1)."""
    tenant = db.get(Tenant, current_agent.tenant_id)
    user = db.get(User, current_agent.id)
    return AgentMeResponse(
        user_id=current_agent.id,
        email=current_agent.email,
        full_name=current_agent.full_name,
        role=current_agent.role,
        tenant_id=current_agent.tenant_id,
        tenant_name=(tenant.display_name if tenant else "") or "",
        availability_status=user.availability_status,
    )


@router.post("/devices", status_code=status.HTTP_204_NO_CONTENT)
def register_device(
    payload: DeviceRegisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Upsert this device's FCM token. Called after login and on every cold start."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()

    if device is None:
        device = AgentDevice(
            user_id=current_agent.id,
            tenant_id=current_agent.tenant_id,
            device_token=payload.device_token,
            platform=payload.platform,
        )
        db.add(device)

    device.platform = payload.platform
    device.last_seen_at = utcnow_naive()
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/devices", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    payload: DeviceUnregisterRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Remove this device on logout. Unknown tokens are a silent 204."""
    device = db.execute(
        select(AgentDevice).where(
            AgentDevice.user_id == current_agent.id,
            AgentDevice.device_token == payload.device_token,
        )
    ).scalar_one_or_none()
    if device is not None:
        db.delete(device)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/availability", response_model=AvailabilityResponse)
def set_availability(
    payload: AvailabilityUpdateRequest,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Manual Available/Away toggle. Only `available` agents ring in tier 1."""
    user = db.get(User, current_agent.id)
    user.availability_status = payload.status
    db.commit()
    return AvailabilityResponse(status=payload.status)
```

- [ ] **Step 7: Mount the router**

In `app/api/v1/api.py`, add `agent_app` to the `from app.api.v1.endpoints import (...)` list (alphabetically first, before `analytics`), and add this include next to the tickets include (~line 261):

```python
# Agent Caller mobile app endpoints
api_router.include_router(agent_app.router, prefix="/agent", tags=["agent-app"])
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_agent_devices_api.py -v --no-cov
```
Expected: 10 passed.

- [ ] **Step 9: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api app/schemas/agent_app.py tests/agent_caller
git commit -m "feat(agent-caller): agent router with identity, device registration and availability"
```

---

### Task 6: Escalation list + detail

**Files:**
- Modify: `app/api/v1/endpoints/agent_app.py`
- Create: `tests/agent_caller/test_agent_escalations_api.py`

**Interfaces:**
- Consumes: `EscalationSummary`, `EscalationDetail`, `TranscriptMessage` (Task 5 schemas); `get_current_agent`.
- Produces: `GET /api/v1/agent/escalations?window_hours=24` → `list[EscalationSummary]`; `GET /api/v1/agent/escalations/{ticket_id}` → `EscalationDetail`; module-level helpers `_wire_status(ticket) -> str` and `_owned_ticket(db, ticket_id, tenant_id) -> Ticket` (raises 404).

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_agent_escalations_api.py`:

```python
"""API tests for the escalation list and detail views."""

import uuid
from datetime import timedelta

import pytest

from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.utils.datetime import utcnow_naive


@pytest.fixture
def escalation(db, agent_tenant):
    session = ConversationSession(
        tenant_id=agent_tenant.id,
        status=SessionStatus.ESCALATED,
        escalation_reason="Customer requested a refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    for role, content in (
        (MessageRole.USER, "I want my money back"),
        (MessageRole.ASSISTANT, "Let me get a human for you"),
    ):
        db.add(Message(session_id=session.id, role=role, content=content))
    ticket = Ticket(
        tenant_id=agent_tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund request",
        priority=TicketPriority.URGENT,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def test_list_returns_pending_escalation(client, auth_headers, escalation):
    resp = client.get("/api/v1/agent/escalations", headers=auth_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["escalation_id"] == str(escalation.id)
    assert row["status"] == "pending"
    assert row["priority"] == "critical"
    assert row["reason"] == "Customer requested a refund"
    assert row["customer_name"] == "Customer"


@pytest.mark.parametrize(
    "ticket_status,assigned,expected",
    [
        (TicketStatus.OPEN, False, "pending"),
        (TicketStatus.IN_PROGRESS, True, "in_progress"),
        (TicketStatus.RESOLVED, True, "resolved"),
        (TicketStatus.CLOSED, True, "resolved"),
        (TicketStatus.MISSED, False, "missed"),
    ],
)
def test_status_derivation(
    client, db, auth_headers, escalation, agent_user, ticket_status, assigned, expected
):
    escalation.status = ticket_status
    escalation.assigned_to = agent_user.id if assigned else None
    db.commit()
    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    assert rows[0]["status"] == expected


def test_window_hours_filters_old_escalations(client, db, auth_headers, escalation):
    escalation.created_at = utcnow_naive() - timedelta(hours=48)
    db.commit()
    rows = client.get("/api/v1/agent/escalations?window_hours=24", headers=auth_headers).json()
    assert rows == []
    rows = client.get("/api/v1/agent/escalations?window_hours=72", headers=auth_headers).json()
    assert len(rows) == 1


def test_list_is_tenant_scoped(client, db, auth_headers, escalation):
    other_tenant_id = uuid.uuid4()
    session = ConversationSession(tenant_id=other_tenant_id, status=SessionStatus.ESCALATED)
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(
        Ticket(
            tenant_id=other_tenant_id,
            session_id=session.id,
            ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
            summary="Not yours",
            priority=TicketPriority.LOW,
            status=TicketStatus.OPEN,
        )
    )
    db.commit()

    rows = client.get("/api/v1/agent/escalations", headers=auth_headers).json()
    assert [r["escalation_id"] for r in rows] == [str(escalation.id)]


def test_detail_includes_transcript(client, auth_headers, escalation):
    resp = client.get(f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalation_id"] == str(escalation.id)
    assert [m["content"] for m in body["transcript"]] == [
        "I want my money back",
        "Let me get a human for you",
    ]
    assert body["handled_by"] is None


def test_detail_reports_handler_once_claimed(client, db, auth_headers, escalation, agent_user):
    escalation.assigned_to = agent_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    escalation.acknowledged_at = utcnow_naive()
    db.commit()
    body = client.get(
        f"/api/v1/agent/escalations/{escalation.id}", headers=auth_headers
    ).json()
    assert body["handled_by"] == agent_user.full_name
    assert body["acknowledged_at"] is not None


def test_cross_tenant_detail_is_404_not_403(client, db, auth_headers):
    other_tenant_id = uuid.uuid4()
    session = ConversationSession(tenant_id=other_tenant_id, status=SessionStatus.ESCALATED)
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=other_tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Not yours",
        priority=TicketPriority.LOW,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    resp = client.get(f"/api/v1/agent/escalations/{ticket.id}", headers=auth_headers)
    assert resp.status_code == 404


def test_unknown_detail_is_404(client, auth_headers):
    resp = client.get(f"/api/v1/agent/escalations/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_agent_escalations_api.py -v --no-cov
```
Expected: FAIL — 404 on `/api/v1/agent/escalations`.

- [ ] **Step 3: Implement the endpoints**

Append to `app/api/v1/endpoints/agent_app.py` (and extend the imports at the top of the file with the names used below):

```python
# --- add to the existing imports at the top of the file ---------------------
# from datetime import timedelta
# from uuid import UUID
# from fastapi import HTTPException, Query
# from app.db.models.conversation import (
#     ConversationSession, Message, Ticket, TicketPriority, TicketStatus,
# )
# from app.db.models.visitor import Visitor
# from app.schemas.agent_app import EscalationDetail, EscalationSummary, TranscriptMessage

_PRIORITY_WIRE = {
    TicketPriority.URGENT: "critical",
    TicketPriority.HIGH: "important",
    TicketPriority.MEDIUM: "normal",
    TicketPriority.LOW: "normal",
}


def _wire_status(ticket: Ticket) -> str:
    """Contract addendum A5 — derive the app's status vocabulary."""
    if ticket.status == TicketStatus.MISSED:
        return "missed"
    if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        return "resolved"
    if ticket.status == TicketStatus.IN_PROGRESS:
        return "in_progress"
    return "pending"


def _customer_name(db: Session, session: ConversationSession | None) -> str:
    if session is None or session.visitor_id is None:
        return "Customer"
    visitor = db.get(Visitor, session.visitor_id)
    return (visitor.name if visitor and visitor.name else None) or "Customer"


def _summary(db: Session, ticket: Ticket, session: ConversationSession | None) -> EscalationSummary:
    return EscalationSummary(
        escalation_id=ticket.id,
        session_id=ticket.session_id,
        customer_name=_customer_name(db, session),
        reason=(session.escalation_reason if session else None) or ticket.summary or "",
        priority=_PRIORITY_WIRE.get(ticket.priority, "normal"),
        status=_wire_status(ticket),
        created_at=ticket.created_at,
    )


def _owned_ticket(db: Session, ticket_id: UUID, tenant_id: UUID) -> Ticket:
    """Load a ticket inside the caller's tenant, or 404 (never 403)."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")
    return ticket


@router.get("/escalations", response_model=list[EscalationSummary])
def list_escalations(
    window_hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Active and recent escalations for this tenant, newest first."""
    cutoff = utcnow_naive() - timedelta(hours=window_hours)
    tickets = (
        db.execute(
            select(Ticket)
            .where(Ticket.tenant_id == current_agent.tenant_id, Ticket.created_at >= cutoff)
            .order_by(Ticket.created_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    sessions = {
        t.session_id: db.get(ConversationSession, t.session_id) for t in tickets
    }
    return [_summary(db, t, sessions.get(t.session_id)) for t in tickets]


@router.get("/escalations/{ticket_id}", response_model=EscalationDetail)
def get_escalation(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Detail + transcript. Serves both the pre-takeover and missed-summary views."""
    ticket = _owned_ticket(db, ticket_id, current_agent.tenant_id)
    session = db.get(ConversationSession, ticket.session_id)

    messages = (
        db.execute(
            select(Message)
            .where(Message.session_id == ticket.session_id)
            .order_by(Message.created_at.asc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    handler = db.get(User, ticket.assigned_to) if ticket.assigned_to else None

    base = _summary(db, ticket, session)
    return EscalationDetail(
        **base.model_dump(),
        handled_by=handler.full_name if handler else None,
        acknowledged_at=ticket.acknowledged_at,
        transcript=[
            TranscriptMessage(
                role=m.role.value if hasattr(m.role, "value") else str(m.role),
                content=m.content,
                created_at=m.created_at,
            )
            for m in messages
        ],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_agent_escalations_api.py -v --no-cov
```
Expected: 12 passed (8 tests + 5 parametrised status cases, minus the one already counted).

- [ ] **Step 5: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api/v1/endpoints/agent_app.py tests/agent_caller/test_agent_escalations_api.py
git commit -m "feat(agent-caller): escalation list and detail endpoints"
```

---

### Task 7: Acknowledge — atomic claim, session handoff, clear fanout

**Files:**
- Modify: `app/api/v1/endpoints/agent_app.py`
- Create: `tests/agent_caller/test_agent_acknowledge_api.py`

**Interfaces:**
- Consumes: `_owned_ticket` (Task 6), `enqueue_clear_alert_async` (Task 3), `AcknowledgeWon` / `AcknowledgeLost` (Task 5).
- Produces: `POST /api/v1/agent/escalations/{ticket_id}/acknowledge` → `200 {won, session_id, ws_url}` or `409 {won:false, handled_by}`; helper `_ws_url(session_id: UUID) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_agent_acknowledge_api.py`:

```python
"""API tests for the atomic escalation claim."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from app.utils.datetime import utcnow_naive


@pytest.fixture(autouse=True)
def _no_redis():
    """The clear fanout is enqueued, not executed, in these tests."""
    with patch(
        "app.api.v1.endpoints.agent_app.enqueue_clear_alert_async", AsyncMock()
    ) as enqueue:
        yield enqueue


@pytest.fixture
def escalation(db, agent_tenant):
    session = ConversationSession(
        tenant_id=agent_tenant.id,
        status=SessionStatus.ESCALATED,
        escalation_reason="Refund",
        escalated_at=utcnow_naive(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=agent_tenant.id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Refund",
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def test_first_claim_wins(client, db, auth_headers, escalation, agent_user):
    resp = client.post(
        f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["won"] is True
    assert body["session_id"] == str(escalation.session_id)
    assert body["ws_url"].endswith(f"/api/v1/ws/agent/{escalation.session_id}")
    assert body["ws_url"].startswith("ws")

    db.refresh(escalation)
    assert escalation.assigned_to == agent_user.id
    assert escalation.status == TicketStatus.IN_PROGRESS
    assert escalation.acknowledged_at is not None

    session = db.get(ConversationSession, escalation.session_id)
    assert session.agent_id == agent_user.id
    assert session.agent_joined_at is not None


def test_second_claim_loses_with_409(client, db, auth_headers, escalation, admin_user):
    # Simulate the racing winner with the same conditional UPDATE the endpoint uses.
    db.execute(
        update(Ticket)
        .where(Ticket.id == escalation.id, Ticket.assigned_to.is_(None))
        .values(
            assigned_to=admin_user.id,
            acknowledged_at=utcnow_naive(),
            status=TicketStatus.IN_PROGRESS,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()

    resp = client.post(
        f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"won": False, "handled_by": admin_user.full_name}


def test_reclaiming_your_own_escalation_is_idempotent(client, auth_headers, escalation):
    first = client.post(
        f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers
    )
    second = client.post(
        f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]


def test_winning_enqueues_the_clear(client, auth_headers, escalation, agent_user, _no_redis):
    client.post(f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers)
    _no_redis.assert_awaited_once()
    args = _no_redis.await_args.args
    assert args[0] == escalation.id
    assert args[1] == agent_user.id


def test_losing_does_not_enqueue_a_clear(
    client, db, auth_headers, escalation, admin_user, _no_redis
):
    escalation.assigned_to = admin_user.id
    escalation.status = TicketStatus.IN_PROGRESS
    db.commit()
    client.post(f"/api/v1/agent/escalations/{escalation.id}/acknowledge", headers=auth_headers)
    _no_redis.assert_not_awaited()


def test_cross_tenant_acknowledge_is_404(client, db, auth_headers):
    other_tenant_id = uuid.uuid4()
    session = ConversationSession(tenant_id=other_tenant_id, status=SessionStatus.ESCALATED)
    db.add(session)
    db.commit()
    db.refresh(session)
    ticket = Ticket(
        tenant_id=other_tenant_id,
        session_id=session.id,
        ticket_number=f"TKT-{uuid.uuid4().hex[:8]}",
        summary="Not yours",
        priority=TicketPriority.LOW,
        status=TicketStatus.OPEN,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    resp = client.post(
        f"/api/v1/agent/escalations/{ticket.id}/acknowledge", headers=auth_headers
    )
    assert resp.status_code == 404


def test_ws_url_uses_wss_for_https_host(monkeypatch):
    from app.api.v1.endpoints import agent_app

    monkeypatch.setattr(agent_app.settings, "SERVER_HOST", "https://api.luranai.com")
    sid = uuid.uuid4()
    assert agent_app._ws_url(sid) == f"wss://api.luranai.com/api/v1/ws/agent/{sid}"

    monkeypatch.setattr(agent_app.settings, "SERVER_HOST", "http://localhost:8000")
    assert agent_app._ws_url(sid) == f"ws://localhost:8000/api/v1/ws/agent/{sid}"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_agent_acknowledge_api.py -v --no-cov
```
Expected: FAIL — 404 on the acknowledge path.

- [ ] **Step 3: Implement acknowledge**

Append to `app/api/v1/endpoints/agent_app.py` (extending the top-of-file imports with `update` from `sqlalchemy`, `settings` from `app.core.config`, `AcknowledgeWon` from the schemas, and `enqueue_clear_alert_async` from `app.services.notification_enqueue`):

```python
def _ws_url(session_id: UUID) -> str:
    """Contract addendum A2 — absolute agent WebSocket URL for this session."""
    base = (settings.SERVER_HOST or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/api/v1/ws/agent/{session_id}"


@router.post("/escalations/{ticket_id}/acknowledge", response_model=AcknowledgeWon)
async def acknowledge_escalation(
    ticket_id: UUID,
    db: Session = Depends(get_db),
    current_agent: UserResponse = Depends(get_current_agent),
):
    """Claim an escalation. Exactly one caller can win.

    The conditional UPDATE *is* the coordination primitive: `assigned_to IS NULL`
    in the WHERE clause means the database serialises concurrent claims for us —
    no locks, no coordinator. `rowcount == 1` means we won.
    """
    ticket = _owned_ticket(db, ticket_id, current_agent.tenant_id)

    result = db.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.assigned_to.is_(None))
        .values(
            assigned_to=current_agent.id,
            acknowledged_at=utcnow_naive(),
            status=TicketStatus.IN_PROGRESS,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    won = result.rowcount == 1

    if not won:
        db.refresh(ticket)
        if ticket.assigned_to == current_agent.id:
            # Already ours — a retry of our own successful claim. Idempotent win.
            return AcknowledgeWon(won=True, session_id=ticket.session_id, ws_url=_ws_url(ticket.session_id))
        handler = db.get(User, ticket.assigned_to) if ticket.assigned_to else None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "won": False,
                "handled_by": handler.full_name if handler else "another agent",
            },
        )

    session = db.get(ConversationSession, ticket.session_id)
    if session is not None:
        session.agent_id = current_agent.id
        session.agent_joined_at = utcnow_naive()
        db.commit()

    try:
        await enqueue_clear_alert_async(ticket_id, current_agent.id)
    except Exception:  # noqa: BLE001 — a missed clear must not fail the claim
        logger.exception("Failed to enqueue clear_alert for escalation %s", ticket_id)

    return AcknowledgeWon(won=True, session_id=ticket.session_id, ws_url=_ws_url(ticket.session_id))
```

Add `import logging` and `logger = logging.getLogger(__name__)` near the top of the module if not already present.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_agent_acknowledge_api.py -v --no-cov
```
Expected: 7 passed.

- [ ] **Step 5: Run the whole feature suite**

```bash
poetry run pytest tests/agent_caller -v --no-cov
```
Expected: all green.

- [ ] **Step 6: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api/v1/endpoints/agent_app.py tests/agent_caller/test_agent_acknowledge_api.py
git commit -m "feat(agent-caller): atomic escalation claim with clear fanout"
```

---

### Task 8: Wire the ring into the escalation path

**Files:**
- Modify: `app/api/v1/endpoints/chat.py` (six ticket-creation sites)
- Create: `tests/agent_caller/test_chat_escalation_ring.py`

**Interfaces:**
- Consumes: `enqueue_agent_alerts_async` (Task 3), `settings.AGENT_ALERT_ENABLED` / `AGENT_ALERT_WAIT_SECONDS` (Task 2).
- Produces: `app.api.v1.endpoints.chat._ring_agents(ticket_id: UUID) -> None` (async, never raises).

**Applying the edits:** the six call sites are at **pre-edit** lines 868, 1067, 1300, 1522, 1684, 2386. Line numbers shift as you edit, so **apply them bottom-up (2386 first, 868 last)** or anchor on surrounding text. The sites at 1067 and 1300 have byte-identical surrounding lines and differ only by indentation (20 vs 24 spaces) — match on indentation to disambiguate.

- [ ] **Step 1: Write the failing test**

Create `tests/agent_caller/test_chat_escalation_ring.py`:

```python
"""The escalation path must enqueue the ring without ever breaking the chat reply."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.endpoints import chat


@pytest.mark.asyncio
async def test_ring_enqueues_both_jobs():
    ticket_id = uuid.uuid4()
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat.settings, "AGENT_ALERT_WAIT_SECONDS", 90),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(ticket_id)
    enqueue.assert_awaited_once_with(ticket_id, 90)


@pytest.mark.asyncio
async def test_ring_is_skipped_when_disabled():
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", False),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(uuid.uuid4())
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_ring_is_skipped_for_a_missing_ticket_id():
    enqueue = AsyncMock()
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(chat, "enqueue_agent_alerts_async", enqueue),
    ):
        await chat._ring_agents(None)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_never_propagates():
    with (
        patch.object(chat.settings, "AGENT_ALERT_ENABLED", True),
        patch.object(
            chat, "enqueue_agent_alerts_async", AsyncMock(side_effect=RuntimeError("redis down"))
        ),
    ):
        await chat._ring_agents(uuid.uuid4())  # must not raise


def test_every_ticket_creation_site_rings():
    """Guard against a future escalation branch forgetting the ring."""
    import inspect

    source = inspect.getsource(chat)
    creations = source.count("ticket_service.create_ticket(")
    rings = source.count("await _ring_agents(")
    assert rings == creations, f"{creations} create_ticket sites but {rings} _ring_agents calls"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_chat_escalation_ring.py -v --no-cov
```
Expected: FAIL — `AttributeError: module 'app.api.v1.endpoints.chat' has no attribute '_ring_agents'`.

- [ ] **Step 3: Add the helper to `chat.py`**

Add to the imports at the top of `app/api/v1/endpoints/chat.py`:

```python
from app.services.notification_enqueue import enqueue_agent_alerts_async
```

`settings` is already imported in this module (verify with `grep -n "^from app.core.config import" app/api/v1/endpoints/chat.py`; add it if absent).

Add this module-level helper directly after the existing module-level helper functions (below `_build_contact_capture_prompt`, before the first route):

```python
async def _ring_agents(ticket_id: UUID | None) -> None:
    """Ring the tenant's agents on their phones for a fresh escalation.

    Best-effort by design: the email escalation is the always-on backstop, so a
    Redis hiccup must never turn into a failed customer chat response. The two
    ARQ jobs carry deterministic job ids, so re-escalating the same ticket
    cannot double-ring.
    """
    if ticket_id is None or not settings.AGENT_ALERT_ENABLED:
        return
    try:
        await enqueue_agent_alerts_async(ticket_id, settings.AGENT_ALERT_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 — never break the chat reply over a push
        logger.exception("Failed to enqueue agent alert for ticket %s", ticket_id)
```

- [ ] **Step 4: Call it at all six escalation sites**

At each of the six sites, insert `await _ring_agents(ticket_id)` on the line immediately after `ticket_id = ticket.id`, at the same indentation. For example, the site at pre-edit line 1522 becomes:

```python
            ticket = ticket_service.create_ticket(ticket_data)
            ticket_id = ticket.id
            await _ring_agents(ticket_id)
```

and the site at pre-edit line 868 (the flow-router escalation) becomes:

```python
                ticket_id = ticket.id
                await _ring_agents(ticket_id)
```

Apply this at pre-edit lines **2386, 1684, 1522, 1300, 1067, 868**, in that order (bottom-up).

- [ ] **Step 5: Verify all six landed**

```bash
grep -c "await _ring_agents(ticket_id)" app/api/v1/endpoints/chat.py
grep -c "ticket_service.create_ticket(" app/api/v1/endpoints/chat.py
```
Expected: both print `6`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_chat_escalation_ring.py -v --no-cov
```
Expected: 5 passed.

- [ ] **Step 7: Confirm no chat regressions**

```bash
poetry run pytest tests/api/v1/test_chat_flow_router.py tests/api/v1/test_chat_sales_wiring.py tests/api/v1/test_chat_lead_capture.py tests/api/v1/test_chat_quote.py -v --no-cov
```
Expected: all pass (same counts as on `develop` — check out `develop` and rerun if any fail, to confirm the failure is pre-existing).

- [ ] **Step 8: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api/v1/endpoints/chat.py tests/agent_caller/test_chat_escalation_ring.py
git commit -m "feat(agent-caller): ring agents on every AI escalation"
```

---

### Task 9: Escalation dedup guard

**Files:**
- Modify: `app/services/conversation_service.py:247` (`escalate_session`)
- Create: `tests/agent_caller/test_escalation_dedup.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ConversationService.escalate_session` returns unchanged for an already-`ESCALATED` session but no longer overwrites `escalated_at` / `escalation_reason`.

**Why this task exists:** the spec (§5, "Dedup") requires that re-escalating an already-`ESCALATED` session must not create a second ticket or a second ring. The ARQ `_job_id` guard already prevents double-ringing *the same ticket*; this task prevents a *second ticket* being minted for the same live escalation, which would produce a genuinely separate ring.

- [ ] **Step 1: Read the current implementation**

```bash
sed -n '240,280p' app/services/conversation_service.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/agent_caller/test_escalation_dedup.py`:

```python
"""Re-escalating an already-escalated session must not restart the clock."""

import uuid

from app.db.models.conversation import ConversationSession, SessionStatus
from app.services.conversation_service import ConversationService


def _session(db, tenant_id):
    session = ConversationSession(tenant_id=tenant_id, status=SessionStatus.ACTIVE)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_first_escalation_sets_reason_and_timestamp(db):
    session = _session(db, uuid.uuid4())
    ConversationService(db).escalate_session(session.id, "Refund request")
    db.refresh(session)
    assert session.status == SessionStatus.ESCALATED
    assert session.escalation_reason == "Refund request"
    assert session.escalated_at is not None


def test_second_escalation_preserves_the_original(db):
    session = _session(db, uuid.uuid4())
    svc = ConversationService(db)
    svc.escalate_session(session.id, "Refund request")
    db.refresh(session)
    original_at, original_reason = session.escalated_at, session.escalation_reason

    svc.escalate_session(session.id, "Still angry")
    db.refresh(session)
    assert session.escalated_at == original_at
    assert session.escalation_reason == original_reason
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
poetry run pytest tests/agent_caller/test_escalation_dedup.py -v --no-cov
```
Expected: `test_second_escalation_preserves_the_original` FAILS (the timestamp/reason are overwritten).

- [ ] **Step 4: Add the guard**

In `app/services/conversation_service.py`, at the top of the `escalate_session` body — after the session is loaded and the not-found check runs, before any mutation — insert:

```python
        # Already escalated: the first escalation owns the reason, the timestamp
        # and the ticket. Re-entering this path (customer keeps typing, a second
        # flow fires) must not restart the clock or mint a second escalation.
        if session.status == SessionStatus.ESCALATED:
            return self._to_session_response(session)
```

Match the exact response-construction call the method already uses for its return value — read the tail of `escalate_session` and reuse it verbatim rather than assuming the helper name.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
poetry run pytest tests/agent_caller/test_escalation_dedup.py -v --no-cov
```
Expected: 2 passed.

- [ ] **Step 6: Confirm no conversation regressions**

```bash
poetry run pytest tests/services tests/api/v1 -q --no-cov
```
Expected: same pass/fail counts as `develop`.

- [ ] **Step 7: Format, lint, commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/conversation_service.py tests/agent_caller/test_escalation_dedup.py
git commit -m "fix(agent-caller): guard against re-escalating an escalated session"
```

---

### Task 10: Full verification, SOP doc, PR

**Files:**
- Create: `docs/sop/agent_caller_escalation_push.md`
- Modify: nothing else

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a merged-ready branch and the SOP record.

- [ ] **Step 1: Run the complete suite in Docker**

Per `project_ci_health`, DB-touching tests need the `luran_default` network and the container image:

```bash
docker compose up -d db redis
docker run --rm --network luran_default -e DATABASE_URL="postgresql://luran:luran@db:5432/luran" -v "$PWD":/app -w /app luran-api:latest pytest tests -q --no-cov
```
Expected: no new failures versus `develop`. Record the exact pass/fail counts.

- [ ] **Step 2: Run both lint gates**

```bash
poetry run ruff check app tests && poetry run black --check app tests
```
Expected: both clean. (`ruff` 0.8.6 / `black` 24.10.0 need `pyproject.toml` inside the container if you lint there — see `project_ci_health`.)

- [ ] **Step 3: Verify the OpenAPI surface**

```bash
poetry run python -c "
from app.main import app
paths = sorted(p for p in app.openapi()['paths'] if '/agent' in p)
print('\n'.join(paths))
"
```
Expected — exactly these six:
```
/api/v1/agent/availability
/api/v1/agent/devices
/api/v1/agent/escalations
/api/v1/agent/escalations/{ticket_id}
/api/v1/agent/escalations/{ticket_id}/acknowledge
/api/v1/agent/me
```

- [ ] **Step 4: Write the SOP**

Create `docs/sop/agent_caller_escalation_push.md` matching the house style of the existing files in `docs/sop/` (read two of them first). Cover:
- **What shipped** — push-based escalation ring for the Agent Caller app; list the PR.
- **Why** — email escalation alone leaves escalations sitting unseen; broadcast + one supervisor-fallback timer gives a "never missed" guarantee without an N-tier chain.
- **How** — broadcast/first-accept-wins, the atomic conditional UPDATE as the coordination primitive, the idempotent deferred fallback (no cancellation), three distinct `NotifEvent` values so an admin in both tiers is not deduped, recipient resolution at send time rather than escalation time. Rejected alternatives: a parallel `Escalation` table, an N-tier ordered chain, `google-auth` as a dependency, CallKit/Critical Alerts.
- **What's involved** — the file table from this plan, plus migration `ac30agentcaller` and the new settings.
- **Verification** — test counts from Step 1, the OpenAPI list from Step 3, both lint gates.
- **Operate / roll back** — `AGENT_ALERT_ENABLED=false` disables all pushes instantly with no deploy; `alembic downgrade -1` reverses the schema; `FIREBASE_CREDENTIALS_JSON` must be added to the encrypted `.env.staging` / `.env.production` bundles via `scripts/env.sh` **before** deploy or the worker logs `FCMConfigError` on every send.
- **Follow-ups** — iOS ships only once the APNs auth key is uploaded to Firebase; per-tenant wait window; delivery-failure fast-skip; time-to-acknowledge metrics from `acknowledged_at`.

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs/sop/agent_caller_escalation_push.md
git commit -m "docs(sop): agent caller escalation push"
git push -u origin feat/agent-caller-push
gh pr create --base develop --title "feat(agent-caller): escalation push infrastructure" --body "$(cat <<'EOF'
## Summary
Rings a tenant's available agents on their phones the moment Luran's AI escalates a conversation; the first to tap Take Over claims the live text chat. Supplements — never replaces — the existing email escalation.

Implements `docs/superpowers/specs/2026-08-15-agent-caller-backend-escalation-push-design.md` per plan `docs/superpowers/plans/2026-08-16-agent-caller-backend-push.md`.

## What's in it
- `agent_devices` table, `users.availability_status`, `tickets.acknowledged_at`, `TicketStatus.MISSED`, `NotifChannel.PUSH` (migration `ac30agentcaller`)
- FCM HTTP v1 client (no new dependency — JWT-bearer grant signed with `python-jose`)
- ARQ jobs: broadcast ring, deferred supervisor fallback, silent clear
- `/api/v1/agent/*` router: identity, devices, availability, escalation list/detail, atomic acknowledge
- Ring wired into all six escalation sites in `chat.py`; re-escalation dedup guard

## Contract addendum
Two additive endpoints/behaviours beyond spec §4 (documented in the plan): `GET /api/v1/agent/me` and the absolute `ws_url` shape. The Flutter app builds against these.

## Deploy notes
- Add `FIREBASE_CREDENTIALS_JSON` to the encrypted `.env` bundles **before** deploying.
- `AGENT_ALERT_ENABLED=false` is the kill switch.
- Pilot on weysure before wider rollout.

## Verification
See `docs/sop/agent_caller_escalation_push.md`.
EOF
)"
```

Do **not** add a Claude co-author trailer to any commit.

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3.1 `agent_devices` | 1 |
| §3.2 `users.availability_status` | 1 |
| §3.3 `Ticket.acknowledged_at` + `MISSED` | 1 |
| §4.1 push payloads (alert / clear / missed) | 3 (builders), 4 (delivery) |
| §4.2 REST endpoints | 5, 6, 7 |
| §4.3 WebSocket takeover (unchanged) | — reused as-is; `ws_url` produced in 7 |
| §5 escalation flow | 8 (hook), 4 (jobs), 7 (claim) |
| §5 dedup | 9 |
| §6 Alert Dispatcher, config, `PUSH` channel, worker registration | 2, 3, 4 |
| §7 security (tenant scoping, 404-not-403, tokens never returned/logged) | 5, 6, 7 |
| §8 testing (claim race, fallback timer, dedup, availability, upsert/prune, tenant isolation) | 7, 4, 9, 3, 5, 6 |
| §9 rollout (kill switch, secrets, pilot) | 2, 10 |

Every spec section maps to a task. §4.3 needs no code — the existing WebSocket is reused unchanged, which Task 7 verifies by asserting the emitted `ws_url` matches its real mounted path (`/api/v1/ws/agent/{session_id}`, confirmed at `app/api/v1/api.py:279`).

**Type consistency** — checked across tasks: `AgentDevice.device_token`, `DevicePlatform`, `AvailabilityStatus`, `NotifEvent.ESCALATION_*`, `AgentAlertService` method names, `FCMResult` fields, `_ring_agents`, `_ws_url`, `_owned_ticket`, `_wire_status` are each defined once and referenced with the same signature everywhere.

**Known assumption to verify during Task 5** — `create_access_token`'s exact signature in `app/core/security.py`, and `Tenant`'s required columns. Both are flagged inline in Task 5 Step 1 with the command to check.
