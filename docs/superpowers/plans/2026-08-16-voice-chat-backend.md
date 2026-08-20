# Voice Chat (in-widget) — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax
> for tracking.

**Goal:** Expose Luran's existing chat pipeline to ElevenLabs Agents as an OpenAI-compatible SSE
"custom LLM", plus a gated token-mint endpoint, so a customer can speak to the assistant in the widget
and hear it reply — with full parity to text chat.

**Architecture:** ElevenLabs runs the voice loop (STT → turn-taking → TTS) over WebRTC in the browser.
Its custom-LLM URL points at `POST /api/v1/voice/llm/chat/completions`, which resolves the tenant +
Luran session from a **server-minted opaque voice token**, calls the existing `send_message()` pipeline
function directly, and re-emits the finished reply as OpenAI SSE frames behind a buffer word. Voice
therefore reuses the existing brain, action engine, and escalation path unchanged.

**Tech Stack:** FastAPI, SSE via `StreamingResponse`, SQLAlchemy + Alembic, Redis (`redis.asyncio`),
existing `RAGService` / `ChatAutomationOrchestrator` / `AutomationService` / `TicketService`.

**Spec:** `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
(spikes: `docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`)

**Branch:** `feat/voice-chat-backend`, off `develop`. Never commit to `develop` or `main`.

---

## Global Constraints

- **API version:** additive only, stays on `/api/v1` → all new routes under `/api/v1/voice/*`.
- **No Claude co-author trailer** on any commit. (`feedback_no_coauthor_claude`)
- **Tenant isolation:** every voice endpoint resolves `tenant_id` from the server-minted voice token,
  **never** from a client- or ElevenLabs-supplied field alone.
- **No secrets to the client:** the browser receives only a short-lived ElevenLabs conversation token.
  The ElevenLabs API key and the voice signing secret live in the encrypted `.env` bundles (`scripts/env.sh`).
- **Lint both:** `poetry run black app tests` **and** `poetry run ruff check app tests` must pass.
- **Tests:** `poetry run pytest`. Per `project_ci_health`, the full suite runs in `luran-api:latest` Docker;
  Redis-dependent tests need `FLUSHDB` between full-suite runs.
- **Naming:** lowercase string enums for any status values crossing the API boundary
  (matches the sales/leads contract convention).
- **No new action code:** tool webhooks are thin adapters over `AutomationService.execute_action`.

---

## Spec Corrections (read before starting)

These were discovered by reading the codebase and **override** the spec where they conflict:

1. **Nothing in this codebase streams.** There is no `StreamingResponse`, `astream`, or `stream=True`
   anywhere in `app/`. `send_message` (`app/api/v1/endpoints/chat.py:482`) is a ~2000-line endpoint
   function returning a complete `ChatResponse`. Spec §3's "the pipeline's final LLM call must stream
   tokens through this endpoint" has **no existing seam**. Per user decision (2026-08-16) we call
   `send_message()` directly and adapt its finished result into SSE frames. Buffer words are therefore
   **load-bearing**, not an optimization — they cover the entire pipeline latency.

2. **Tenant/session binding does not rely on ElevenLabs metadata.** Spike 1 (whether EL metadata
   passthrough is trustworthy) has not returned. This plan is written to be **spike-independent**: the
   mint endpoint issues an opaque `voice_token` stored in Redis against `{tenant_id, luran_session_id,
   text_out}`, and the custom-LLM endpoint resolves it server-side. This is strictly safer than trusting
   EL metadata and needs no rework whichever way Spike 1 lands.

3. **Slice 4 (write-action tool webhooks) is gated on Spike 2.** Because `send_message()` runs the action
   engine inline, writes already complete inside the pipeline call — there is no natural seam to emit an
   EL tool call. Whether we need the tool-webhook path at all depends on Spike 2's verdict on tolerable
   dead air. **Do not start Slice 4 until the spike findings land.** Slices 1–3 are fully unblocked.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/core/config.py` (modify) | ElevenLabs + voice settings |
| `alembic/versions/c1a0_add_voice_capabilities.py` (create) | `can_use_voice`, `max_voice_minutes_per_month` on `tenant_capabilities` |
| `app/services/voice/__init__.py` (create) | package marker |
| `app/services/voice/session_tokens.py` (create) | mint/resolve the opaque voice token (Redis) |
| `app/services/voice/gating.py` (create) | flag + minute-cap + concurrency checks; the 409 reasons |
| `app/services/voice/metering.py` (create) | minute counters, monthly rollover, Postgres flush |
| `app/services/voice/elevenlabs_client.py` (create) | thin EL REST client (mint conversation token) |
| `app/services/voice/sse.py` (create) | OpenAI chunk framing + buffer-word prefixing |
| `app/services/voice/auth.py` (create) | EL→Luran request verification (swap point for Spike 1) |
| `app/api/v1/endpoints/voice.py` (create) | all `/voice/*` routes |
| `app/schemas/voice.py` (create) | request/response models |
| `app/api/v1/api.py` (modify) | router include |
| `tests/voice/…` (create) | one test module per service + endpoint |

---

## Slice 1 — Foundations, gating, token mint  *(spike-independent)*

Deliverable: a widget can `POST /api/v1/voice/session/token` and get a real ElevenLabs token, or a
409 with a machine-readable reason. PR + `docs/sop/voice_chat_gating_and_token_mint.md`.

### Task 1: Settings + voice capability migration

**Files:**
- Modify: `app/core/config.py`
- Create: `alembic/versions/c1a0_add_voice_capabilities.py`
- Modify: `app/db/models/tenant.py` (class `TenantCapabilities`, ~line 347)
- Test: `tests/voice/test_voice_capabilities.py`

**Interfaces:**
- Produces: `settings.ELEVENLABS_API_KEY: str | None`, `settings.ELEVENLABS_AGENT_ID: str | None`,
  `settings.VOICE_SHARED_SECRET: str | None`, `settings.VOICE_SESSION_TOKEN_TTL_SECONDS: int` (default `600`),
  `settings.VOICE_MAX_CONCURRENT_SESSIONS: int` (default `20`).
- Produces: `TenantCapabilities.can_use_voice: bool` (default `False`),
  `TenantCapabilities.max_voice_minutes_per_month: int | None` (default `None` = unlimited).

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_voice_capabilities.py
from app.core.config import settings
from app.db.models.tenant import TenantCapabilities


def test_voice_settings_exist_with_safe_defaults():
    assert settings.VOICE_SESSION_TOKEN_TTL_SECONDS == 600
    assert settings.VOICE_MAX_CONCURRENT_SESSIONS == 20
    # Secrets default to None so a missing .env fails closed rather than booting insecure.
    assert hasattr(settings, "ELEVENLABS_API_KEY")
    assert hasattr(settings, "VOICE_SHARED_SECRET")


def test_capabilities_default_voice_off():
    cap = TenantCapabilities()
    assert cap.can_use_voice is False or cap.can_use_voice is None
    assert cap.max_voice_minutes_per_month is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_voice_capabilities.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'VOICE_SESSION_TOKEN_TTL_SECONDS'`

- [ ] **Step 3: Add the settings**

In `app/core/config.py`, inside the `Settings` class, alongside the other integration settings:

```python
    # --- Voice chat (ElevenLabs Agents) -------------------------------------
    ELEVENLABS_API_KEY: str | None = None
    ELEVENLABS_AGENT_ID: str | None = None
    ELEVENLABS_API_BASE: str = "https://api.elevenlabs.io"
    # Shared secret ElevenLabs presents back to us on custom-LLM / tool calls.
    VOICE_SHARED_SECRET: str | None = None
    # Opaque voice-session token lifetime. Short: it is minted per call start.
    VOICE_SESSION_TOKEN_TTL_SECONDS: int = 600
    # Platform-wide simultaneous voice sessions (EL plan cap ~20 Pro / ~40 Business).
    VOICE_MAX_CONCURRENT_SESSIONS: int = 20
```

- [ ] **Step 4: Add the model columns**

In `app/db/models/tenant.py`, in `TenantCapabilities`, after `max_automation_actions`:

```python
    can_use_voice: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default="false",
        comment="Can use in-widget voice chat",
    )

    max_voice_minutes_per_month: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Monthly voice minutes cap (null = unlimited)",
    )
```

- [ ] **Step 5: Write the migration**

```python
# alembic/versions/c1a0_add_voice_capabilities.py
"""add voice capabilities to tenant_capabilities

Revision ID: c1a0
Revises: b7d21c9f3a04
"""

import sqlalchemy as sa
from alembic import op

revision = "c1a0"
down_revision = "b7d21c9f3a04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_capabilities",
        sa.Column(
            "can_use_voice",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "tenant_capabilities",
        sa.Column("max_voice_minutes_per_month", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_voice_minutes_per_month")
    op.drop_column("tenant_capabilities", "can_use_voice")
```

> **NOTE for implementer:** verify `down_revision` matches the current head — run
> `poetry run alembic heads` and use the real head if it is not `b7d21c9f3a04`.

- [ ] **Step 6: Run migration and tests**

Run: `poetry run alembic upgrade head && poetry run pytest tests/voice/test_voice_capabilities.py -v`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/core/config.py app/db/models/tenant.py alembic/versions/c1a0_add_voice_capabilities.py tests/voice/
git commit -m "feat(voice): add voice settings and tenant voice capabilities"
```

---

### Task 2: Opaque voice-session token store

**Files:**
- Create: `app/services/voice/__init__.py` (empty)
- Create: `app/services/voice/session_tokens.py`
- Test: `tests/voice/test_session_tokens.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class VoiceSessionContext:
      tenant_id: UUID
      luran_session_id: UUID
      text_out: bool

  class VoiceSessionTokenStore:
      def __init__(self, redis_url: str | None = None) -> None: ...
      async def mint(self, *, tenant_id: UUID, luran_session_id: UUID, text_out: bool) -> str: ...
      async def resolve(self, token: str) -> VoiceSessionContext | None: ...
      async def revoke(self, token: str) -> None: ...
  ```
- Consumed by: Task 4 (mint endpoint), Task 6 (custom-LLM endpoint), Task 8 (escalate).

This is the component that makes the whole backend **spike-independent**: ElevenLabs only ever carries an
opaque random string; the authoritative `{tenant_id, luran_session_id}` binding lives in our Redis.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_session_tokens.py
import pytest
from uuid import uuid4

from app.services.voice.session_tokens import VoiceSessionTokenStore


@pytest.mark.asyncio
async def test_mint_then_resolve_round_trips_context():
    store = VoiceSessionTokenStore()
    tenant_id, session_id = uuid4(), uuid4()

    token = await store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=True)

    assert isinstance(token, str) and len(token) >= 32
    ctx = await store.resolve(token)
    assert ctx is not None
    assert ctx.tenant_id == tenant_id
    assert ctx.luran_session_id == session_id
    assert ctx.text_out is True


@pytest.mark.asyncio
async def test_resolve_unknown_token_returns_none():
    store = VoiceSessionTokenStore()
    assert await store.resolve("not-a-real-token") is None


@pytest.mark.asyncio
async def test_revoke_makes_token_unresolvable():
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)
    await store.revoke(token)
    assert await store.resolve(token) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_session_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.voice'`

- [ ] **Step 3: Implement**

```python
# app/services/voice/session_tokens.py
"""Opaque, server-minted voice-session tokens.

ElevenLabs carries only the opaque string; the authoritative tenant/session
binding lives here in Redis. This deliberately avoids trusting any metadata
ElevenLabs passes back to us (see spec §9 and Spike 1).
"""

import json
import secrets
from dataclasses import dataclass
from uuid import UUID

import redis.asyncio as aioredis

from app.core.config import settings

_KEY_PREFIX = "voice:session_token:"


@dataclass(frozen=True)
class VoiceSessionContext:
    tenant_id: UUID
    luran_session_id: UUID
    text_out: bool


class VoiceSessionTokenStore:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def mint(self, *, tenant_id: UUID, luran_session_id: UUID, text_out: bool) -> str:
        token = secrets.token_urlsafe(32)
        payload = json.dumps(
            {
                "tenant_id": str(tenant_id),
                "luran_session_id": str(luran_session_id),
                "text_out": text_out,
            }
        )
        redis = await self._get_redis()
        await redis.set(
            _KEY_PREFIX + token,
            payload,
            ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
        )
        return token

    async def resolve(self, token: str) -> VoiceSessionContext | None:
        if not token:
            return None
        redis = await self._get_redis()
        raw = await redis.get(_KEY_PREFIX + token)
        if not raw:
            return None
        data = json.loads(raw)
        return VoiceSessionContext(
            tenant_id=UUID(data["tenant_id"]),
            luran_session_id=UUID(data["luran_session_id"]),
            text_out=bool(data["text_out"]),
        )

    async def revoke(self, token: str) -> None:
        redis = await self._get_redis()
        await redis.delete(_KEY_PREFIX + token)
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/voice/test_session_tokens.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/ tests/voice/test_session_tokens.py
git commit -m "feat(voice): opaque server-minted voice session token store"
```

---

### Task 3: Gating service (flag → cap → concurrency)

**Files:**
- Create: `app/services/voice/gating.py`
- Test: `tests/voice/test_gating.py`

**Interfaces:**
- Consumes: `TenantCapabilities.can_use_voice`, `.max_voice_minutes_per_month` (Task 1).
- Produces:
  ```python
  class VoiceDenialReason(str, Enum):
      FLAG_DISABLED = "voice_disabled"
      MINUTE_CAP_REACHED = "minute_cap_reached"
      CONCURRENCY_LIMIT = "concurrency_limit"

  @dataclass(frozen=True)
  class VoiceGateResult:
      allowed: bool
      reason: VoiceDenialReason | None = None

  class VoiceGate:
      def __init__(self, db: Session, redis_url: str | None = None) -> None: ...
      async def check(self, tenant_id: UUID) -> VoiceGateResult: ...
      async def acquire_slot(self, tenant_id: UUID, token: str) -> None: ...
      async def release_slot(self, tenant_id: UUID, token: str) -> None: ...
  ```
- Consumed by: Task 4 (mint endpoint returns `409` with `reason`).

Order matters and is asserted by the tests: flag, then cap, then concurrency.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_gating.py
import pytest
from uuid import uuid4

from app.services.voice.gating import VoiceDenialReason, VoiceGate


@pytest.mark.asyncio
async def test_flag_disabled_denies_first(db_session, voice_tenant_factory):
    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=0)
    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    # Both flag and cap would fail; flag must win — it is checked first.
    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


@pytest.mark.asyncio
async def test_minute_cap_reached_denies(db_session, voice_tenant_factory, voice_usage_setter):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=10)
    voice_usage_setter(tenant.id, minutes=10)
    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    assert result.reason == VoiceDenialReason.MINUTE_CAP_REACHED


@pytest.mark.asyncio
async def test_concurrency_ceiling_denies(db_session, voice_tenant_factory, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", 1)
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)

    await gate.acquire_slot(tenant.id, "tok-1")
    result = await gate.check(tenant.id)
    assert result.reason == VoiceDenialReason.CONCURRENCY_LIMIT

    await gate.release_slot(tenant.id, "tok-1")
    assert (await gate.check(tenant.id)).allowed is True


@pytest.mark.asyncio
async def test_unlimited_minutes_allowed(db_session, voice_tenant_factory):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)
    assert (await gate.check(tenant.id)).allowed is True
```

- [ ] **Step 2: Add the fixtures**

Append to `tests/conftest.py`:

```python
@pytest.fixture(scope="function")
def voice_tenant_factory(db_session):
    """Create a tenant with specific voice capabilities."""
    from app.db.models.tenant import Tenant, TenantCapabilities

    def _make(*, can_use_voice: bool, max_voice_minutes_per_month: int | None):
        tenant = Tenant(name=f"voice-test-{uuid4().hex[:8]}", slug=f"vt-{uuid4().hex[:8]}")
        db_session.add(tenant)
        db_session.flush()
        db_session.add(
            TenantCapabilities(
                tenant_id=tenant.id,
                can_use_voice=can_use_voice,
                max_voice_minutes_per_month=max_voice_minutes_per_month,
            )
        )
        db_session.commit()
        return tenant

    return _make


@pytest.fixture(scope="function")
def voice_usage_setter():
    """Seed the Redis monthly voice-minute counter for a tenant.

    Synchronous on purpose: `asyncio.run` per call keeps this usable from both
    async tests and the sync TestClient tests. Never use
    `asyncio.get_event_loop()` — it is deprecated and errors on Python 3.12+
    when no loop is running.
    """
    import asyncio

    from app.services.voice.metering import VoiceMeter

    def _set(tenant_id, *, minutes: int):
        asyncio.run(VoiceMeter().add_minutes(tenant_id, minutes))

    return _set
```

> **NOTE:** `voice_usage_setter` depends on `VoiceMeter` from Task 9. Implement the minimal
> `VoiceMeter.add_minutes` / `get_monthly_minutes` pair now as part of this task (they are ~20 lines);
> Task 9 extends the same class with lifecycle metering and the Postgres flush. Check `Tenant`'s real
> required columns in `app/db/models/tenant.py` before writing the factory — add any other non-nullable
> fields it needs.

- [ ] **Step 3: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_gating.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.voice.gating'`

- [ ] **Step 4: Implement**

```python
# app/services/voice/gating.py
"""Voice access gating: tenant flag, monthly minute cap, concurrency ceiling.

Checked in that order at token-mint time (spec §5). Any failure yields a 409
with a machine-readable reason the widget uses to fall back to text.
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.tenant import TenantCapabilities
from app.services.voice.metering import VoiceMeter

_CONCURRENCY_KEY = "voice:concurrent:{tenant_id}"


class VoiceDenialReason(str, Enum):
    FLAG_DISABLED = "voice_disabled"
    MINUTE_CAP_REACHED = "minute_cap_reached"
    CONCURRENCY_LIMIT = "concurrency_limit"


@dataclass(frozen=True)
class VoiceGateResult:
    allowed: bool
    reason: VoiceDenialReason | None = None


class VoiceGate:
    def __init__(self, db: Session, redis_url: str | None = None) -> None:
        self.db = db
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._meter = VoiceMeter(redis_url=self.redis_url)

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _capabilities(self, tenant_id: UUID) -> TenantCapabilities | None:
        return self.db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        ).scalar_one_or_none()

    async def check(self, tenant_id: UUID) -> VoiceGateResult:
        caps = self._capabilities(tenant_id)

        # 1. Feature flag — fail closed when capabilities are missing entirely.
        if caps is None or not caps.can_use_voice:
            return VoiceGateResult(False, VoiceDenialReason.FLAG_DISABLED)

        # 2. Monthly minute cap (null = unlimited).
        if caps.max_voice_minutes_per_month is not None:
            used = await self._meter.get_monthly_minutes(tenant_id)
            if used >= caps.max_voice_minutes_per_month:
                return VoiceGateResult(False, VoiceDenialReason.MINUTE_CAP_REACHED)

        # 3. Concurrency ceiling.
        redis = await self._get_redis()
        active = await redis.scard(_CONCURRENCY_KEY.format(tenant_id=tenant_id))
        if active >= settings.VOICE_MAX_CONCURRENT_SESSIONS:
            return VoiceGateResult(False, VoiceDenialReason.CONCURRENCY_LIMIT)

        return VoiceGateResult(True)

    async def acquire_slot(self, tenant_id: UUID, token: str) -> None:
        redis = await self._get_redis()
        key = _CONCURRENCY_KEY.format(tenant_id=tenant_id)
        await redis.sadd(key, token)
        # Safety net: a dropped call must not hold a slot forever.
        await redis.expire(key, 60 * 60 * 2)

    async def release_slot(self, tenant_id: UUID, token: str) -> None:
        redis = await self._get_redis()
        await redis.srem(_CONCURRENCY_KEY.format(tenant_id=tenant_id), token)
```

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/voice/test_gating.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/gating.py app/services/voice/metering.py tests/voice/test_gating.py tests/conftest.py
git commit -m "feat(voice): tenant gating for flag, minute cap and concurrency"
```

---

### Task 4: ElevenLabs client + token-mint endpoint

**Files:**
- Create: `app/services/voice/elevenlabs_client.py`
- Create: `app/schemas/voice.py`
- Create: `app/api/v1/endpoints/voice.py`
- Modify: `app/api/v1/api.py`
- Test: `tests/voice/test_token_mint.py`

**Interfaces:**
- Consumes: `VoiceGate` (Task 3), `VoiceSessionTokenStore` (Task 2).
- Produces:
  ```python
  # app/schemas/voice.py
  class VoiceTokenRequest(BaseModel):
      tenant_id: UUID
      luran_session_id: UUID
      text_out: bool = False

  class VoiceTokenResponse(BaseModel):
      conversation_token: str
      agent_id: str
      voice_token: str          # our opaque token, echoed back by EL as a dynamic variable
      expires_in_seconds: int

  # app/services/voice/elevenlabs_client.py
  class ElevenLabsClient:
      async def mint_conversation_token(self, *, agent_id: str, dynamic_variables: dict) -> str: ...
  ```
- Route: `POST /api/v1/voice/session/token` → 200 `VoiceTokenResponse` | 409 `{"detail": {"reason": ...}}`

The route is **public** (no auth dependency), matching `POST /chat/message` which the widget already
calls unauthenticated — it is keyed by `tenant_id` + an existing `luran_session_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_token_mint.py
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, patch


def _payload(tenant_id, session_id):
    return {
        "tenant_id": str(tenant_id),
        "luran_session_id": str(session_id),
        "text_out": False,
    }


def test_mint_succeeds_and_returns_tokens(pg_client, voice_tenant_factory, voice_session_factory):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-abc"),
    ):
        resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_token"] == "el-convo-token-abc"
    assert len(body["voice_token"]) >= 32
    assert body["expires_in_seconds"] == 600


def test_mint_409_when_flag_disabled(pg_client, voice_tenant_factory, voice_session_factory):
    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "voice_disabled"


def test_mint_409_when_cap_reached(
    pg_client, voice_tenant_factory, voice_session_factory, voice_usage_setter
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=5)
    session = voice_session_factory(tenant.id)
    voice_usage_setter(tenant.id, minutes=5)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "minute_cap_reached"


def test_mint_rejects_session_from_another_tenant(pg_client, voice_tenant_factory, voice_session_factory):
    tenant_a = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    tenant_b = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_b = voice_session_factory(tenant_b.id)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant_a.id, session_b.id))

    assert resp.status_code == 404
```

- [ ] **Step 2: Add the session fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture(scope="function")
def voice_session_factory(db_session):
    """Create a ConversationSession belonging to a tenant."""
    from app.db.models.conversation import ConversationSession, SessionStatus

    def _make(tenant_id):
        session = ConversationSession(
            tenant_id=tenant_id,
            user_identifier=f"visitor-{uuid4().hex[:8]}",
            status=SessionStatus.ACTIVE,
        )
        db_session.add(session)
        db_session.commit()
        return session

    return _make
```

> **NOTE:** check `ConversationSession`'s required columns in `app/db/models/conversation.py` and add any
> other non-nullable fields.

- [ ] **Step 3: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_token_mint.py -v`
Expected: FAIL — 404 on every request (route does not exist yet)

- [ ] **Step 4: Implement the ElevenLabs client**

```python
# app/services/voice/elevenlabs_client.py
"""Minimal ElevenLabs REST client — only what voice chat needs."""

import httpx
from loguru import logger

from app.core.config import settings


class ElevenLabsMintError(RuntimeError):
    """Raised when ElevenLabs refuses to mint a conversation token."""


class ElevenLabsClient:
    def __init__(self, api_key: str | None = None, api_base: str | None = None) -> None:
        self.api_key = api_key or settings.ELEVENLABS_API_KEY
        self.api_base = (api_base or settings.ELEVENLABS_API_BASE).rstrip("/")

    async def mint_conversation_token(self, *, agent_id: str, dynamic_variables: dict) -> str:
        """Mint a short-lived WebRTC conversation token for the browser.

        The dynamic variables ride along so the custom-LLM endpoint and tool
        webhooks receive our opaque voice_token on every call.
        """
        if not self.api_key:
            raise ElevenLabsMintError("ELEVENLABS_API_KEY is not configured")

        url = f"{self.api_base}/v1/convai/conversation/token"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                params={"agent_id": agent_id},
                headers={"xi-api-key": self.api_key},
            )
        if response.status_code != 200:
            logger.error(f"ElevenLabs token mint failed: {response.status_code} {response.text}")
            raise ElevenLabsMintError(f"ElevenLabs returned {response.status_code}")

        payload = response.json()
        token = payload.get("token")
        if not token:
            raise ElevenLabsMintError("ElevenLabs response contained no token")
        return token
```

> **NOTE:** the exact mint path/param shape is one of the things **Spike 1** confirms. If the spike shows a
> different endpoint, change only this method — nothing else depends on its internals.

- [ ] **Step 5: Implement schemas + route**

```python
# app/schemas/voice.py
from uuid import UUID

from pydantic import BaseModel, Field


class VoiceTokenRequest(BaseModel):
    tenant_id: UUID
    luran_session_id: UUID = Field(..., description="Existing Luran conversation session to continue")
    text_out: bool = Field(default=False, description="Also render assistant replies as text")


class VoiceTokenResponse(BaseModel):
    conversation_token: str
    agent_id: str
    voice_token: str
    expires_in_seconds: int
```

```python
# app/api/v1/endpoints/voice.py
"""Voice chat endpoints — ElevenLabs Agents integration.

Spec: docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md
"""

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.db.models.conversation import ConversationSession
from app.schemas.voice import VoiceTokenRequest, VoiceTokenResponse
from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError
from app.services.voice.gating import VoiceGate
from app.services.voice.session_tokens import VoiceSessionTokenStore

router = APIRouter()


@router.post(
    "/session/token",
    response_model=VoiceTokenResponse,
    status_code=status.HTTP_200_OK,
)
async def mint_voice_session_token(
    payload: VoiceTokenRequest,
    db: Session = Depends(get_db),
):
    """Mint a short-lived ElevenLabs WebRTC token, subject to tenant gating.

    409 with a machine-readable reason means the widget should stay in text.
    """
    if not settings.ELEVENLABS_AGENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "voice_not_configured"},
        )

    # The session must exist AND belong to this tenant — this is the tenant-isolation boundary.
    session = db.execute(
        select(ConversationSession).where(
            ConversationSession.id == payload.luran_session_id,
            ConversationSession.tenant_id == payload.tenant_id,
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "session_not_found"},
        )

    gate = VoiceGate(db)
    result = await gate.check(payload.tenant_id)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": result.reason.value},
        )

    store = VoiceSessionTokenStore()
    voice_token = await store.mint(
        tenant_id=payload.tenant_id,
        luran_session_id=payload.luran_session_id,
        text_out=payload.text_out,
    )

    try:
        conversation_token = await ElevenLabsClient().mint_conversation_token(
            agent_id=settings.ELEVENLABS_AGENT_ID,
            dynamic_variables={"voice_token": voice_token},
        )
    except ElevenLabsMintError as exc:
        await store.revoke(voice_token)
        logger.error(f"Voice token mint failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "voice_provider_unavailable"},
        ) from exc

    await gate.acquire_slot(payload.tenant_id, voice_token)

    return VoiceTokenResponse(
        conversation_token=conversation_token,
        agent_id=settings.ELEVENLABS_AGENT_ID,
        voice_token=voice_token,
        expires_in_seconds=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
    )
```

- [ ] **Step 6: Wire the router**

In `app/api/v1/api.py`, add `voice` to the `from app.api.v1.endpoints import (...)` block, then after the
`chat` include (~line 258):

```python
api_router.include_router(voice.router, prefix="/voice", tags=["voice"])
```

- [ ] **Step 7: Run tests**

Run: `poetry run pytest tests/voice/ -v`
Expected: PASS (all 4 mint tests + earlier tasks)

- [ ] **Step 8: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/elevenlabs_client.py app/schemas/voice.py app/api/v1/endpoints/voice.py app/api/v1/api.py tests/voice/ tests/conftest.py
git commit -m "feat(voice): gated ElevenLabs token mint endpoint"
```

---

### Task 5: Slice 1 SOP

**Files:**
- Create: `docs/sop/voice_chat_gating_and_token_mint.md`

- [ ] **Step 1: Write the SOP**

Follow the existing house style — read `docs/sop/auditor_redirect_canonicalization.md` first and match its
structure. Cover: **What shipped** (gated voice token mint), **Why** (voice needs per-tenant gating before
any minutes are billable), **How** (opaque server-minted token instead of trusting EL metadata — and why:
spike-independence), **What's involved** (the file table above with paths), **Verification** (`pytest
tests/voice/`, the 4 gating cases), **Operate/roll back** (env vars needed; `alembic downgrade -1`;
disabling by flipping `can_use_voice`), **Follow-ups** (Slice 4 spike-gated).

- [ ] **Step 2: Commit**

```bash
git add docs/sop/voice_chat_gating_and_token_mint.md
git commit -m "docs(sop): voice chat gating and token mint"
```

---

## Slice 2 — Custom-LLM SSE endpoint  *(spike-independent)*

Deliverable: ElevenLabs can point its custom-LLM URL at us and get a spoken reply from the real Luran
pipeline. PR + `docs/sop/voice_chat_custom_llm_endpoint.md`.

### Task 6: EL→Luran request verification

**Files:**
- Create: `app/services/voice/auth.py`
- Test: `tests/voice/test_voice_auth.py`

**Interfaces:**
- Produces: `async def verify_elevenlabs_request(request: Request) -> None` — a FastAPI dependency that
  raises `HTTPException(401)` on failure.

**This is the Spike 1 swap point.** Implement the static shared-secret check now; if the spike shows EL
sends a verifiable HMAC, add the HMAC branch *inside this function* — no caller changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_voice_auth.py
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.config import settings
from app.services.voice.auth import verify_elevenlabs_request


def _request(headers: dict) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/voice/llm/chat/completions",
        "headers": Headers(headers).raw,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_accepts_matching_shared_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    await verify_elevenlabs_request(_request({"authorization": "Bearer s3cret"}))


@pytest.mark.asyncio
async def test_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "Bearer WRONG"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_fails_closed_when_secret_unset(monkeypatch):
    """An unconfigured secret must reject everything, not accept everything."""
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", None)
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "Bearer anything"}))
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_voice_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.voice.auth'`

- [ ] **Step 3: Implement**

```python
# app/services/voice/auth.py
"""Verify that an inbound request really came from ElevenLabs.

SPIKE 1 SWAP POINT. Today this is a constant-time shared-secret bearer check,
which is what the ElevenLabs custom-LLM config lets us set. If Spike 1 shows EL
sends a verifiable HMAC signature (e.g. an `xi-signature: t=…,v0=…` header),
add that branch HERE — every caller depends only on this function's contract.
"""

import hmac

from fastapi import HTTPException, Request, status
from loguru import logger

from app.core.config import settings


async def verify_elevenlabs_request(request: Request) -> None:
    """Raise 401 unless the request carries our shared secret.

    Fails closed: an unset secret rejects everything.
    """
    expected = settings.VOICE_SHARED_SECRET
    if not expected:
        logger.error("VOICE_SHARED_SECRET is not configured — rejecting voice request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="voice auth not configured",
        )

    header = request.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else ""

    if not hmac.compare_digest(presented, expected):
        logger.warning("Rejected voice request with invalid ElevenLabs credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/voice/test_voice_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/auth.py tests/voice/test_voice_auth.py
git commit -m "feat(voice): verify inbound ElevenLabs requests, failing closed"
```

---

### Task 7: OpenAI SSE framing + buffer words

**Files:**
- Create: `app/services/voice/sse.py`
- Test: `tests/voice/test_sse.py`

**Interfaces:**
- Produces:
  ```python
  BUFFER_WORD = "... "

  def openai_chunk(*, completion_id: str, model: str, created: int,
                   delta: dict, finish_reason: str | None = None) -> str: ...

  def stream_reply(reply_text: str, *, model: str,
                   buffer_word: bool = True) -> Iterator[str]: ...
  ```
- Consumed by: Task 8.

`stream_reply` yields fully-formed `data: …\n\n` strings, ending with `data: [DONE]\n\n`.
The buffer word **must** end with a trailing space — ElevenLabs needs it for prosody (spec §3).

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_sse.py
import json

from app.services.voice.sse import BUFFER_WORD, stream_reply


def _frames(chunks):
    """Parse SSE strings into the JSON payloads, dropping the [DONE] sentinel."""
    out = []
    for chunk in chunks:
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        body = chunk[len("data: ") : -2]
        if body == "[DONE]":
            continue
        out.append(json.loads(body))
    return out


def test_buffer_word_is_first_content_and_keeps_trailing_space():
    chunks = list(stream_reply("Hello there friend", model="m", buffer_word=True))
    payloads = _frames(chunks)
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    first_content = next(c for c in contents if c)
    assert first_content == BUFFER_WORD
    assert BUFFER_WORD.endswith(" "), "ElevenLabs needs the trailing space for prosody"


def test_stream_ends_with_done_sentinel():
    chunks = list(stream_reply("hi", model="m"))
    assert chunks[-1] == "data: [DONE]\n\n"


def test_full_reply_text_is_preserved():
    reply = "Your meeting is booked for Tuesday at three."
    chunks = list(stream_reply(reply, model="m", buffer_word=False))
    text = "".join(
        p["choices"][0]["delta"].get("content") or "" for p in _frames(chunks)
    )
    assert text.strip() == reply


def test_first_frame_announces_assistant_role():
    payloads = _frames(list(stream_reply("hi", model="m")))
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_finish_reason_stop_present_exactly_once():
    payloads = _frames(list(stream_reply("hi", model="m")))
    stops = [p for p in payloads if p["choices"][0]["finish_reason"] == "stop"]
    assert len(stops) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_sse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.voice.sse'`

- [ ] **Step 3: Implement**

```python
# app/services/voice/sse.py
"""OpenAI-compatible SSE framing for the ElevenLabs custom-LLM endpoint.

Our pipeline does not stream (see plan "Spec Corrections" #1): we get a finished
reply and re-emit it as chunks. The buffer word is therefore load-bearing — it is
the only thing covering pipeline latency before audio starts.
"""

import json
import time
import uuid
from collections.abc import Iterator

# Ellipsis + TRAILING SPACE. The trailing space is required by ElevenLabs so TTS
# has prosody to speak while the rest of the completion arrives (spec §3).
BUFFER_WORD = "... "


def openai_chunk(
    *,
    completion_id: str,
    model: str,
    created: int,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def stream_reply(
    reply_text: str,
    *,
    model: str,
    buffer_word: bool = True,
) -> Iterator[str]:
    """Yield the reply as OpenAI SSE frames, buffer word first."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        return openai_chunk(
            completion_id=completion_id,
            model=model,
            created=created,
            delta=delta,
            finish_reason=finish_reason,
        )

    yield chunk({"role": "assistant"})

    if buffer_word:
        yield chunk({"content": BUFFER_WORD})

    for word in reply_text.split(" "):
        yield chunk({"content": word + " "})

    yield chunk({}, finish_reason="stop")
    yield "data: [DONE]\n\n"
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/voice/test_sse.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/sse.py tests/voice/test_sse.py
git commit -m "feat(voice): OpenAI SSE framing with load-bearing buffer word"
```

---

### Task 8: The custom-LLM endpoint

**Files:**
- Modify: `app/api/v1/endpoints/voice.py`
- Test: `tests/voice/test_custom_llm.py`

**Interfaces:**
- Consumes: `verify_elevenlabs_request` (Task 6), `stream_reply` (Task 7),
  `VoiceSessionTokenStore.resolve` (Task 2), `send_message` + `MessageRequest` from
  `app.api.v1.endpoints.chat` / `app.schemas.conversation`.
- Route: `POST /api/v1/voice/llm/chat/completions` → `text/event-stream`.

The endpoint extracts the **last user message** from the OpenAI `messages` array, resolves the voice token
to a `VoiceSessionContext`, and calls the real pipeline. **Parity is by construction** — this is literally
the same function `/chat/message` calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_custom_llm.py
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.config import settings


def _body(voice_token: str, user_text: str = "what are your hours?") -> dict:
    return {
        "model": "luran-voice",
        "stream": True,
        "messages": [
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": user_text},
        ],
        "elevenlabs_extra_body": {"voice_token": voice_token},
    }


def _fake_chat_response(session_id, text):
    from types import SimpleNamespace

    return SimpleNamespace(
        session_id=session_id,
        message=SimpleNamespace(content=text),
        should_escalate=False,
        escalation_reason=None,
        ticket_id=None,
    )


@pytest.fixture(autouse=True)
def _voice_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")


def _auth():
    return {"Authorization": "Bearer s3cret"}


def test_unauthenticated_request_is_rejected(pg_client):
    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=_body("tok"))
    assert resp.status_code == 401


def test_unknown_voice_token_is_rejected(pg_client):
    resp = pg_client.post(
        "/api/v1/voice/llm/chat/completions", json=_body("bogus"), headers=_auth()
    )
    assert resp.status_code == 401


def test_streams_pipeline_reply_behind_buffer_word(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine.")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.split("\n\n")
        if line.startswith("data: ") and not line.endswith("[DONE]")
    ]
    contents = [f["choices"][0]["delta"].get("content") for f in frames]
    spoken = "".join(c for c in contents if c)
    assert spoken.startswith("... ")
    assert "We open at nine." in spoken


def test_pipeline_receives_the_resolved_tenant_and_session(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """The pipeline must be driven by OUR resolved context, never EL-supplied ids."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "ok"))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        pg_client.post("/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth())

    message_request = mock.call_args.args[0]
    assert message_request.session_id == session.id
    assert message_request.tenant_id == tenant.id
    assert message_request.message == "what are your hours?"


def test_pipeline_error_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A pipeline exception must not leave the caller in silence."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(side_effect=RuntimeError("pipeline exploded")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    assert "sorry" in resp.text.lower()
```

- [ ] **Step 2: Add the minted-token fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture(scope="function")
def minted_voice_token():
    """Mint a real voice token so endpoint tests exercise the true resolve path."""
    import asyncio

    from app.services.voice.session_tokens import VoiceSessionTokenStore

    def _make(tenant_id, session_id, *, text_out: bool = False):
        store = VoiceSessionTokenStore()
        return asyncio.run(
            store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=text_out)
        )

    return _make
```

- [ ] **Step 3: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_custom_llm.py -v`
Expected: FAIL — 404 (route does not exist)

- [ ] **Step 4: Implement**

Add to `app/api/v1/endpoints/voice.py` (extend the imports at the top):

```python
from typing import Any

from fastapi import Body, Request
from fastapi.responses import StreamingResponse

from app.api.v1.endpoints.chat import send_message
from app.schemas.conversation import MessageRequest
from app.services.voice.auth import verify_elevenlabs_request
from app.services.voice.session_tokens import VoiceSessionContext
from app.services.voice.sse import stream_reply

# Spoken when the pipeline fails. Silence is the worst possible failure mode on a
# voice call, so we always return a speakable 200 rather than an error status.
_FALLBACK_REPLY = "I am sorry, I ran into a problem just then. Could you say that again?"


def _extract_voice_token(payload: dict[str, Any]) -> str:
    """Pull our opaque token out of wherever ElevenLabs put it.

    We check several carriers because Spike 1 has not yet pinned down which one
    EL actually uses. All of them are opaque-token carriers, never trusted ids.
    """
    extra = payload.get("elevenlabs_extra_body") or {}
    if isinstance(extra, dict) and extra.get("voice_token"):
        return str(extra["voice_token"])

    variables = payload.get("dynamic_variables") or {}
    if isinstance(variables, dict) and variables.get("voice_token"):
        return str(variables["voice_token"])

    if payload.get("user"):
        return str(payload["user"])

    return ""


def _last_user_message(payload: dict[str, Any]) -> str:
    for message in reversed(payload.get("messages") or []):
        if message.get("role") == "user" and message.get("content"):
            return str(message["content"])
    return ""


@router.post("/llm/chat/completions")
async def voice_custom_llm(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    _: None = Depends(verify_elevenlabs_request),
):
    """OpenAI-compatible custom-LLM endpoint backed by the real Luran pipeline.

    Parity is by construction: this calls the same `send_message` that
    `POST /chat/message` calls. See spec §3.
    """
    model = str(payload.get("model") or "luran-voice")

    token = _extract_voice_token(payload)
    context: VoiceSessionContext | None = await VoiceSessionTokenStore().resolve(token)
    if context is None:
        logger.warning("Voice custom-LLM call carried an unresolvable voice token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown voice session",
        )

    user_text = _last_user_message(payload)
    if not user_text:
        return StreamingResponse(
            iter(list(stream_reply("Sorry, I did not catch that.", model=model))),
            media_type="text/event-stream",
        )

    try:
        chat_response = await send_message(
            MessageRequest(
                message=user_text,
                session_id=context.luran_session_id,
                tenant_id=context.tenant_id,
            ),
            db,
        )
        reply_text = chat_response.message.content
    except Exception as exc:  # noqa: BLE001 — never leave a caller in silence
        logger.exception(f"Voice pipeline failed: {exc}")
        reply_text = _FALLBACK_REPLY

    return StreamingResponse(
        iter(list(stream_reply(reply_text, model=model))),
        media_type="text/event-stream",
    )
```

> **NOTE:** `iter(list(...))` materialises the frames before responding. That is deliberate for v1 — the
> reply is already complete, so there is nothing to stream progressively, and it keeps the DB session
> from being used after the request scope closes. When the streaming refactor lands (follow-ups), this
> becomes a true async generator.

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/voice/ -v`
Expected: PASS (all voice tests)

- [ ] **Step 6: Verify the full suite still passes**

Run: `poetry run pytest -q`
Expected: no new failures vs. the pre-change baseline. Record the baseline first with
`git stash && poetry run pytest -q | tail -3 && git stash pop` if you are unsure.

- [ ] **Step 7: Lint and commit**

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api/v1/endpoints/voice.py tests/voice/ tests/conftest.py
git commit -m "feat(voice): custom-LLM SSE endpoint backed by the chat pipeline"
```

---

### Task 9: Slice 2 SOP

**Files:**
- Create: `docs/sop/voice_chat_custom_llm_endpoint.md`

- [ ] **Step 1: Write it**, matching house style. Must record the two non-obvious decisions and *why*:
  (a) we call `send_message()` directly rather than refactoring it, because nothing in the codebase
  streams and refactoring the product's most critical endpoint to enable voice was judged the larger risk;
  (b) auth + token-carrier extraction are deliberately written as swap points pending Spike 1.
  Include the "operate" note that `VOICE_SHARED_SECRET` must be set or **all** voice requests 401 by design.

- [ ] **Step 2: Commit**

```bash
git add docs/sop/voice_chat_custom_llm_endpoint.md
git commit -m "docs(sop): voice chat custom-LLM endpoint"
```

---

## Slice 3 — Escalation + usage metering  *(spike-independent)*

Deliverable: voice calls can hand off to a human and consume metered minutes. PR +
`docs/sop/voice_chat_escalation_and_metering.md`.

### Task 10: Escalate client-tool handler

**Files:**
- Modify: `app/api/v1/endpoints/voice.py`
- Test: `tests/voice/test_escalate.py`

**Interfaces:**
- Consumes: `ConversationService.escalate_session` (`app/services/conversation_service.py:247`),
  `TicketService.create_ticket` (`app/services/ticket_service.py:96`), `TicketCreate` schema.
- Route: `POST /api/v1/voice/tools/escalate_to_human` → `{"switch_to_text": true, "ticket_id": "..."}`

The widget half of this (ending the EL session, switching UI to text) is the frontend's Task 8.
The tool name `escalate_to_human` is **case-sensitive** and must match the EL Agent config exactly.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_escalate.py
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.db.models.conversation import ConversationSession, SessionStatus


@pytest.fixture(autouse=True)
def _voice_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")


def _auth():
    return {"Authorization": "Bearer s3cret"}


def test_escalation_marks_session_and_returns_switch_signal(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    resp = pg_client.post(
        "/api/v1/voice/tools/escalate_to_human",
        json={"voice_token": token, "reason": "customer asked for a person"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.json()["switch_to_text"] is True
    assert resp.json()["ticket_id"]

    db_session.expire_all()
    refreshed = db_session.get(ConversationSession, session.id)
    assert refreshed.status == SessionStatus.ESCALATED


def test_escalation_requires_valid_voice_token(pg_client):
    resp = pg_client.post(
        "/api/v1/voice/tools/escalate_to_human",
        json={"voice_token": "bogus", "reason": "x"},
        headers=_auth(),
    )
    assert resp.status_code == 401


def test_escalation_is_idempotent_for_one_session(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A repeated tool call must not open a second ticket."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    body = {"voice_token": token, "reason": "help"}

    first = pg_client.post("/api/v1/voice/tools/escalate_to_human", json=body, headers=_auth())
    second = pg_client.post("/api/v1/voice/tools/escalate_to_human", json=body, headers=_auth())

    assert first.json()["ticket_id"] == second.json()["ticket_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/voice/test_escalate.py -v`
Expected: FAIL — 404

- [ ] **Step 3: Implement**

Add to `app/api/v1/endpoints/voice.py`:

```python
from app.schemas.ticket import TicketCreate
from app.services.conversation_service import ConversationService
from app.services.ticket_service import TicketService


@router.post("/tools/escalate_to_human")
async def voice_escalate_to_human(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    _: None = Depends(verify_elevenlabs_request),
):
    """Voice → human handoff. Converts the call to text (spec §7).

    Tool name is case-sensitive and must match the ElevenLabs Agent config.
    """
    context = await VoiceSessionTokenStore().resolve(str(payload.get("voice_token") or ""))
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown voice session"
        )

    reason = str(payload.get("reason") or "Customer requested a human during a voice call")

    conversation_service = ConversationService(db)
    ticket_service = TicketService(db)

    # Idempotent: an already-escalated session reuses its open ticket rather than
    # opening a second one if ElevenLabs retries the tool call.
    existing = ticket_service.get_open_ticket_for_session(context.luran_session_id)
    if existing is not None:
        return {"switch_to_text": True, "ticket_id": str(existing.id)}

    conversation_service.escalate_session(context.luran_session_id, reason)
    ticket = ticket_service.create_ticket(
        TicketCreate(
            tenant_id=context.tenant_id,
            session_id=context.luran_session_id,
            subject="Voice call escalated to human",
            description=reason,
        )
    )

    return {"switch_to_text": True, "ticket_id": str(ticket.id)}
```

> **NOTE:** `TicketService.get_open_ticket_for_session` may not exist — check
> `app/services/ticket_service.py`. If absent, add it there (a `select(Ticket).where(session_id == …,
> status.in_([open statuses]))` returning the newest), with its own unit test. Also check `TicketCreate`'s
> real required fields in `app/schemas/ticket.py` and match them. The caller-app ring
> (`project_agent_caller_app`) is **not built yet** — leave a `# TODO(agent-caller): ring here` comment
> referencing that project rather than inventing an interface.

- [ ] **Step 4: Run tests, lint, commit**

Run: `poetry run pytest tests/voice/test_escalate.py -v` → PASS

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/api/v1/endpoints/voice.py app/services/ticket_service.py tests/voice/test_escalate.py
git commit -m "feat(voice): escalate-to-human tool converts call to text"
```

---

### Task 11: Usage metering + session lifecycle

**Files:**
- Modify: `app/services/voice/metering.py` (extends the minimal version from Task 3)
- Modify: `app/api/v1/endpoints/voice.py`
- Test: `tests/voice/test_metering.py`

**Interfaces:**
- Produces:
  ```python
  class VoiceMeter:
      def __init__(self, redis_url: str | None = None) -> None: ...
      async def add_minutes(self, tenant_id: UUID, minutes: float) -> float: ...
      async def get_monthly_minutes(self, tenant_id: UUID) -> float: ...
  ```
- Route: `POST /api/v1/voice/session/end` — called by the widget on hang-up; releases the concurrency
  slot, revokes the voice token, and meters the elapsed minutes.

Monthly key rollover is achieved by putting `YYYY-MM` in the Redis key — no cron needed.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/test_metering.py
import pytest
from uuid import uuid4

from app.services.voice.metering import VoiceMeter


@pytest.mark.asyncio
async def test_minutes_accumulate():
    meter = VoiceMeter()
    tenant_id = uuid4()
    await meter.add_minutes(tenant_id, 2.5)
    await meter.add_minutes(tenant_id, 1.5)
    assert await meter.get_monthly_minutes(tenant_id) == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_unused_tenant_reports_zero():
    assert await VoiceMeter().get_monthly_minutes(uuid4()) == 0


@pytest.mark.asyncio
async def test_counter_key_is_month_scoped():
    """Rollover is implicit: a new month writes a different key."""
    meter = VoiceMeter()
    tenant_id = uuid4()
    assert "2026-" in meter._key(tenant_id) or meter._key(tenant_id).count("-") >= 1
```

```python
def test_session_end_releases_slot_and_meters(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    import asyncio

    from app.services.voice.metering import VoiceMeter

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    resp = pg_client.post(
        "/api/v1/voice/session/end",
        json={"voice_token": token, "duration_seconds": 90},
    )

    assert resp.status_code == 200
    used = asyncio.run(VoiceMeter().get_monthly_minutes(tenant.id))
    assert used == pytest.approx(1.5)
```

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/voice/test_metering.py -v`
Expected: FAIL

- [ ] **Step 3: Implement the meter**

```python
# app/services/voice/metering.py
"""Per-tenant monthly voice-minute metering.

The month is baked into the Redis key, so rollover needs no scheduled job.
"""

from datetime import datetime, timezone
from uuid import UUID

import redis.asyncio as aioredis

from app.core.config import settings

_KEY = "voice:minutes:{tenant_id}:{month}"
# Keep two months of history so a late flush or an audit query still finds the data.
_TTL_SECONDS = 60 * 60 * 24 * 62


class VoiceMeter:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _key(self, tenant_id: UUID) -> str:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return _KEY.format(tenant_id=tenant_id, month=month)

    async def add_minutes(self, tenant_id: UUID, minutes: float) -> float:
        redis = await self._get_redis()
        key = self._key(tenant_id)
        total = await redis.incrbyfloat(key, minutes)
        await redis.expire(key, _TTL_SECONDS)
        return float(total)

    async def get_monthly_minutes(self, tenant_id: UUID) -> float:
        redis = await self._get_redis()
        raw = await redis.get(self._key(tenant_id))
        return float(raw) if raw else 0.0
```

- [ ] **Step 4: Implement the session-end route**

Add to `app/api/v1/endpoints/voice.py`:

```python
from app.services.voice.metering import VoiceMeter


@router.post("/session/end")
async def end_voice_session(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Called by the widget on hang-up: meter minutes, free the concurrency slot.

    Deliberately unauthenticated like the mint route — the opaque voice token is
    the credential, and the worst a forged call can do is end its own session.
    """
    token = str(payload.get("voice_token") or "")
    store = VoiceSessionTokenStore()
    context = await store.resolve(token)
    if context is None:
        # Already ended or expired — nothing to do, and no information to leak.
        return {"ok": True}

    duration_seconds = float(payload.get("duration_seconds") or 0)
    if duration_seconds > 0:
        await VoiceMeter().add_minutes(context.tenant_id, duration_seconds / 60.0)

    await VoiceGate(db).release_slot(context.tenant_id, token)
    await store.revoke(token)
    return {"ok": True}
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `poetry run pytest tests/voice/ -v` → PASS

```bash
poetry run black app tests && poetry run ruff check app tests
git add app/services/voice/metering.py app/api/v1/endpoints/voice.py tests/voice/test_metering.py
git commit -m "feat(voice): monthly minute metering and session teardown"
```

---

### Task 12: Slice 3 SOP + full-suite verification

- [ ] **Step 1:** Run the full suite in Docker per `project_ci_health`:

```bash
docker exec -w /app -e PYTHONPATH=/app luran_api poetry run pytest -q
```

Record the pass count. If Redis-dependent tests flake, `FLUSHDB` between runs (known issue).

- [ ] **Step 2:** Write `docs/sop/voice_chat_escalation_and_metering.md` in house style. Record that
  minute rollover is key-scoped (no cron), that the concurrency slot has a 2h safety expiry so a dropped
  call cannot leak a slot forever, and that the caller-app ring is a documented TODO pending
  `project_agent_caller_app`.

- [ ] **Step 3: Commit**

```bash
git add docs/sop/voice_chat_escalation_and_metering.md
git commit -m "docs(sop): voice chat escalation and metering"
```

---

## Slice 4 — Write-action tool webhooks  *(BLOCKED on Spike 2 — do not start)*

**Why blocked:** `send_message()` runs the action engine inline, so writes already complete inside the
pipeline call. Introducing EL tool webhooks means *diverting* those writes out of the pipeline — real
added complexity, justified only if Spike 2 shows in-pipeline write latency produces intolerable dead air
that buffer words cannot cover.

**Decision rule once the spike lands:**

| Spike 2 verdict | Action |
|---|---|
| Buffer word covers writes up to ~6s acceptably | **Drop Slice 4.** Writes stay in-pipeline. Record the decision in the Slice 3 SOP and close it out. |
| Dead air unacceptable beyond ~Ns, tool sounds help | Build Slice 4: `POST /api/v1/voice/tools/{tool_name}` over `AutomationService.execute_action` with an idempotency key derived from `luran_session_id + tool + args`, plus the pipeline change to emit a tool call instead of executing the write inline. |
| Dead air unacceptable even at 2s, sounds do not help | Neither — switch to a **spoken pre-ack + deferred confirmation** pattern in the pipeline ("I'm booking that now, I'll confirm by email"). Smaller change, no tool webhooks. |

Write the chosen path up as a new plan slice before implementing; do not improvise it.

---

## Self-Review

**Spec coverage:**

| Spec § | Covered by |
|---|---|
| §3 custom-LLM endpoint | Tasks 6, 7, 8 (with correction #1) |
| §4 write-action webhooks | Slice 4 — deliberately deferred, decision rule documented |
| §5 token mint + gating | Tasks 1, 2, 3, 4 |
| §6 metering + caps | Tasks 3, 11 |
| §7 escalate handler | Task 10 (caller-app ring is a TODO — that project is unbuilt) |
| §8 EL Agent config | Consumed by Task 4; the agent itself is configured manually at integration |
| §9 security | Tasks 2 (opaque token), 6 (fail-closed auth), 4 (tenant/session ownership check) |
| §10 testing | Every task is TDD; Task 12 runs the Docker suite |
| §11 rollout + spikes | Spike rig delivered separately; Slice 4 gated |

**Known gaps, deliberately left:** caller-app ring (blocked on `project_agent_caller_app`); per-tenant
voice-id selection (spec §8 "optionally overridable" — v1 uses one agent voice); EL session-lifecycle
webhook metering (spec §6 offers "session start/end webhook **or** duration on close" — we use duration
on close, which needs no inbound webhook).

**Type consistency:** `VoiceSessionContext` fields (`tenant_id`, `luran_session_id`, `text_out`) are used
identically in Tasks 2, 4, 8, 10, 11. `VoiceDenialReason` values (`voice_disabled`, `minute_cap_reached`,
`concurrency_limit`) are lowercase strings and match the frontend plan's `VoiceDenialReason` union exactly.
`voice_token` is the field name everywhere — mint response, custom-LLM extraction, escalate body, end body.
