# SOP — Voice chat: usage metering + session lifecycle (Task 11)

**Status:** Shipped on branch `feat/voice-chat-backend` (not yet merged) · 2026-08-17
**Area:** `app/api/v1/endpoints/voice.py`, `app/services/voice/session_tokens.py`,
`app/schemas/voice.py`

This is the last substantive task of the voice-chat backend plan. It closes two gaps that were
live in the shipped code before this task: nothing released a concurrency slot at normal call end
(only the mint-failure path did), and `VoiceMeter.add_minutes` had no production caller at all —
the minute cap was being checked against a counter nothing ever incremented.

---

## What shipped

`POST /api/v1/voice/session/end` (`app/api/v1/endpoints/voice.py`) — called by the widget on
hang-up. Resolves the opaque voice token, meters the call's elapsed minutes, and releases the
tenant's concurrency slot.

Also added, in `app/services/voice/session_tokens.py`:
- `VoiceSessionContext.minted_at` — a new field recording when the token was minted.
- `VoiceSessionTokenStore.resolve_and_revoke()` — an atomic fetch-and-delete (Redis `GETDEL`) used
  only by `/session/end`.

`app/services/voice/metering.py` needed no changes — Task 3 already built the full `VoiceMeter`
interface; this task is what finally calls it in production.

## Why

Two facts, verified directly in the code before starting:

1. `grep -rn "release_slot(" app` returned exactly one call site — inside `_release_reserved_slot`,
   which only runs on the mint-failure path. Every *successful* call held its concurrency slot
   until the 2-hour ZSET eviction, so a tenant making enough short calls in a row would get locked
   out of voice with `concurrency_limit` for up to two hours.
2. `VoiceMeter.add_minutes`'s only reference anywhere was `tests/conftest.py:394`, inside a test
   fixture. The minute cap in `VoiceGate.check` reads `get_monthly_minutes`, but nothing in
   production ever wrote to it — the cap was decorative.

There was also no `/voice/session/end` route at all before this task.

## How it works — and the three design decisions

### 1. Where do minutes come from? Server-derived, not client-reported

The task brief's own sample code (`{"voice_token": token, "duration_seconds": 90}`) had the caller
report its own call duration. **Deliberately not implemented that way** — a client-reported
duration is a number a client can lie about, and it meters a paid resource. Instead:

- `VoiceSessionTokenStore.mint()` now stamps `minted_at: time.time()` into the token's Redis
  payload at mint time.
- `resolve()`'s TTL-slide (added in an earlier task, to stop a long call from going silent
  mid-conversation) deliberately does **not** touch `minted_at` — it stays pinned to the original
  mint, which is exactly what "how long has this call been running" needs.
- `/session/end` computes `elapsed_minutes = (time.time() - context.minted_at) / 60`. The request
  schema (`VoiceSessionEndRequest`) has no duration field at all — even if a client sends
  `duration_seconds`, Pydantic drops it silently, and it's never read.

Tokens minted before this field existed (a one-time window during rollout) fall back to `minted_at
= now` at parse time rather than crashing — that specific in-flight population of calls undercounts
by at most one mint-TTL window (10 minutes today), which is strictly safer than a 500 or a
fabricated number.

### 2. Must `/session/end` be idempotent? Yes — enforced atomically, not just by convention

The brief's sample code did `resolve()` then `revoke()` as two separate calls. That has a real
race: two near-simultaneous end requests for the same token (a double-tap hangup, a client retry
racing the original request) could both `GET` the payload before either `DELETE`s it, and both
would go on to meter the same call.

Closed with `VoiceSessionTokenStore.resolve_and_revoke()` — a single Redis `GETDEL` round trip.
Exactly one of two racing callers can ever get the payload back; the other gets `None` and no-ops.
This is the same "atomic instead of check-then-act" instinct the concurrency gate's Lua script
already applies (`gating.py`'s `_TRY_ACQUIRE_SLOT_SCRIPT`, closed in an earlier task for the mint
path) — applied here to the teardown path.

Proven under an actual race, not just sequential calls:
`test_resolve_and_revoke_is_atomic_under_concurrent_duplicate_calls` fires two
`resolve_and_revoke()` calls via `asyncio.gather()` on the same token and asserts exactly one
non-`None` result.

**Known, accepted gap:** a browser that crashes or loses network never calls `/session/end` at
all. Not solved by this task — the slot self-heals via its own 2-hour TTL, and the minute cap fails
closed (denies new calls) rather than open, so an unended call costs at most one leaked slot-hour,
never unbounded minutes.

### 3. Ordering when one step fails and the other doesn't

Metering and slot release are wrapped in **independent** `try`/`except` blocks — a failure in one
must not skip the other. Order: `resolve_and_revoke` (the idempotency gate) → meter → release slot.

Metering runs before slot release because losing a metering write is a silent, non-self-healing
under-bill with no recovery path, whereas a slot leaked here still ages out via the concurrency
ZSET's own per-member TTL (2h, see `gating.py`). Putting metering first means a subsequent crash or
Redis blip during slot release still leaves the financially important write done.

Both failure paths are logged via `logger.error` and swallowed, never surfaced as a 500 — this
route fires at hangup, after the widget has already torn the call down locally, so there is no
caller left who could act on an error response anyway (`resolve_and_revoke` failing outright also
degrades to `{"ok": true}`, for the same reason).

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/api/v1/endpoints/voice.py` | new route `POST /session/end`; imports `VoiceMeter`, `VoiceSessionEndRequest/Response`, `time` |
| `app/schemas/voice.py` | new `VoiceSessionEndRequest` (`voice_token: str`), `VoiceSessionEndResponse` (`ok: bool = True`) |
| `app/services/voice/session_tokens.py` | `VoiceSessionContext.minted_at` field; `mint()` stamps it; new `_parse()` helper (shared by `resolve()` and the new method); new `resolve_and_revoke()` |
| `tests/voice/test_session_tokens.py` | 5 new tests for `minted_at` + `resolve_and_revoke` (incl. the concurrency race test) |
| `tests/voice/test_metering.py` | new — regression coverage for the pre-existing `VoiceMeter` (accumulation, zero-for-unused, month-scoped key, tenant isolation) |
| `tests/voice/test_session_end.py` | new — 8 tests: happy path, ceiling-then-freed-then-mint-succeeds, client-duration ignored, idempotent second call, unknown token, metering-fails-still-releases, release-fails-still-meters, missing-field validation |

API is additive — new route on the existing `voice` router, no existing contract touched. No
migration involved. `app/services/voice/metering.py` has no diff; it already implemented the
required interface.

## Verification

Run with `REDIS_URL=redis://localhost:6379/0` — `.env`'s Docker-internal hostname doesn't resolve
from the host (same caveat as every prior voice-task SOP).

- **TDD:** all new tests written first and confirmed RED (5 `test_session_tokens.py` additions
  failed on missing `minted_at`/`resolve_and_revoke`; all 8 `test_session_end.py` tests 404'd with
  no route registered), then implementation added, then confirmed GREEN.
- `tests/voice/` — **85 passed** (68 pre-existing + 17 new).
- Full suite — **1012 passed, 1 failed, 33 skipped** (995 pre-existing + 17 new). The 1 failure,
  `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`, is the
  same pre-existing dev-DB row-pollution failure documented in every prior voice-task SOP —
  unrelated to this work.
- `black`/`ruff` on the 6 files touched — clean.
- **Break-it-then-revert, metering:** commented out the `VoiceMeter().add_minutes(...)` call in the
  endpoint. Re-ran `tests/voice/test_session_end.py` — 3 tests went red
  (`test_session_end_releases_slot_and_meters`, `test_session_end_is_idempotent_second_call_is_a_noop`,
  `test_session_end_never_500s_and_still_meters_when_slot_release_fails`), each on the *stored
  monthly total* assertion, not a "was it called" mock assertion. Reverted; full `tests/voice/`
  suite back to 85 passed.
- **Break-it-then-revert, slot release:** commented out the `VoiceGate(db).release_slot(...)` call.
  Re-ran the same file — 4 tests went red, including
  `test_session_end_frees_slot_for_a_subsequent_mint_at_the_ceiling` (asserts a real mint at the
  ceiling now succeeds after the release, per the brief's explicit ask), each on the sorted-set
  membership/`ZCARD`, not the response status code. Reverted; full `tests/voice/` suite back to 85
  passed.

## Operate / roll back

- No new env var and no feature flag. `/session/end` is unauthenticated like `/session/token` — the
  opaque voice token is the credential, and the worst a forged call can do is end a session that
  isn't its own (self-heals via the token/slot TTLs). It carries the same `voice_rate_limit`
  dependency as `/session/token` (IP-keyed) as a deliberate addition beyond the brief's sample code,
  for consistency with that route's abuse protection.
- **Roll back the code:** revert this task's commit(s) on `feat/voice-chat-backend`. Nothing else in
  the codebase calls `resolve_and_revoke` or references `/session/end` yet. Rolling back reopens
  both gaps this task closed (slot leak on normal call end, dead minute counter) — not a safe
  rollback target once real traffic depends on it.
- Not yet deployed anywhere — this branch hasn't merged to `develop`.

## Follow-ups

- **Crash/disconnect gap** (see design decision #2 above): a browser that never calls `/session/end`
  leaks a slot for up to 2h and never meters that call's minutes at all — under-billing, not
  over-billing, so it's the safe direction to be wrong in, but it means monthly minute totals from
  this mechanism alone will under-report actual usage for any call that doesn't end cleanly. A
  future task could add a periodic reconciliation job (e.g. cross-referencing ElevenLabs' own
  call-duration webhooks/API, once Task 9's Postgres flush mentioned in `metering.py`'s original
  docstring exists) for durable, audit-grade billing.
- **No Postgres flush yet.** `metering.py`'s original docstring mentions "a periodic Postgres flush
  for durable reporting" as a future task — Redis remains the only store for monthly minutes today
  (45-day TTL, comfortably past any billing cycle, but not a permanent record).

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
Task 10 SOP: `docs/sop/voice_chat_escalate_to_human_tool.md`
