# SOP — Tenant voice settings: preference storage, gate ordering, GET/PUT endpoints

**Status:** Shipped on branch `feat/tenant-voice-settings` (not yet merged) · commits `161d41a`
through `afb2eda` (`git log 9ca1d54..afb2eda` for the full range — `9ca1d54` is the plan-writing
commit itself and is excluded) · 2026-08-21 → 2026-08-22
**Area:** `app/db/models/tenant.py`, `app/services/voice/gating.py`, `app/services/voice/voices.py`,
`app/api/v1/endpoints/tenant.py`, `app/schemas/tenant.py`, one migration

---

## What shipped

Lets a tenant admin see and control their own voice chat: whether it's on, which of a curated set
of ElevenLabs voices it uses, and how many minutes they've used this month against their platform
grant. Two new columns on `tenant_ai_configs` (`voice_enabled`, `voice_id`), the gate's `check()`
now honours that preference as its own step, and a `GET`/`PUT /api/v1/tenant/voice/settings` pair
the tenant dashboard's Voice Settings page will read and write.

This sits on top of the voice-chat-backend branch's gating/mint pipeline (`docs/sop/voice_chat_*`)
— it does not change the mint flow's mechanics, only adds one more thing the gate checks and a
tenant-facing surface to control it.

Four tasks, two slices:

| Slice | Task | Commits (`A..B`, A excluded) | What it covers |
|---|---|---|---|
| 1 — schema + gate | T1 — preference columns | `161d41a..63c080c` | `voice_enabled`/`voice_id` on `TenantAIConfig`, migration `5a7851d2e0c2` |
| 1 — schema + gate | T2 — gate honours preference | `63c080c..a956566` | `VoiceGate.check()` order: grant → preference → cap → concurrency |
| 2 — endpoints | T3 — read endpoint | `85160a7..825439e` | `GET /voice/settings`: grant, preference, usage, curated voice list |
| 2 — endpoints | T4 — write endpoint | `825439e..afb2eda` | `PUT /voice/settings`: writes only `enabled` + `voice_id` |

(`85160a7`, between T2 and T3, is a standalone docs commit pointing the test suite at a correctly
migrated database — see Verification. It isn't part of either task's diff.)

## Why

The dashboard needs a tenant-controllable on/off and voice choice that is genuinely separate from
the platform's paid grant (`TenantCapabilities.can_use_voice`) — a tenant must be able to turn
voice off for their own widget without that write path ever touching, or being able to touch,
whether they're allowed to use it at all.

## How it works — four decisions worth explaining

### 1. The preference lives on `TenantAIConfig`, not `TenantCapabilities`

`TenantCapabilities` is what the **platform grants** — `can_use_voice`, `max_voice_minutes_per_month`,
written only by platform-admin paths. A preference is not a grant, and putting it on a different
table makes "a tenant cannot self-grant a paid per-minute capability" a structural property of the
schema rather than a rule some code path has to remember to enforce. Concretely, two things fall
out of this for free:

- The tenant-facing write path (`update_my_voice_settings`, `app/api/v1/endpoints/tenant.py:250-279`)
  has no query, no import, no code path that touches `TenantCapabilities` at all — there's nothing
  to forget to guard.
- `_update_capabilities_for_tier` (the plan-tier automation that rewrites a tenant's
  `TenantCapabilities` row on plan change) can never clobber a tenant's voice on/off or voice
  choice, because they aren't on the row it writes.

`app/db/models/tenant.py:300-317` carries this rationale directly in the model comment.

### 2. The gate reuses `voice_disabled` rather than adding a new reason string

`VoiceGate.check()` (`app/services/voice/gating.py:114-141`) now has four steps in order: grant →
preference → monthly cap → concurrency. Step 2, the new one, returns
`VoiceDenialReason.FLAG_DISABLED` (`"voice_disabled"`) — the same reason the grant check itself
returns, not a new `PREFERENCE_DISABLED` value (`gating.py:121-127`). Denial reasons cross the API
boundary to the widget, and the shipped widget (voice-chat-backend branch) already has copy and
fallback behaviour keyed on `voice_disabled`. A tenant who turned voice off and a tenant who was
never granted it should both get "voice chat is not available here" — the widget doesn't need to
distinguish the two, and reusing the reason means it doesn't have to be taught a new value.

### 3. `voice_enabled` defaults `True`, `can_use_voice` defaults `False` — this is deliberate, not inconsistent

Grant closed, preference open. `TenantCapabilities.can_use_voice` defaults `False` because granting
a paid capability is a deliberate platform act — nobody gets billable voice minutes by accident.
`TenantAIConfig.voice_enabled` defaults `True` (`server_default="true"`,
`alembic/versions/5a7851d2e0c2_add_tenant_voice_preference.py:26-31`) because the toggle exists to
let a tenant opt **out**, not in — once a tenant is granted, voice should work immediately without
a second manual step, and a missing `TenantAIConfig` row must not silently kill voice for every
existing tenant (`gating.py:104-112` states this explicitly: "a missing row means 'never
configured', which is NOT 'disabled'"). This is also what makes a future plan-tier auto-grant
(flip `can_use_voice` for a tier) work with zero further change to this code — the preference is
already open by default, so the grant alone is what turns voice on end-to-end.

### 4. A trap: `VoiceSettingsUpdate` has no `extra="forbid"`, and that's load-bearing

Every other `*Update` schema in `app/schemas/tenant.py` — `TenantUpdate`, `TenantFeaturedUpdate`,
`TenantAIConfigUpdate`, `TenantCapabilitiesUpdate` — declares `model_config =
ConfigDict(extra="forbid")`. `VoiceSettingsUpdate` (`app/schemas/tenant.py:342-344`) does not. This
looks exactly like an oversight someone will "fix" for consistency the next time they're in this
file. **Don't.** The security test `test_put_cannot_grant_the_capability`
(`tests/voice/test_tenant_voice_settings.py:176-198`) posts `{"enabled": True, "voice_id": None,
"can_use_voice": True}` and asserts the grant is unchanged — it depends on Pydantic v2's default
`extra="ignore"` silently dropping the unrecognised `can_use_voice` field so the request still
returns 200. Add `extra="forbid"` and that same request becomes a 422 instead; the natural reaction
is to "fix" the test to stop sending `can_use_voice` rather than notice that the fix just removed
the one thing proving a tenant can't self-grant. The schema's missing `extra="forbid"` isn't a gap
— the test's entire premise depends on it staying missing. (Recorded in the plan's ledger,
`.superpowers/sdd/2026-08-21-tenant-voice-settings-backend/progress.md`, "TRAP RECORDED", verbatim
before this SOP was written.)

## What's involved (files touched)

| File | Change |
|------|--------|
| `alembic/versions/5a7851d2e0c2_add_tenant_voice_preference.py` | additive migration, `down_revision="d1e2mergevoicecaller"` |
| `app/db/models/tenant.py` | `TenantAIConfig.voice_enabled` (bool, default `True`/`server_default="true"`), `.voice_id` (`String(64)`, nullable) |
| `app/services/voice/gating.py` | `VoiceGate._voice_enabled_by_tenant()`; `check()` gains step 2 (preference), reuses `FLAG_DISABLED` |
| `app/services/voice/voices.py` | `CuratedVoice`, `CURATED_VOICES`, `is_curated_voice_id()` — **placeholder ids, see Follow-ups** |
| `app/schemas/tenant.py` | `CuratedVoiceResponse`, `VoiceSettingsResponse`, `VoiceSettingsUpdate` (no `extra="forbid"`, see above) |
| `app/api/v1/endpoints/tenant.py` | `GET /voice/settings` (`:205-242`), `PUT /voice/settings` (`:245-279`) |
| `tests/voice/test_tenant_voice_preference.py` | 3 tests — ORM default, DDL `server_default` via `information_schema`, nullable `voice_id` |
| `tests/voice/test_gating.py` | +4 tests for the new preference step, incl. a mock-based ordering test |
| `tests/voice/test_tenant_voice_settings.py` | 9 tests — GET (grant/preference/usage shape, unconditional shape, DB-backed preference read, Redis-degradation, auth) + PUT (update, **the security test**, unknown-voice-id 422, auth) |

Full paths: `GET /api/v1/tenant/voice/settings`, `PUT /api/v1/tenant/voice/settings`. Both additive
— new routes, new columns, no existing contract touched.

## Verification

**The dev database (`luran_db`) cannot run this suite — use `luran_test`.** `luran_db` is stamped
`c1a0` but partially migrated past that point in an inconsistent way: `agent_devices` exists (so
`alembic upgrade head` dies on `DuplicateTable`), while `users.availability_status` and
`tickets.acknowledged_at` do not (so any test inserting a `User` or `Ticket` row fails on a missing
column). `PG_DATABASE_URL` reads `DATABASE_URL`, so pointing the suite at a separately built,
fully-migrated database needs no code change:

```bash
PW=$(grep -E '^DATABASE_URL' .env | sed 's|.*://luran_user:||' | sed 's|@.*||')
TESTDB="postgresql://luran_user:${PW}@localhost:5432/luran_test"
DATABASE_URL="$TESTDB" REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/voice/ -q --no-cov
```

Run this session, against `luran_test` built from the full 78-migration chain (`alembic upgrade
head` succeeds, single head `5a7851d2e0c2`):

```
134 passed, 10233 warnings in 3.81s
```

Full suite, same database:

```
DATABASE_URL="$TESTDB" REDIS_URL=redis://localhost:6379/0 poetry run pytest -q --no-cov
1158 passed, 33 skipped, 224050 warnings in 97.57s
```

**Zero failures — this is the standard against `luran_test`, not the ~1 pre-existing failure
expected against the drifted dev DB.** Earlier in this feature's own ledger, three
`tests/voice/test_escalate.py` failures were logged as "pre-existing and environmental" against
`luran_db`. They were not. They were `psycopg2.errors.UndefinedColumn: column
tickets.acknowledged_at does not exist` — the same dev-DB drift described above — and pass cleanly
against a correctly migrated schema, confirmed again in this session's full-suite run (0 failures,
`test_escalate.py` included).

New-file breakdown (subset of the 134, run individually this session):

```
tests/voice/test_tenant_voice_preference.py   3 passed
tests/voice/test_tenant_voice_settings.py     9 passed
tests/voice/test_gating.py                   12 passed   (4 new + 8 pre-existing)
```

`ruff check` on every file in the table above — clean. `black --check` — clean on every file
**except** `tests/voice/test_gating.py`, which needs reformatting on the two test functions T2
added (`test_tenant_disabled_voice_is_denied_even_when_granted`,
`test_ungranted_tenant_never_consults_the_preference` — both currently wrapped across two lines
where black wants one). Confirmed this is new drift from this feature, not carried forward: `black
--check` against the file as it stood at `63c080c` (immediately before T2) already wanted changes,
but to *different* lines (pre-existing repo-wide drift); the two lines T2 added are additional,
separate drift on top of that. Not blocking — `ruff` is the enforced gate in CI, `black` is
advisory here per the existing repo-wide backlog — but real, and listed in Follow-ups rather than
silently left for someone else to notice.

Migration round-trip: `alembic upgrade head` reaches single head `5a7851d2e0c2` cleanly against a
fresh database (see command above). Full `upgrade → downgrade -1 → upgrade` round-trip was **not**
re-run in this session (the model/migration code was not touched by Task 5); it was verified
against a scratch database during Task 1 (see
`.superpowers/sdd/2026-08-21-tenant-voice-settings-backend/task-1-report.md`) and the schema
mechanics haven't changed since.

## Operate / roll back

- **To turn voice off for a tenant without touching the grant:** `PUT /voice/settings` with
  `{"enabled": false}` as that tenant, or directly `UPDATE tenant_ai_configs SET voice_enabled =
  false WHERE tenant_id = ...`. Takes effect on the next `VoiceGate.check()` — no cache, no restart.
- **A tenant with no `TenantAIConfig` row at all is treated as enabled** (`gating.py:104-112`) — so
  disabling voice platform-wide for a tenant that predates this feature and has never called `PUT`
  requires either an explicit row or relying on the grant (`can_use_voice`) alone.
- **Roll back the migration:** `alembic downgrade -1` from `5a7851d2e0c2` drops both columns;
  additive and reversible, no backfill, no data loss.
- **Roll back the code:** revert commits `9ca1d54..afb2eda` (11 commits — `git log 9ca1d54..afb2eda`;
  `9ca1d54` itself is the plan-writing commit, excluded by `..` semantics, and can stay). Nothing
  outside `app/services/voice/`, `app/api/v1/endpoints/tenant.py`, and `app/schemas/tenant.py`
  references these symbols.
- Not yet deployed — branch hasn't merged to `develop`.

## Follow-ups

- **The curated voice list is still a placeholder — this did not ship real ElevenLabs voice ids.**
  `app/services/voice/voices.py` carries exactly one entry, `id="REPLACE_ME_voice_1"`,
  `name="Placeholder One"`, with a `TODO(voice-ids)` docstring saying so explicitly. This was a
  **deliberate, flagged** placeholder from the plan (`9ca1d54`'s commit message: "the curated
  ElevenLabs voice ids cannot be invented and must be taken from the real account library") and the
  design spec called for ~8 voices spanning gender, accent, and pace, auditioned and recorded here
  with reasoning (`docs/superpowers/specs/2026-08-21-tenant-voice-settings-design.md`, §4). **None
  of that happened in Tasks 1-4 — no real voices were chosen, so there is nothing to record here.**
  This blocks the dashboard's voice-picker from being useful and blocks `PUT`'s validation
  (`is_curated_voice_id`) from accepting any real id — someone with access to the ElevenLabs account
  needs to pick and paste in real ids before this ships to any tenant.
- **`VoiceSettingsUpdate` has no `extra="forbid"`, by design — do not add it.** See decision 4 above.
  If a future change needs stricter body validation on this endpoint, it must be done without
  breaking the guarantee `test_put_cannot_grant_the_capability` exists to prove.
- **New black drift in `tests/voice/test_gating.py`**, two lines from T2's own additions (see
  Verification). Not blocking, not fixed here (documentation-only task) — worth a one-line `black`
  pass whenever that file is next touched for a real change.
- **`config is None` branch in `PUT` is effectively dead in production** — `TenantService.create_tenant`
  always creates a `TenantAIConfig` row, so a concurrent double-`PUT` on a genuinely row-less tenant
  would surface a unique-constraint 500 rather than a graceful conflict. Logged as a deferred minor
  during Task 4's review, not fixed — non-issue in practice.
- **Endpoint logger doesn't capture `exc_info`** on the usage-degradation path
  (`app/api/v1/endpoints/tenant.py:229-233`) — a Redis outage and a malformed-value `ValueError` are
  indistinguishable in logs. Deferred during Task 3's review.
- **Frontend not built.** This is backend-only; the dashboard Voice Settings page consuming these
  two endpoints is separate, unstarted work.

Design spec: `docs/superpowers/specs/2026-08-21-tenant-voice-settings-design.md`
Plan: `docs/superpowers/plans/2026-08-21-tenant-voice-settings-backend.md`
Task reports + review diffs: `.superpowers/sdd/2026-08-21-tenant-voice-settings-backend/`
