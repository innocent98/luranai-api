# Tenant Voice Settings — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a tenant admin turn voice on/off for their own widget, pick the assistant's voice, and read their monthly minute usage — without ever being able to grant themselves the capability.

**Architecture:** Tenant *preference* lives on `TenantAIConfig` (`tenant_ai_configs`); the platform *grant* stays on `TenantCapabilities`. Separate tables make "a tenant cannot self-grant a paid per-minute capability" structural rather than a rule in a code path. `VoiceGate.check` gains a preference step between the grant and the minute cap. Two tenant-self endpoints expose read and write.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Pydantic v2, pytest, Redis (existing metering).

**Spec:** `docs/superpowers/specs/2026-08-21-tenant-voice-settings-design.md`

## Global Constraints

- Branch off `develop`. Do **not** commit to `develop` — senior reviews before merge.
- No `Co-Authored-By: Claude` trailer.
- **Run the voice suite as `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/ -q --no-cov`.** `.env` holds `redis://redis:6379/0`, the Docker-internal hostname, which does not resolve from the host; without the override you get ~17 spurious failures that are **not** regressions. Containers: `docker compose up -d db redis`.
- **Alembic also needs a host override**, for the same reason: `.env` has
  `DATABASE_URL=postgresql://luran_user:...@db:5432/luran_db`, and `db` is the Docker-internal
  hostname. From the host, run migrations as
  `DATABASE_URL="$(grep '^DATABASE_URL' .env | sed 's/^DATABASE_URL=//' | sed 's/@db:/@localhost:/')" poetry run alembic ...`
- **USE A PROPERLY-MIGRATED TEST DATABASE.** The dev DB (`luran_db`) is *partially* applied: it is
  stamped `c1a0`, yet `agent_devices` already exists while `users.availability_status` and
  `tickets.acknowledged_at` do not. So `alembic upgrade head` against it fails on
  `DuplicateTable: agent_devices`, AND any test inserting a `User` or a `Ticket` row fails on the missing
  columns. `PG_DATABASE_URL` in `tests/conftest.py` reads the `DATABASE_URL` env var, so point the suite
  at a clean database instead — the dev DB is shared and must be left alone:
  ```bash
  PW=$(grep -E '^DATABASE_URL' .env | sed 's|.*://luran_user:||' | sed 's|@.*||')
  TESTDB="postgresql://luran_user:${PW}@localhost:5432/luran_test"
  # one-time: docker exec luran_db psql -U luran_user -d postgres -c "CREATE DATABASE luran_test;"
  #           DATABASE_URL="$TESTDB" poetry run alembic upgrade head
  DATABASE_URL="$TESTDB" REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/ -q --no-cov
  ```
  **Against `luran_test` the whole voice suite passes with ZERO failures** — including the three
  `tests/voice/test_escalate.py` tests previously believed pre-existing-broken. They were never broken;
  the dev DB was. Treat any failure there as real.
- **(Superseded) The dev database is drifted** (`alembic_version` says `c1a0` while later tables already exist),
  so `alembic upgrade head` against it fails with `DuplicateTable: relation "agent_devices"
  already exists`. That is pre-existing and NOT caused by this work. Verify migrations against a
  scratch database instead: `CREATE DATABASE migtest_voice`, run the full chain against it, then
  drop it.
- Exactly one genuinely pre-existing full-suite failure: `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`. Baseline before this plan: **1045 passing, 118 voice.**
- `black` wants to reformat ~63 unrelated files (pre-existing drift). Run `black`/`ruff` but **commit only your own files**, reverting unrelated reformatting.
- The denial reason for a tenant-disabled voice is the existing `VoiceDenialReason.FLAG_DISABLED = "voice_disabled"` — do **not** add a new reason string. Reasons cross the API boundary and the shipped widget already handles this value.
- **Tests that cannot fail are the recurring defect on this feature.** For every test, state what broken implementation it would let through. A prior slice shipped tests proving we parsed three request fields ElevenLabs never sends.

---

### Task 1: Schema — tenant voice preference columns

**Files:**
- Modify: `app/db/models/tenant.py` (class `TenantAIConfig`, after `low_confidence_action`)
- Create: `alembic/versions/<rev>_add_tenant_voice_preference.py`
- Test: `tests/voice/test_tenant_voice_preference.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TenantAIConfig.voice_enabled: bool` (default `True`, NOT NULL) and `TenantAIConfig.voice_id: str | None` (nullable, `String(64)`). Tasks 2–4 read both.

- [ ] **Step 1: Confirm the real alembic head**

Run: `poetry run alembic heads`
Expected: `d1e2mergevoicecaller (head)`. **Use whatever this prints as `down_revision`** — a guessed head silently forks the chain, which has already happened once on this feature.

- [ ] **Step 2: Write the failing test**

Create `tests/voice/test_tenant_voice_preference.py`:

```python
from app.db.models.tenant import TenantAIConfig
from tests.conftest import _bare_voice_tenant as _tenant


def test_voice_preference_defaults_to_enabled_with_no_voice_chosen(db_session):
    """Defaults matter: granting a tenant voice must make it work immediately.

    Would fail if `voice_enabled` defaulted to False (voice granted but silently
    dead) or if `voice_id` defaulted to a string (a fake id reaching the mint).
    Flushes through the session so the DB server_default is exercised, not just
    the Python-side default.
    """
    tenant = _tenant(db_session)
    config = TenantAIConfig(tenant_id=tenant.id)
    db_session.add(config)
    db_session.flush()
    db_session.refresh(config)

    assert config.voice_enabled is True
    assert config.voice_id is None
```

- [ ] **Step 3: Run it and watch it fail**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_tenant_voice_preference.py -q --no-cov`
Expected: FAIL — `AttributeError` / `TypeError` on the unknown `voice_enabled`.

- [ ] **Step 4: Add the columns**

In `app/db/models/tenant.py`, inside `class TenantAIConfig`, immediately after the `low_confidence_action` column:

```python
    # Voice chat (in-widget). NOT `brand_voice` above — that is AI tone.
    # This is the tenant's own on/off for voice chat plus their chosen
    # ElevenLabs voice. Deliberately here and NOT on TenantCapabilities:
    # capabilities are what the PLATFORM GRANTS, and keeping preference on a
    # separate table means the tenant-facing write path has no reference to
    # the grant at all.
    voice_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
        comment="Tenant's own on/off for voice chat (grant is TenantCapabilities.can_use_voice)",
    )
    voice_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Chosen ElevenLabs voice id; null = platform default",
    )
```

Default is **True** so that granting a tenant voice makes it work immediately — the admin grant is the deliberate act, and the toggle exists to opt *out*.

- [ ] **Step 5: Generate and edit the migration**

Run: `poetry run alembic revision -m "add tenant voice preference"`

Edit the generated file so `down_revision` is the head from Step 1, and the body is:

```python
def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "voice_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Tenant's own on/off for voice chat (grant is TenantCapabilities.can_use_voice)",
        ),
    )
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "voice_id",
            sa.String(length=64),
            nullable=True,
            comment="Chosen ElevenLabs voice id; null = platform default",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "voice_id")
    op.drop_column("tenant_ai_configs", "voice_enabled")
```

`server_default=sa.text("true")` matters: existing rows must backfill to enabled, not NULL.

- [ ] **Step 6: Apply it and round-trip it**

Run: `poetry run alembic upgrade head && poetry run alembic downgrade -1 && poetry run alembic upgrade head`
Expected: all three succeed with no error. This proves the downgrade is real, not decorative.

- [ ] **Step 7: Run the test and watch it pass**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_tenant_voice_preference.py -q --no-cov`
Expected: PASS.

- [ ] **Step 8: Prove the test can fail**

Temporarily change the model default to `default=False`, re-run, confirm FAIL, then revert. Record the output in your report.

- [ ] **Step 9: Commit**

```bash
git add app/db/models/tenant.py alembic/versions/ tests/voice/test_tenant_voice_preference.py
git commit -m "feat(voice): tenant voice preference columns on tenant_ai_configs"
```

---

### Task 2: Gate — honour the tenant preference

**Files:**
- Modify: `app/services/voice/gating.py` (`VoiceGate._ai_config` new, `VoiceGate.check`)
- Test: `tests/voice/test_gating.py` (append)

**Interfaces:**
- Consumes: `TenantAIConfig.voice_enabled` from Task 1.
- Produces: `VoiceGate.check` now denies with `VoiceDenialReason.FLAG_DISABLED` when the tenant has switched voice off. No signature change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/voice/test_gating.py`:

```python
async def test_tenant_disabled_voice_is_denied_even_when_granted(
    db_session, voice_tenant_factory
):
    """The whole point of the preference: granted, but the tenant said no.

    Would fail if the preference check were missing entirely (the tenant would
    be allowed) — which is the defect this task exists to prevent.
    """
    from app.db.models.tenant import TenantAIConfig

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    db_session.add(TenantAIConfig(tenant_id=tenant.id, voice_enabled=False))
    db_session.commit()

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


async def test_ungranted_tenant_is_denied_by_the_grant_not_the_preference(
    db_session, voice_tenant_factory
):
    """Ordering is load-bearing: the grant is checked FIRST.

    A tenant with no grant but preference ON must still be denied. Would fail
    if the two checks were swapped, or if the preference check short-circuited
    the grant check.
    """
    from app.db.models.tenant import TenantAIConfig

    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    db_session.add(TenantAIConfig(tenant_id=tenant.id, voice_enabled=True))
    db_session.commit()

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


async def test_missing_ai_config_row_does_not_block_a_granted_tenant(
    db_session, voice_tenant_factory
):
    """A tenant with no TenantAIConfig row at all must still get voice.

    The preference defaults to enabled, so absence means 'not opted out', not
    'disabled'. Would fail if the lookup treated a missing row as False —
    which would silently kill voice for every tenant lacking that row.
    """
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is True
```

- [ ] **Step 2: Run them and watch them fail**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_gating.py -q --no-cov -k "tenant_disabled or ungranted_tenant or missing_ai_config"`
Expected: `test_tenant_disabled_voice_is_denied_even_when_granted` FAILS (`allowed is True`). The other two pass already — that is correct and expected; they are regression guards for the ordering you are about to touch.

- [ ] **Step 3: Add the config lookup**

In `app/services/voice/gating.py`, add the `TenantAIConfig` import alongside the existing `TenantCapabilities` import, then add this method directly below `_capabilities`:

```python
    def _voice_enabled_by_tenant(self, tenant_id: UUID) -> bool:
        """The tenant's own on/off, independent of the platform grant.

        A missing row means 'never configured', which is NOT 'disabled' — the
        column defaults to enabled so that granting voice makes it work
        immediately. Returning False here would silently kill voice for every
        tenant without a TenantAIConfig row.
        """
        config = self.db.execute(
            select(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        return True if config is None else config.voice_enabled
```

- [ ] **Step 4: Insert the check into `check()`**

In `VoiceGate.check`, between the feature-flag block and the minute-cap block:

```python
        # 2. Tenant's own preference. AFTER the grant so an ungranted tenant is
        # never told anything about their preference, and BEFORE the cap so a
        # disabled tenant costs no Redis read. Reuses FLAG_DISABLED because the
        # reason crosses the API boundary and the shipped widget already handles
        # that value with the right copy.
        if not self._voice_enabled_by_tenant(tenant_id):
            return VoiceGateResult(False, VoiceDenialReason.FLAG_DISABLED)
```

Renumber the following comments (`# 2. Monthly minute cap` → `# 3.`, `# 3. Concurrency ceiling` → `# 4.`).

- [ ] **Step 5: Run the whole voice suite**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/ -q --no-cov`
Expected: PASS, 121 tests (118 baseline + 3 new).

- [ ] **Step 6: Prove the new test can fail**

Comment out the preference check from Step 4, re-run, confirm `test_tenant_disabled_voice_is_denied_even_when_granted` FAILS, restore. Record the output.

- [ ] **Step 7: Commit**

```bash
git add app/services/voice/gating.py tests/voice/test_gating.py
git commit -m "feat(voice): gate honours the tenant's own voice on/off preference"
```

---

### Task 3: Curated voice list + read endpoint

**Files:**
- Create: `app/services/voice/voices.py`
- Modify: `app/schemas/tenant.py` (append schemas)
- Modify: `app/api/v1/endpoints/tenant.py` (append route)
- Test: `tests/voice/test_tenant_voice_settings.py`

**Interfaces:**
- Consumes: Task 1's columns; `VoiceMeter.get_monthly_minutes(tenant_id) -> float`.
- Produces: `GET /api/v1/tenant/voice/settings` returning `VoiceSettingsResponse`; `CURATED_VOICES: list[CuratedVoice]` and `is_curated_voice_id(voice_id: str) -> bool` from `app/services/voice/voices.py` (Task 4 uses the latter).

- [ ] **Step 1: Create the curated voice module**

Create `app/services/voice/voices.py`:

```python
"""Curated ElevenLabs voices offered to tenants.

Deliberately a fixed list rather than a live proxy of the ElevenLabs voices
API: predictable quality and cost, no third-party call on a settings page
load, and every voice vetted for support conversations.

TODO(voice-ids): the `id` values below are PLACEHOLDERS and MUST be replaced
with real ids from our ElevenLabs account library before this ships. Fabricated
ids look plausible and fail only at mint time, far from the mistake. Pick
voices spanning gender, accent and pace, audition each one, and record which
you chose and why in the SOP.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedVoice:
    id: str
    name: str
    description: str
    preview_url: str


CURATED_VOICES: list[CuratedVoice] = [
    CuratedVoice(
        id="REPLACE_ME_voice_1",
        name="Placeholder One",
        description="Replace with a real ElevenLabs voice before shipping",
        preview_url="https://storage.googleapis.com/eleven-public-prod/REPLACE_ME.mp3",
    ),
]


def is_curated_voice_id(voice_id: str) -> bool:
    """True when `voice_id` is one we offer. Used to reject arbitrary ids at
    write time rather than letting them fail at mint time."""
    return any(v.id == voice_id for v in CURATED_VOICES)
```

**This is the one intentional placeholder in this plan.** Do not invent ids — flag it in your report so the reviewer sees it is outstanding.

- [ ] **Step 2: Add the schemas**

Append to `app/schemas/tenant.py`:

```python
class CuratedVoiceResponse(BaseModel):
    id: str
    name: str
    description: str
    preview_url: str


class VoiceSettingsResponse(BaseModel):
    granted: bool = Field(..., description="Platform grant (TenantCapabilities.can_use_voice)")
    enabled: bool = Field(..., description="Tenant's own on/off")
    voice_id: str | None = Field(None, description="Chosen voice; null = platform default")
    minutes_used: float | None = Field(
        None, description="Minutes used this month; null when usage is unavailable"
    )
    minutes_limit: int | None = Field(None, description="Monthly cap; null = unlimited")
    available_voices: list[CuratedVoiceResponse]


class VoiceSettingsUpdate(BaseModel):
    enabled: bool
    voice_id: str | None = None
```

`VoiceSettingsUpdate` has **no** capability fields. That absence is the security property — it is why the endpoint has no code path to the grant.

- [ ] **Step 3: Write the failing tests**

Create `tests/voice/test_tenant_voice_settings.py`:

```python
import pytest
from uuid import uuid4

from app.core.security import create_access_token
from app.db.models.tenant import TenantAIConfig, TenantCapabilities
from app.db.models.user import User, UserRole
from tests.conftest import _bare_voice_tenant as _tenant


@pytest.fixture
def voice_admin(db_session):
    """A tenant admin plus their tenant — the pair the endpoints need.

    `regular_user_token` returns only a token, so tests using it cannot set up
    or assert on the tenant. This returns both.
    """

    def _make(*, can_use_voice: bool, max_voice_minutes_per_month: int | None = None):
        tenant = _tenant(db_session)
        db_session.add(
            TenantCapabilities(
                tenant_id=tenant.id,
                can_use_voice=can_use_voice,
                max_voice_minutes_per_month=max_voice_minutes_per_month,
            )
        )
        user = User(
            email=f"admin_{uuid4().hex[:8]}@example.com",
            hashed_password="$2b$12$placeholder_hash_not_real",
            full_name="Voice Admin",
            role=UserRole.TENANT_ADMIN,
            tenant_id=tenant.id,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        token = create_access_token(
            subject=str(user.id),
            additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
        )
        return tenant, token

    return _make


def test_get_returns_grant_preference_and_usage(pg_client, db_session, voice_admin, voice_usage_setter):
    """One call gives the page everything it needs.

    Would fail if `granted` were sourced from the preference (or vice versa) —
    the two must not be conflated, since that conflation is exactly what would
    let a tenant appear to self-grant.
    """
    tenant, token = voice_admin(can_use_voice=True, max_voice_minutes_per_month=500)
    voice_usage_setter(tenant.id, minutes=142)

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granted"] is True
    assert body["enabled"] is True
    assert body["voice_id"] is None
    assert body["minutes_limit"] == 500
    assert body["minutes_used"] == pytest.approx(142, abs=1)
    assert len(body["available_voices"]) >= 1


def test_get_returns_every_field_even_when_not_granted(pg_client, voice_admin):
    """The response shape is unconditional — the page decides what to render.

    Would fail if the endpoint omitted fields when `granted` is false, which
    would make the response shape conditional and harder to consume.
    """
    _, token = voice_admin(can_use_voice=False)

    resp = pg_client.get(
        "/api/v1/tenant/voice/settings", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["granted"] is False
    assert "enabled" in body
    assert "minutes_used" in body
    assert "available_voices" in body


def test_get_requires_authentication(pg_client):
    """Would fail if the route were registered without the tenant-admin dependency."""
    resp = pg_client.get("/api/v1/tenant/voice/settings")
    assert resp.status_code in (401, 403)
```

- [ ] **Step 4: Run them and watch them fail**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_tenant_voice_settings.py -q --no-cov`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 5: Implement the endpoint**

In `app/api/v1/endpoints/tenant.py`, add imports (`TenantAIConfig`, the three new schemas, `CURATED_VOICES`, `VoiceMeter`) and append:

```python
@router.get(
    "/voice/settings",
    response_model=VoiceSettingsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_my_voice_settings(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """Everything the tenant's Voice Chat settings page needs, in one call.

    Always returns every field regardless of `granted` — the endpoint reports
    state and the page decides what to render. A conditional shape would be
    harder to type on the client and gives the page less than it needs.
    """
    tenant_service = TenantService(db)
    capabilities = tenant_service.get_capabilities(current_user.tenant_id)

    config = (
        db.query(TenantAIConfig)
        .filter(TenantAIConfig.tenant_id == current_user.tenant_id)
        .first()
    )

    # Usage must DEGRADE, not fail. A settings page that 500s because a cache
    # is down is worse than one missing a number.
    try:
        minutes_used = await VoiceMeter().get_monthly_minutes(current_user.tenant_id)
    except Exception:  # noqa: BLE001 — usage is informational, never load-bearing
        logger.warning("Voice usage read failed for tenant %s", current_user.tenant_id)
        minutes_used = None

    return VoiceSettingsResponse(
        granted=bool(capabilities and capabilities.can_use_voice),
        enabled=True if config is None else config.voice_enabled,
        voice_id=config.voice_id if config else None,
        minutes_used=minutes_used,
        minutes_limit=capabilities.max_voice_minutes_per_month if capabilities else None,
        available_voices=[CuratedVoiceResponse(**vars(v)) for v in CURATED_VOICES],
    )
```

Add `import logging` / `logger = logging.getLogger(__name__)` at module top if not already present.

- [ ] **Step 6: Run the tests and watch them pass**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_tenant_voice_settings.py -q --no-cov`
Expected: PASS, 3 tests.

- [ ] **Step 7: Prove the degradation path**

Temporarily point `REDIS_URL` at a dead port (`redis://localhost:6399/0`) and re-run `test_get_returns_grant_preference_and_usage`. Expected: still `200`, with `minutes_used` null (the usage assertion will fail — that is fine and expected; you are proving the endpoint does not 500). Restore. Record the output.

- [ ] **Step 8: Commit**

```bash
git add app/services/voice/voices.py app/schemas/tenant.py app/api/v1/endpoints/tenant.py tests/voice/test_tenant_voice_settings.py
git commit -m "feat(voice): tenant voice settings read endpoint with curated voices"
```

---

### Task 4: Write endpoint

**Files:**
- Modify: `app/api/v1/endpoints/tenant.py` (append route)
- Test: `tests/voice/test_tenant_voice_settings.py` (append)

**Interfaces:**
- Consumes: `VoiceSettingsUpdate`, `is_curated_voice_id` from Task 3.
- Produces: `PUT /api/v1/tenant/voice/settings` returning `VoiceSettingsResponse`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/voice/test_tenant_voice_settings.py`:

```python
def test_put_updates_preference_and_voice(pg_client, db_session, voice_admin):
    """Would fail if the write silently no-opped — assert the stored row, not
    just the response body."""
    tenant, token = voice_admin(can_use_voice=True)
    voice_id = CURATED_VOICES[0].id

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False, "voice_id": voice_id},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    config = (
        db_session.query(TenantAIConfig)
        .filter(TenantAIConfig.tenant_id == tenant.id)
        .first()
    )
    assert config.voice_enabled is False
    assert config.voice_id == voice_id


def test_put_cannot_grant_the_capability(pg_client, db_session, voice_admin):
    """THE security test. A tenant must never self-grant a paid capability.

    Posts `can_use_voice` in the body and asserts the grant is unchanged. The
    schema has no such field, so this is a regression guard against someone
    adding one — it would fail the moment `can_use_voice` became writable here.
    """
    tenant, token = voice_admin(can_use_voice=False)

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True, "voice_id": None, "can_use_voice": True},
    )

    assert resp.status_code == 200
    db_session.expire_all()
    caps = (
        db_session.query(TenantCapabilities)
        .filter(TenantCapabilities.tenant_id == tenant.id)
        .first()
    )
    assert caps.can_use_voice is False


def test_put_rejects_a_voice_id_we_do_not_offer(pg_client, voice_admin):
    """Reject at write time, not mint time — far from the mistake is far too late.

    Would fail if validation were missing: an arbitrary id would be stored and
    then fail on a live call."""
    _, token = voice_admin(can_use_voice=True)

    resp = pg_client.put(
        "/api/v1/tenant/voice/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True, "voice_id": "not-a-voice-we-offer"},
    )

    assert resp.status_code == 422
```

Add `CURATED_VOICES` to that file's imports.

- [ ] **Step 2: Run them and watch them fail**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/test_tenant_voice_settings.py -q --no-cov -k put`
Expected: FAIL — 405/404, no PUT route.

- [ ] **Step 3: Implement**

Append to `app/api/v1/endpoints/tenant.py`:

```python
@router.put(
    "/voice/settings",
    response_model=VoiceSettingsResponse,
    status_code=status.HTTP_200_OK,
)
async def update_my_voice_settings(
    payload: VoiceSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """Update the tenant's OWN voice preference.

    This endpoint has no code path that reads or writes TenantCapabilities.
    A tenant can switch their own voice off and on and choose a voice; they
    cannot grant themselves the capability. `enabled` is writable even when
    ungranted — a tenant may pre-configure, and the gate still refuses to mint.
    """
    if payload.voice_id is not None and not is_curated_voice_id(payload.voice_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown voice id",
        )

    config = (
        db.query(TenantAIConfig)
        .filter(TenantAIConfig.tenant_id == current_user.tenant_id)
        .first()
    )
    if config is None:
        config = TenantAIConfig(tenant_id=current_user.tenant_id)
        db.add(config)

    config.voice_enabled = payload.enabled
    config.voice_id = payload.voice_id
    db.commit()

    return await get_my_voice_settings(db=db, current_user=current_user)
```

- [ ] **Step 4: Run the whole voice suite**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/ -q --no-cov`
Expected: PASS, 127 tests (121 after Task 2, plus 3 from Task 3 and 3 here).

- [ ] **Step 5: Prove the security test can fail**

Temporarily add `can_use_voice: bool | None = None` to `VoiceSettingsUpdate` and a line setting it on capabilities. Re-run `test_put_cannot_grant_the_capability`, confirm FAIL, then revert **both** edits. Record the output — this is the most important break-it evidence in the plan.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/tenant.py tests/voice/test_tenant_voice_settings.py
git commit -m "feat(voice): tenant voice settings write endpoint"
```

---

### Task 5: SOP, PRD entries, checklist

**Files:**
- Create: `docs/sop/tenant_voice_settings.md`
- Modify: `../docs/luran_prd.md` (add Features 3.9 and 3.10)
- Modify: the master checklist at the workspace `docs/checklist/`

- [ ] **Step 1: Run the full suite and record real numbers**

Run: `REDIS_URL=redis://localhost:6379/0 poetry run pytest -q --no-cov`
Expected: 1 failed (the known auditor one), ~1054 passed. **Use the numbers you actually observe** — do not copy these.

- [ ] **Step 2: Write the SOP**

Cover: what shipped with commit refs; why the preference lives on `TenantAIConfig` and not `TenantCapabilities`; the gate's new order and why `FLAG_DISABLED` is reused; that the write endpoint has no path to the grant and how that is tested; **which curated voices were chosen and why**; the usage-degrades-not-fails decision; files touched; verification; rollback.

Get the commit range right: `A..B` **excludes** A. Run the command and confirm it covers every file you claim. That exact mistake has produced a broken rollback instruction twice on this feature.

- [ ] **Step 3: Add the PRD entries**

`docs/luran_prd.md` has zero voice mentions. Add `### Feature 3.9 — Voice Chat` and `### Feature 3.10 — Voice Settings & Usage` in the same style as the surrounding `Feature 3.x` entries (What it does / Trigger / How it works).

- [ ] **Step 4: Update the master checklist**

- [ ] **Step 5: Commit**

```bash
git add docs/sop/tenant_voice_settings.md ../docs/luran_prd.md
git commit -m "docs(voice): tenant voice settings SOP and PRD entries"
```

---

## Self-Review

**Spec coverage:** §1 columns → Task 1. §2 gate order → Task 2. §3 endpoints → Tasks 3–4. §4 curated list → Task 3 (with an explicit, flagged placeholder for the real ids). §5 frontend → the separate dashboard plan. §6 testing → embedded per task. §7 slices → Tasks 1–2 are slice 1, Tasks 3–4 are slice 2. §8 out-of-scope → nothing here strays into pricing or plan-tier work. PRD note → Task 5.

**Placeholders:** one deliberate and flagged — the curated voice ids in `voices.py`, which cannot be invented and must come from the real ElevenLabs library.

**Type consistency:** `voice_enabled`/`voice_id` identical across Tasks 1–4. `VoiceSettingsResponse`/`VoiceSettingsUpdate`/`CuratedVoiceResponse` defined in Task 3 and used unchanged in Task 4. `is_curated_voice_id` defined in Task 3, used in Task 4. `VoiceDenialReason.FLAG_DISABLED` matches the existing enum verbatim.
