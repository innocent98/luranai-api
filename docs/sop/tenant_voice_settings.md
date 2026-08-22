# SOP — Tenant voice settings: preference storage, gate ordering, GET/PUT endpoints

**Status:** Shipped on branch `feat/tenant-voice-settings` (not yet merged) · commits `161d41a`
through `afb2eda` (`git log 9ca1d54..afb2eda` for the full range — `9ca1d54` is the plan-writing
commit itself and is excluded) · 2026-08-21 → 2026-08-22 · **final whole-branch review fixes**
(PUT test-coverage gap, black drift, three SOP correctness errors, two minors) landed
`742b16f..2563c7f` on 2026-08-22, this doc's own corrections in the commit that follows
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

- The tenant-facing **voice settings** write path (`update_my_voice_settings`,
  `app/api/v1/endpoints/tenant.py:250-279`) has no query, no import, no code path that touches
  `TenantCapabilities` at all — there's nothing to forget to guard. **This claim is scoped to that
  one endpoint, not to "a tenant admin" in general** — see the `/subscription/upgrade` Follow-up
  below for a different tenant-facing route that already reaches `TenantCapabilities` today.
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

### 4. `VoiceSettingsUpdate` has no `extra="forbid"` — a deliberate omission, but not the only way to write the guard

**Correction (2026-08-22 final-branch review):** this section originally claimed the security
test's "entire premise depends on" `extra="forbid"` staying absent from `VoiceSettingsUpdate`
(`app/schemas/tenant.py:342-344`). That claim was checked live during the final review and is
**false** — it is *a* way to write the guard, not the only one. Corrected reasoning below.

Every other `*Update` schema in `app/schemas/tenant.py` — `TenantUpdate`, `TenantFeaturedUpdate`,
`TenantAIConfigUpdate`, `TenantCapabilitiesUpdate` — declares `model_config =
ConfigDict(extra="forbid")`. `VoiceSettingsUpdate` does not. Under the current `extra="ignore"`
default, `test_put_cannot_grant_the_capability`
(`tests/voice/test_tenant_voice_settings.py:176-198`) posts `{"enabled": True, "voice_id": None,
"can_use_voice": True}`, gets a 200 with the unrecognised `can_use_voice` field silently dropped,
and asserts the grant is unchanged. **Under `extra="forbid"` the identical request returns 422
instead — a rejection, not a silent grant — and a test asserting `resp.status_code == 422` plus
`caps.can_use_voice is False` proves exactly the same property.** It would still fail the moment a
real `can_use_voice` field were ever added to the schema, which is the guarantee that actually
matters. So the omission is preservable but not required by the test; nothing forces it.

Meanwhile `extra="ignore"` has a real, live cost that compounds Finding 1's full-replace risk: a
frontend typo like `voiceId` for `voice_id` is silently dropped by the schema **and** silently
clears the saved voice on the very next `PUT`, because the endpoint writes `payload.voice_id`'s
now-defaulted `None` unconditionally. Under `extra="forbid"` that typo would 422 instead of
corrupting state.

**Not changed here** — this is a deliberate decision for a separate change (the test would need
rewriting to the 422-based form in the same breath), tracked as a Follow-up below, not implemented
during this review-fixes pass.

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
| `tests/voice/test_tenant_voice_settings.py` | 10 tests — GET (grant/preference/usage shape, unconditional shape, DB-backed preference read, Redis-degradation, auth) + PUT (update via the create branch, **update via the seeded/production branch, pinning full-replace — added in final-branch review**, **the security test**, unknown-voice-id 422, auth) |

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

**Re-run after the 2026-08-22 final-branch review fixes** (adds one PUT test, see files-touched
table above), same database:

```
135 passed, 10306 warnings in 3.19s
```

Full suite, same database, before the review fixes:

```
DATABASE_URL="$TESTDB" REDIS_URL=redis://localhost:6379/0 poetry run pytest -q --no-cov
1158 passed, 33 skipped, 224050 warnings in 97.57s
```

Full suite after the review fixes:

```
1159 passed, 33 skipped, 224381 warnings in 102.13s
```

**Zero failures — this is the standard against `luran_test`, not the ~1 pre-existing failure
expected against the drifted dev DB.** Earlier in this feature's own ledger, three
`tests/voice/test_escalate.py` failures were logged as "pre-existing and environmental" against
`luran_db`. They were not. They were `psycopg2.errors.UndefinedColumn: column
tickets.acknowledged_at does not exist` — the same dev-DB drift described above — and pass cleanly
against a correctly migrated schema, confirmed again in this session's full-suite run (0 failures,
`test_escalate.py` included).

New-file breakdown (subset of the 135, run individually this session):

```
tests/voice/test_tenant_voice_preference.py   3 passed
tests/voice/test_tenant_voice_settings.py    10 passed
tests/voice/test_gating.py                   12 passed   (4 new + 8 pre-existing)
```

`ruff check` on every file in the table above — clean.

**Correction (2026-08-22 final-branch review):** this section previously claimed `black --check`
against `tests/voice/test_gating.py` as it stood at `63c080c` "already wanted changes, but to
different lines (pre-existing repo-wide drift)", and used that to defer the fix as non-blocking.
**That claim was false.** Re-checked live under the repo's own `pyproject.toml`
(`line-length = 100`, confirmed via `grep -n line-length pyproject.toml`): the file was **clean**
at `63c080c` —

```
$ git show 63c080c:tests/voice/test_gating.py > /tmp/x.py
$ poetry run black --check --diff --line-length 100 /tmp/x.py
All done! ✨ 🍰 ✨
1 file would be left unchanged.
```

The earlier check almost certainly ran black on a copy outside the repo tree, where it falls back
to its 88-column default instead of picking up `pyproject.toml`. So **all** the drift in that file
— the two signature lines T2 wrapped across two lines where black at 100 columns wants one — was
this branch's own, not pre-existing, and the false claim was the stated reason it was deferred.
**Fixed** in the same review-fixes pass (`poetry run black tests/voice/test_gating.py`, commit
`f50b238`). `black --check` is now clean on every file in the table above, with no exceptions.

Migration round-trip: `alembic upgrade head` reaches single head `5a7851d2e0c2` cleanly against a
fresh database (see command above). Full `upgrade → downgrade -1 → upgrade` round-trip was **not**
re-run in this session (the model/migration code was not touched by Task 5); it was verified
against a scratch database during Task 1 (see
`.superpowers/sdd/2026-08-21-tenant-voice-settings-backend/task-1-report.md`) and the schema
mechanics haven't changed since.

## Operate / roll back

**Correction (2026-08-22 final-branch review):** this section previously instructed operators to
turn voice off with `PUT /voice/settings {"enabled": false}` alone. Followed literally, that
destroys the tenant's saved `voice_id` — verified live during the final review. Corrected below.

- **This endpoint is a full replace, unlike `PUT /tenant/ai-config` next to it in the same file**
  (which is PATCH-like via `model_dump(exclude_unset=True)`, `app/api/v1/endpoints/tenant.py:131`).
  `update_my_voice_settings` writes both `config.voice_enabled` and `config.voice_id`
  unconditionally from the request body every time (`tenant.py:277-278`) — there is no partial
  update. **Always send both fields.**
- **To turn voice off for a tenant without touching their saved voice choice:** `PUT
  /voice/settings` with `{"enabled": false, "voice_id": "<their current voice_id>"}` as that
  tenant — **not** `{"enabled": false}` alone. Because `voice_id` defaults to `null` on the
  schema and this endpoint is a full replace, omitting it clears the tenant's saved voice, not
  just leaves it alone (pinned by
  `tests/voice/test_tenant_voice_settings.py::test_put_on_existing_config_is_a_full_replace`, added
  2026-08-22). Read the current `voice_id` from `GET /voice/settings` first if it isn't already
  known, or update the row directly: `UPDATE tenant_ai_configs SET voice_enabled = false WHERE
  tenant_id = ...` (leaves `voice_id` untouched). Either way takes effect on the next
  `VoiceGate.check()` — no cache, no restart.
- **A tenant with no `TenantAIConfig` row at all is treated as enabled** (`gating.py:104-112`) — so
  disabling voice platform-wide for a tenant that predates this feature and has never called `PUT`
  requires either an explicit row or relying on the grant (`can_use_voice`) alone.
- **Roll back the migration:** `alembic downgrade -1` from `5a7851d2e0c2` drops both columns;
  additive and reversible, no backfill, no data loss.
- **Roll back the code:** revert commits `9ca1d54..afb2eda` (11 commits — `git log 9ca1d54..afb2eda`;
  `9ca1d54` itself is the plan-writing commit, excluded by `..` semantics, and can stay), plus the
  final-branch review-fixes commits `742b16f..2563c7f` (test addition, black formatting, the two
  minors) and this doc's own correction commit. Nothing outside `app/services/voice/`,
  `app/api/v1/endpoints/tenant.py`, and `app/schemas/tenant.py` references these symbols.
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
- **`VoiceSettingsUpdate` has no `extra="forbid"` — preservable either way, revisit deliberately.**
  See decision 4 above (corrected 2026-08-22): the omission is *a* way to write the security guard
  `test_put_cannot_grant_the_capability` depends on, not the only one — a 422-plus-unchanged-grant
  form of that test would prove the same property under `extra="forbid"`. Not changed in this
  review-fixes pass; if a future change adds `extra="forbid"` here, update that test to the
  422-based form in the same commit, and account for the typo-swallowing cost noted in decision 4
  (`voiceId` vs `voice_id` silently drops **and** — because of Finding 1's full-replace semantics —
  silently clears the saved voice).
- ~~New black drift in `tests/voice/test_gating.py`~~ **Fixed 2026-08-22** (`poetry run black
  tests/voice/test_gating.py`, commit `f50b238`) — was real drift from this feature's own commits,
  not pre-existing repo drift as originally (incorrectly) recorded; see the corrected Verification
  paragraph above.
- **`config is None` branch in `PUT` is effectively dead in production** — `TenantService.create_tenant`
  always creates a `TenantAIConfig` row, so a concurrent double-`PUT` on a genuinely row-less tenant
  would surface a unique-constraint 500 rather than a graceful conflict. Logged as a deferred minor
  during Task 4's review, not fixed — non-issue in practice. (The branch that actually runs in
  production is now covered by
  `test_put_on_existing_config_is_a_full_replace`, added 2026-08-22 — see files-touched table.)
- ~~Endpoint logger doesn't capture `exc_info` on the usage-degradation path~~ **Fixed 2026-08-22**
  (`app/api/v1/endpoints/tenant.py`, commit `2563c7f`) — `logger.warning(..., exc_info=True)` so a
  Redis outage and a malformed-value `ValueError` are now distinguishable in logs.
- **`voice_id` is stored and validated but consumed by nothing — choosing a voice currently has no
  effect on a call.** Added 2026-08-22 during final-branch review; `grep` across `app/` finds no
  reader of `TenantAIConfig.voice_id` outside this feature's own read/write path. The mint path
  (`mint_conversation_token`) takes only `agent_id` and `participant_name`, and the caller passes a
  single global `settings.ELEVENLABS_AGENT_ID` with no per-voice override. This is spec-conformant —
  no slice in this branch's plan wired a voice override into minting — but applying a tenant's
  chosen voice to a real call needs a mint-path change (a per-tenant/per-voice agent id, or an
  ElevenLabs conversation-level voice override), which is unstarted work. **The dashboard's Voice
  Settings page must not imply that picking a voice changes anything yet** — the copy needs to be
  honest about this until the mint path is wired up, on top of already needing to wait on real
  ElevenLabs ids (see the placeholder-ids Follow-up above).
- **`POST /api/v1/subscription/upgrade` is a prerequisite audit before voice keys enter plan
  config.** Added 2026-08-22 during final-branch review. The isolation claim in decision 1 above is
  scoped to `update_my_voice_settings` specifically, and holds there — but there is a *different*,
  already-existing tenant-facing route to `TenantCapabilities`:
  `upgrade_subscription_direct` (`app/api/v1/endpoints/subscription.py:235`) is
  `Depends(get_current_tenant_admin)`, has no additional router-level guard, is documented in its
  own docstring as upgrading "without payment processing" (intended for testing/admin/promotional
  use, not production traffic), and calls `_update_capabilities_for_tier`
  (`app/services/subscription_service.py:63`), which `setattr`s every key present in
  `SubscriptionPlan.get_capabilities()` onto the tenant's `TenantCapabilities` row. **Today this
  cannot touch voice** — confirmed live: `get_capabilities()`
  (`app/db/models/subscription_plan.py:117-137`) has no `can_use_voice` or
  `max_voice_minutes_per_month` key, and neither does `SubscriptionPlan`'s column set. **The moment
  plan-tier voice keys are added to that model — which is explicitly the next piece of planned
  work for this feature — a tenant admin gains the ability to self-grant `can_use_voice` and
  `max_voice_minutes_per_month` by calling one endpoint that does no payment processing.** Not
  introduced by this branch, not fixed here — it's out of scope for a voice-settings change and
  needs its own deliberate remediation (e.g. gate the endpoint behind a platform-admin role, or
  exclude capability keys the endpoint isn't meant to grant). **Audit and close this before any
  commit adds voice keys to `SubscriptionPlan`/`get_capabilities()`.**
- **Frontend not built.** This is backend-only; the dashboard Voice Settings page consuming these
  two endpoints is separate, unstarted work. (See the `voice_id`-not-consumed Follow-up above —
  whatever page gets built must not imply voice choice already does something at call time.)

Design spec: `docs/superpowers/specs/2026-08-21-tenant-voice-settings-design.md`
Plan: `docs/superpowers/plans/2026-08-21-tenant-voice-settings-backend.md`
Task reports + review diffs: `.superpowers/sdd/2026-08-21-tenant-voice-settings-backend/`
