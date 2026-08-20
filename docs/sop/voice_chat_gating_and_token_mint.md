# SOP — Voice chat: tenant gating + opaque token mint (Slice 1)

**Status:** Shipped on branch `feat/voice-chat-backend` (not yet merged) · code commits `12c2177` through `ee913e9` (`git log c28283d..ee913e9` for the full range incl. plan/spec docs) · 2026-08-16
**Area:** `app/services/voice/`, `app/api/v1/endpoints/voice.py`, `app/core/config.py`, `app/db/models/tenant.py`, one migration

---

## What shipped

The backend half of Slice 1 for in-widget voice chat: a gated `POST /api/v1/voice/session/token`
endpoint that mints a short-lived ElevenLabs WebRTC conversation token, but only after checking
per-tenant voice access (feature flag → monthly minute cap → concurrency ceiling) and minting our
own **opaque session token** that ElevenLabs carries back to us on every subsequent call. No
minutes are billable and no ElevenLabs call happens unless a tenant clears all three gates.

This is backend-only. The widget/frontend integration, the custom-LLM SSE endpoint ElevenLabs
calls back into, and `/voice/session/end` are later slices — see Follow-ups.

## Why

Voice minutes cost real money against a paid ElevenLabs plan, and the ElevenLabs auth/token-mint
wire shape was still unresolved (Spike 1, still with the user) at the time this was built. Two
requirements had to be met simultaneously: (1) nothing should be able to spend voice minutes
without passing tenant-level gating, and (2) the rest of the backend — gating, concurrency
control, the mint endpoint — needed to be buildable and testable **without** waiting on the
ElevenLabs spike to land, since ElevenLabs is the one dependency outside the team's control.

## How it works

### Opaque token, not trusted ElevenLabs metadata

`app/services/voice/session_tokens.py`'s `VoiceSessionTokenStore` mints a random
`secrets.token_urlsafe(32)` string and stores `{tenant_id, luran_session_id, text_out}` against it
in Redis (`voice:session_token:{token}`, TTL = `settings.VOICE_SESSION_TOKEN_TTL_SECONDS`, 600s
default). ElevenLabs is handed only the opaque string; when it calls back into our server (custom
LLM endpoint, tool webhooks — later slices), we `resolve()` the token server-side to recover the
tenant/session binding. **We never trust tenant identity ElevenLabs might pass back to us.** This
is also what let the entire gating + mint-endpoint backend be built and tested while Spike 1 (the
real ElevenLabs wire shape) was still unresolved — the token store's contract doesn't depend on
any ElevenLabs implementation detail.

### Where tenant isolation actually lives

`VoiceSessionTokenStore.mint()` takes `tenant_id` as a plain parameter and trusts its caller — by
design, that's the store's contract, not a bug. The isolation boundary lives one layer up, in the
endpoint (`app/api/v1/endpoints/voice.py:74-84`): it loads the `ConversationSession` filtered on
**both** `id == payload.luran_session_id` and `tenant_id == payload.tenant_id`, and returns an
identical 404 `{"reason": "session_not_found"}` whether the session doesn't exist at all or exists
under a different tenant. There is no existence oracle — a caller can't distinguish "no such
session" from "wrong tenant" by response shape.

### Gating order and the atomic concurrency reserve

`app/services/voice/gating.py`'s `VoiceGate.check(tenant_id)` runs, in order:

1. **Flag** — `TenantCapabilities.can_use_voice`. **Fail-closed**: a tenant with no
   `TenantCapabilities` row at all is denied, same as `can_use_voice=False`.
2. **Monthly minute cap** — `max_voice_minutes_per_month` (`None` = unlimited) against
   `VoiceMeter.get_monthly_minutes()` (`app/services/voice/metering.py`, Redis `INCRBYFLOAT`
   counter keyed `voice:minutes:{tenant_id}:{YYYY-MM}`, 45-day TTL so month rollover needs no
   scheduled job and Task 9's planned Postgres flush has margin).
3. **Concurrency ceiling** — a non-atomic peek against `settings.VOICE_MAX_CONCURRENT_SESSIONS`
   (20 default), used only as a fast-fail for the flag/cap checks in the endpoint. It is **not**
   the authoritative concurrency decision.

The authoritative decision is `VoiceGate.try_acquire_slot(tenant_id, token)`, a single-round-trip
Lua script (`_TRY_ACQUIRE_SLOT_SCRIPT`) that evicts stale members, checks the ceiling, and reserves
a slot atomically. This exists because the original two-call shape (`check()`'s `SCARD` in one
round trip, the endpoint's `SADD` in another) had a TOCTOU race: two concurrent mints arriving at
`ceiling − 1` could both observe "under the ceiling" and both acquire, overshooting the ElevenLabs
plan cap. Redis executes a Lua script as one atomic unit — nothing can interleave between the read
and the write — so that race is closed.

**In the endpoint's mint flow** (`app/api/v1/endpoints/voice.py:86-159`), the slot is reserved
*before* the ElevenLabs call, not after: `gate.check()` (flag/cap only) → mint the opaque voice
token → `try_acquire_slot()` (the real concurrency decision, 409 `concurrency_limit` + token revoke
on failure) → call ElevenLabs. Reserving first means a request over the concurrency ceiling never
spends a billable EL call. The cost is that every post-reserve failure path must release the slot
it just took — handled by an `el_succeeded` flag plus a `finally` block that calls
`_release_reserved_slot()` whenever the ElevenLabs call didn't complete. `finally` also fires on
`asyncio.CancelledError` (client disconnects mid-call), which a bare `except` would not catch,
closing the leak surface a mid-flight disconnect would otherwise open.

### Why the concurrency set is a sorted set, not a plain set

The first version used a plain Redis `SET` with `EXPIRE key 7200` (2h) on every `SADD` as a
"safety net" against a crashed session leaking a slot forever. It did the opposite: `EXPIRE`
resets the TTL of the **whole key**, not the member. Any tenant minting more than once every two
hours kept the key perpetually alive, so a slot orphaned by a crash never aged out — members
accumulated until `SCARD == ceiling`, permanently locking that tenant out of voice. This is the
single most instructive bug in this slice: a mechanism that read as a safety net was actually
guaranteeing the failure it claimed to prevent.

The fix (commit `ee913e9`) switched the concurrency key to a **sorted set** scored by acquire time,
read from Redis's own `TIME` command inside the Lua script (not passed in from Python, so eviction
is judged by one clock regardless of app-server clock skew). Every `try_acquire_slot()` call first
runs `ZREMRANGEBYSCORE key -inf (now-ttl)` to evict anything older than the 2h TTL, giving each
member genuine per-member expiry independent of whether the key itself has been touched recently.
`acquire_slot`, `release_slot`, and `check()`'s peek were all migrated to the same `ZADD`/`ZREM`/
`ZCOUNT` model so all three stay consistent with the authoritative script's data shape.

### Why voice has its own rate limiter

`voice_rate_limiter` (`app/utils/http_rate_limiter.py`) is a dedicated 10 calls/60s limiter, not a
reuse of `chat_rate_limiter` (20/60s). The concurrency ceiling caps how many voice sessions are
*simultaneously active*; it says nothing about how fast one caller can churn mint → fail → retry.
Every mint attempt that clears gating spends a real ElevenLabs API call — a public,
unauthenticated endpoint (mirrors `/chat/message`) burning paid third-party quota needed its own,
independently tunable throttle rather than inheriting chat's number by coincidence.

### `dynamic_variables` is accepted but NOT transmitted

`ElevenLabsClient.mint_conversation_token(..., dynamic_variables: dict)` accepts the parameter to
match the published interface — it's meant to be the mechanism by which the opaque `voice_token`
reaches ElevenLabs so later calls (the custom-LLM endpoint, tool webhooks) can identify the
session — but **the parameter is not currently transmitted to ElevenLabs**. The real wire shape
for attaching dynamic variables to a WebRTC conversation-token mint is unresolved, carrying a
`TODO(spike-1)` in the docstring (`app/services/voice/elevenlabs_client.py:21-29`) and a one-line
comment at the endpoint's call site. **This is a known gap, not an implementation detail**: until
Spike 1 resolves the wire shape and this method is updated, the `voice_token` does not reach
ElevenLabs through this call path, and nothing downstream should assume otherwise.

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/core/config.py` | 6 voice settings — `ELEVENLABS_API_KEY`/`ELEVENLABS_AGENT_ID`/`VOICE_SHARED_SECRET` default `None` (fail-closed), `ELEVENLABS_API_BASE`, `VOICE_SESSION_TOKEN_TTL_SECONDS` (600), `VOICE_MAX_CONCURRENT_SESSIONS` (20) |
| `app/db/models/tenant.py` | `TenantCapabilities.can_use_voice` (bool, default `False`), `.max_voice_minutes_per_month` (nullable int) |
| `alembic/versions/c1a0_add_voice_capabilities.py` | additive migration, `down_revision="ab20blockedquality"` |
| `app/services/voice/session_tokens.py` | `VoiceSessionContext`, `VoiceSessionTokenStore` (`mint`/`resolve`/`revoke`) |
| `app/services/voice/gating.py` | `VoiceDenialReason`, `VoiceGateResult`, `VoiceGate` (`check`, `try_acquire_slot`, `acquire_slot`, `release_slot`) |
| `app/services/voice/metering.py` | `VoiceMeter` (`add_minutes`, `get_monthly_minutes`) — minimal, extended by a later slice |
| `app/services/voice/elevenlabs_client.py` | `ElevenLabsClient.mint_conversation_token`, `ElevenLabsMintError` |
| `app/schemas/voice.py` | `VoiceTokenRequest` / `VoiceTokenResponse` |
| `app/api/v1/endpoints/voice.py` | `POST /session/token` |
| `app/api/v1/api.py` | wires `voice.router` under `/voice` |
| `app/utils/http_rate_limiter.py` | `voice_rate_limiter` (10/60s), `voice_rate_limit` dependency |

Full path: `POST /api/v1/voice/session/token`. API is additive — new router, new columns, no
existing contract touched.

## Verification

- `tests/voice/` — **30 passed** (8 gating, 5 session-token, 2 capabilities, 6 ElevenLabs client,
  9 token-mint, run against dev Postgres + Redis), verified locally this session.
- Full suite — **953 passed, 1 failed, 33 skipped**, same result across every task in this slice.
  The 1 failure, `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`,
  is a pre-existing dev-DB row-count assertion (`assert 11 == 0`) caused by leftover `AuditReport`
  rows in the shared local Postgres instance — reproduced with this branch's changes stashed, so
  it predates and is unrelated to this work. Skip it, don't chase it.
- `ruff check` on every touched file — clean.
- Migration round-trip verified this session: `alembic downgrade -1` (`c1a0` → `ab20blockedquality`,
  columns dropped) → `alembic upgrade head` (`ab20blockedquality` → `c1a0`, columns restored)
  → `alembic current` confirms single head `c1a0`.
- TDD + break-it-then-revert discipline per task: each of the four gating properties (check
  ordering, fail-closed on missing capabilities row, atomic reserve under concurrency, revoke-on-
  EL-failure cleanup) and the four Task 4 review fixes (leak surfaces on unexpected exceptions,
  the permanent-lockout TTL bug, the ElevenLabs client's JSON/shape guards, and the missing rate
  limit) were independently broken, confirmed red for the right reason, then reverted — full
  detail with actual command output in `.superpowers/sdd/2026-08-16-voice-chat-backend/task-{1,2,3,4}-report.md`.

## Operate / roll back

- **Fail-closed by default.** `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, and `VOICE_SHARED_SECRET`
  all default to `None`; `can_use_voice` defaults to `False`. With no env vars set and no tenant
  opted in, the endpoint 503s (`voice_not_configured`) before touching gating, and every tenant is
  gated off by default. None of the three env vars appear in `.env.example` yet — add them there
  when a real ElevenLabs account is provisioned for staging/prod.
- **To enable voice for a tenant:** set `can_use_voice = true` (and optionally
  `max_voice_minutes_per_month`) on that tenant's `TenantCapabilities` row. No app restart needed —
  `VoiceGate` reads the row per request.
- **To disable platform-wide without a deploy: unset `ELEVENLABS_AGENT_ID`, not `ELEVENLABS_API_KEY`.**
  Only `ELEVENLABS_AGENT_ID` is checked up front (`voice.py:65-69`) — clearing it 503s
  `voice_not_configured` immediately, before the session lookup, gating, or any Redis call.
  Clearing `ELEVENLABS_API_KEY` instead does **not** short-circuit: the request still runs the
  tenant-isolation DB lookup, `gate.check()`, mints the opaque voice token, and atomically reserves
  a concurrency slot — all before `ElevenLabsClient.mint_conversation_token()` finally rejects it
  (`elevenlabs_client.py:36-37`, no network call made) with a *different* reason code,
  `voice_provider_unavailable`. The reserved slot and voice token are still released/revoked by the
  endpoint's `finally` cleanup, so nothing leaks — but every request pays a full DB read + several
  Redis round trips it wouldn't need to if `ELEVENLABS_AGENT_ID` had been the switch. Verified this
  session with a throwaway test against both settings independently: unsetting `ELEVENLABS_API_KEY`
  alone yields `503 {"reason": "voice_provider_unavailable"}`; unsetting `ELEVENLABS_AGENT_ID` alone
  yields `503 {"reason": "voice_not_configured"}`.
- **Roll back the migration:** `alembic downgrade -1` from `c1a0` drops both columns; additive and
  reversible, no backfill, no data loss (verified round-trip above).
- **Roll back the code:** revert the 8 voice commits on `feat/voice-chat-backend`
  (`git log c28283d..ee913e9` lists all 10 commits on the branch, including 2 docs-only plan/spec
  commits not touched by this rollback); nothing else in the codebase imports `app/services/voice/`
  or references the new router.
- Not yet deployed anywhere — this branch hasn't merged to `develop`.

## Follow-ups

- **Nothing releases a slot at normal session end yet** — `try_acquire_slot`'s reservation is only
  released on mint *failure* (EL error, unexpected exception, cancellation). A session that mints
  successfully and runs to completion holds its concurrency slot until the 2h sorted-set eviction
  window passes. `/voice/session/end` (a later task) is what should release it immediately; until
  it lands, the ceiling is enforced by the 2h TTL alone, not by real session lifecycle.
- **Operational — verify before enabling voice broadly:** `voice_rate_limit` uses the `"user"`
  rate-limiter strategy, which falls back to `request.client.host` for this unauthenticated
  endpoint (same as `chat_rate_limit`). Nothing in the repo sets `FORWARDED_ALLOW_IPS`, and prod
  publishes the container on `127.0.0.1:8000` behind a host nginx — uvicorn's default
  `forwarded_allow_ips="127.0.0.1"` likely won't trust `X-Forwarded-For` from that hop, which would
  collapse every external caller into one bucket and make the 10/min limit **global**, not
  per-visitor. Pre-existing behavior shared with `/chat/message`, not introduced here — but confirm
  client-IP resolution before this endpoint sees real public traffic.
- **`dynamic_variables` wire shape unresolved** (see How, above) — the opaque `voice_token` does
  not currently reach ElevenLabs. Blocked on Spike 1, still with the user. Nothing past this slice
  should assume the token is threaded through until `elevenlabs_client.py`'s `TODO(spike-1)` is
  resolved.
- **Five deferred Minors from Task 4's review**, none blocking. Four — `_release_reserved_slot`
  only catches `RedisError` though its docstring says both cleanup steps always run; the
  broad-except path logs via `logger.error` instead of `.exception` (drops the traceback); the
  `finally` cleanup `await` isn't `asyncio.shield`ed; the stale-member-eviction test's 1s TTL is
  timing-fragile — came from the external code review of `ee913e9`, not from the implementer's own
  report. The fifth, `voice_not_configured` (503, missing `ELEVENLABS_AGENT_ID`) has no dedicated
  test, is self-reported in `task-4-report.md:188` (round-1 self-review) — the mint tests all
  monkeypatch a dummy agent ID, so that branch is exercised only implicitly, never triggered.
- **One pre-existing, unrelated full-suite failure**
  (`tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`) — dev-DB
  row-count pollution, not caused by this work. See Verification.
- **Next backend slice:** the custom-LLM SSE endpoint ElevenLabs' agent calls to get a spoken reply
  from the real Luran pipeline — spike-independent, can proceed without Spike 1. Shipped as
  Slice 2 — see `docs/sop/voice_chat_custom_llm_endpoint.md`, which also documents a related
  change to `VoiceSessionTokenStore.resolve()` (sliding TTL renewal) made after this SOP was
  written.

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Spike notes: `docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
