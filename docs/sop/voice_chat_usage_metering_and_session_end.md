# SOP — Voice chat: usage metering + session lifecycle (Task 11)

**Status:** Shipped on branch `feat/voice-chat-backend` (not yet merged) · 2026-08-17, revised after
review rounds 2 and 3 same day
**Area:** `app/api/v1/endpoints/voice.py`, `app/services/voice/session_tokens.py`,
`app/services/voice/metering.py`, `app/schemas/voice.py`

This is the last substantive task of the voice-chat backend plan. It closes two gaps that were
live in the shipped code before this task: nothing released a concurrency slot at normal call end
(only the mint-failure path did), and `VoiceMeter.add_minutes` had no production caller at all —
the minute cap was being checked against a counter nothing ever incremented.

**This document was rewritten after review round 2 found the first version's core design wrong,
not just its wording, and revised again after round 3 found a regression the round-2 fix itself
introduced.** Round 1 shipped metering entirely inside `/voice/session/end`, and this SOP
originally said an unended call is safe because "the minute cap fails safe (denies new calls)."
That's false — the cap reads a counter only `/session/end` wrote to, so if a client never calls it
(crash, tab close, network loss, or simply never sending it), usage stays at zero and the cap
denies nothing while ElevenLabs minutes keep being spent. Round 2 fixed that with per-turn
metering (see "How it works" §1) — but embedded the metering tick inside the token's own payload,
which round 3 found (a) let two concurrent turns double-bill the same delta, since a read-then-write
isn't atomic, and (b) forced the token's TTL-slide to become a payload-rewriting `SET`, which
resurrects a binding a concurrent delete just removed. Round 3 moved the tick to its own Redis key
and made the whole bill-and-advance operation a single atomic Lua script — see §1 and §2 below.

---

## What shipped

`POST /api/v1/voice/session/end` (`app/api/v1/endpoints/voice.py`) — called by the widget on
hang-up. Resolves the opaque voice token, bills the small trailing minute-delta since the call's
last turn, and releases the tenant's concurrency slot.

**The primary metering mechanism is NOT this route.** `VoiceSessionTokenStore.resolve()` — called
on every conversational turn by `POST /llm/chat/completions`, and also by the escalate-to-human
tool — now bills the wall-clock delta since the token's last metering tick on every call, via a
single atomic Redis Lua script. This is what actually gives the minute cap teeth mid-call and
makes metering survive a call that never ends cleanly.

Changes in `app/services/voice/session_tokens.py`:
- `VoiceSessionContext.minted_at` — when the token was minted. Diagnostic only; billing does not
  derive duration from it (see §1 below for why).
- A metering tick in its own Redis key (`voice:session_tick:{token}`), NOT embedded in the token's
  payload — moved there in round 3 (see the atomicity note in §1 and the resurrection note in §2).
  Seeded to `minted_at` at mint time.
- `_meter_turn()` — one Lua `EVAL` that reads the tick, bills the elapsed delta directly into the
  tenant's monthly-minutes counter, and advances the tick, all as one indivisible Redis operation.
  Called by both `resolve()` (every turn) and `resolve_and_revoke()` (the final partial delta at
  hang-up).
- `resolve()`'s TTL-slide is a bare `EXPIRE` again (it was briefly a payload-rewriting `SET` in
  round 2, to persist the tick that no longer lives in the payload) — `EXPIRE` on a missing key is
  a no-op that can never recreate it.

`app/services/voice/metering.py` gained one small addition in round 3: `VoiceMeter.key_and_ttl()`,
exposing a tenant's monthly-minutes Redis key + TTL so `_meter_turn`'s Lua script can increment it
atomically alongside the tick, which `add_minutes`'s own separate round trip can't provide. No
other change — Task 3 already built the rest of the `VoiceMeter` interface this task calls in
production.

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

## How it works — and the design decisions

### 1. Where do minutes come from? Server-derived, per-turn — not from `/session/end`, and not from `minted_at`

Round 1 derived duration from `time.time() - context.minted_at`, computed once, at
`/session/end`. Two problems, both raised in review:

- **It's entirely client-triggered.** If the widget never calls `/session/end` — a crash, a closed
  tab, a lost network connection, or a client that simply omits the call — metering never runs at
  all for that call. The party who benefits from that (unmetered minutes) is the tenant running
  the widget on their own page, which is the adversarial direction, not a safe one to be wrong in.
  Meanwhile `resolve()`'s TTL slide and the 2h concurrency-slot TTL mean nothing else stops the
  call from continuing — a client that never calls `/session/end` gets fresh unmetered concurrency
  slots every two hours while earlier calls keep running.
- **`minted_at` measures token lifetime, not call duration.** It's stamped at `POST
  /session/token`, before WebRTC connects and before mic permission — so a single end-to-end
  `time.time() - minted_at` calculation bakes that setup lag into the bill, and grows unboundedly
  wrong the longer the gap between mint and end.

**Fix: meter incrementally at turn time.** `resolve()` is called on every conversational turn (by
`voice_custom_llm`) and is proof the caller is still on the call. A metering tick lives in its own
Redis key per token, seeded to `minted_at` at mint time. Every `resolve()` calls `_meter_turn()`,
which runs this Lua script as one atomic Redis operation:

```lua
local last_tick = tonumber(redis.call('GET', tick_key)) or now
local elapsed_minutes = math.max(0, now - last_tick) / 60.0
if elapsed_minutes > 0 then
    redis.call('INCRBYFLOAT', meter_key, elapsed_minutes)
    redis.call('EXPIRE', meter_key, meter_ttl)
end
redis.call('SET', tick_key, now, 'EX', tick_ttl)
```

(`now` is read from Redis's own `TIME` command inside the script, mirroring `gating.py`'s
`_TRY_ACQUIRE_SLOT_SCRIPT` — one clock, deterministic regardless of app-server clock skew.)

**Why a Lua script and not a Python read-then-write:** round 2's first cut did exactly that — read
the tick, compute the delta, call `VoiceMeter.add_minutes`, write the new tick back into the
token's JSON payload. Review round 3 found this genuinely racy: two overlapping `resolve()` calls
on the same token are not hypothetical (`voice.py` explicitly contemplates two concurrent turns
racing for the same rate-limit slot, and the escalate-to-human tool resolves independently of an
in-flight turn). Two overlapping calls could both read the same tick, both bill the same delta,
and the later write would clobber the earlier one — over-billing the tenant, and in the wrong
direction for a customer-facing meter. Running the whole read-bill-advance sequence inside one Lua
script closes this outright: Redis executes scripts atomically (single-threaded), so two
concurrent `EVAL`s of the same script can never interleave. Proven by
`test_concurrent_resolves_do_not_double_bill_the_same_delta`, which fires two `resolve()` calls via
`asyncio.gather()` on a token with a 30s-stale tick and asserts the total billed is ~30s once, not
~60s.

Because the whole script is one atomic unit, a Redis-level failure (an `EVAL` exception, e.g. an
outage) never bills AND never advances the tick — there's no way for one to happen without the
other, unlike the round-2 split sequence. A lost delta from a transient outage is folded into the
next successful tick instead of silently vanishing (proven by
`test_metering_failure_does_not_advance_the_tick_so_the_next_success_catches_up`).

`/voice/session/end` still exists, and still calls `resolve_and_revoke()` — but that method is now
an *optimisation*, not the enforcement path: it bills only the small trailing delta between the
last turn and hang-up. A client that never calls it still gets billed for every turn it completed;
only the silence after the last turn goes unbilled.

The first turn's delta still includes WebRTC-connect + mic-permission lag (there's no earlier
proof-of-life signal to tick from), but that lag is now bounded to one turn's delta instead of
compounding across the entire call the way `minted_at`-based billing did.

Client-supplied duration is still never trusted: `VoiceSessionEndRequest` has no duration field at
all, so even if a client sends `duration_seconds`, Pydantic drops it silently.

**Migration compatibility, explicitly tested:** a payload written by a pre-deploy `mint()` has no
`minted_at` in its JSON, and (since the tick moved to its own key in round 3) a genuinely
pre-metering token also simply has no tick key at all. `_context_from` falls back `minted_at` to
"now" rather than `0.0`; `_METER_TURN_SCRIPT`'s own `GET tick_key` returning nil falls the tick
back to `now` too — the epoch fallback was considered and rejected in both places, because it
would bill ~30 million minutes for the gap since 1970 on the very first turn after deploy.
`test_resolve_tolerates_a_payload_minted_before_minted_at_existed` writes exactly that legacy
shape directly into Redis and asserts `resolve()` doesn't crash and doesn't over-bill.

### 2. Must `/session/end` be idempotent? Yes — enforced atomically, not just by convention

The brief's sample code did `resolve()` then `revoke()` as two separate calls. That has a real
race: two near-simultaneous end requests for the same token (a double-tap hangup, a client retry
racing the original request) could both `GET` the payload before either `DELETE`s it, and both
would go on to bill the same trailing delta.

Closed with `VoiceSessionTokenStore.resolve_and_revoke()` — a single Redis `GETDEL` round trip.
Exactly one of two racing callers can ever get the payload back; the other gets `None` and no-ops.
This mirrors the concurrency gate's own atomic-reserve pattern (`gating.py`'s
`_TRY_ACQUIRE_SLOT_SCRIPT`, closed in an earlier task for the mint path) applied to the teardown
path.

Proven under an actual race, not just sequential calls:
`test_resolve_and_revoke_is_atomic_under_concurrent_duplicate_calls` fires two
`resolve_and_revoke()` calls via `asyncio.gather()` on the same token and asserts exactly one
non-`None` result.

**Remaining, accepted gap:** the silence between a call's last turn and hang-up (a few seconds to
tens of seconds, not the whole call) goes unbilled if `/session/end` itself never fires. Small and
bounded, unlike round 1's whole-call gap.

**A second, sharper regression closed in round 3: `resolve()` could resurrect a deleted binding.**
Round 2's `resolve()` needed to persist the metering tick it embedded in the token's own JSON
payload, so its TTL-slide became a `SET ...` instead of the original `EXPIRE`. A plain `SET`
unconditionally *recreates* a missing key — so if `/session/end`'s `resolve_and_revoke` (GETDEL)
or a terminal-rejection `revoke()` deleted the binding between this `resolve()`'s `GET` and its own
`SET`, the delete was silently undone with a fresh full TTL. Two concrete consequences, both
defeating a mechanism this codebase deliberately built: the call kept answering (and metering)
*after* its concurrency slot was released, and a caller could loop on exactly the unfixable
rejection `revoke()` exists to stop (the `voice_custom_llm` terminal-policy-denial path — see
`_TERMINAL_REPLY`'s handling in `voice.py`). Moving the tick to its own key means `resolve()`
never needs to rewrite the token's payload at all any more, so its TTL-slide went back to a bare
`EXPIRE` — a documented Redis no-op on a missing key that can never recreate one, restoring the
property the pre-metering code had for free.
`test_resolve_does_not_resurrect_a_key_deleted_mid_flight` proves this directly: it deletes the
key from inside a patched `redis.get` immediately after `resolve()`'s own read returns (simulating
the exact race), then asserts the key stays deleted.

### 3. Failure ordering and isolation

`resolve()` and `resolve_and_revoke()` both call `_meter_turn()`, which swallows its own Lua `EVAL`
failures internally (logged, not raised) — see that method's docstring. This is deliberate: a
metering hiccup must never break a live conversational turn, since `resolve()` runs inline in the
hot path that also has to keep the call from going silent.

In `/voice/session/end`, `resolve_and_revoke` is called first (it is itself the idempotency gate,
and it swallows its own metering failures), then slot release runs in its own `try`/`except` —
independent of whatever `resolve_and_revoke` did internally. A slot leaked here still ages out via
the concurrency ZSET's own per-member TTL (2h, see `gating.py`). Neither failure is ever surfaced
as a 500 — the route fires at hangup, after the widget has already torn the call down locally, so
there is no caller left who could act on an error response anyway.

### 4. No rate limit on `/voice/session/end`

Round 1 reused `voice_rate_limit` — the same IP-keyed limiter, and the same Redis key prefix
(`http_voice`), as `POST /session/token`. That silently doubled the cost of every voice call (mint
+ end = 2 units against a shared budget) and could 429 the cleanup/billing path itself for
visitors behind shared NAT on a busy tenant site — producing exactly the two symptoms this task
exists to fix. Removed rather than given its own limiter: the route requires possession of an
unguessable `secrets.token_urlsafe(32)` token, is idempotent, and no-ops for an unknown token, so
its abuse surface is a token-scoped kill switch, not resource exhaustion.
`test_session_end_has_no_rate_limit_dependency` asserts `voice_rate_limit` is not in the route's
FastAPI dependency graph — structural, not a call-N-times-and-count-429s test, since the latter
would pass at any sufficiently high shared limit and hide the sharing.

### 5. Docstring accuracy on the security tradeoff

Round 1's docstring said a forged call to this route "self-heals via the token/slot TTLs." That's
wrong for the token: `resolve_and_revoke` **deletes** the binding immediately, so a call against
someone else's still-live token ends that real call early — its next turn 401s and goes silent for
the caller who was still talking, not "recovers on a TTL" the way a leaked slot does. The route's
docstring now names this directly rather than claiming a self-heal property it doesn't have.
Exploitability stays low (the token is a 256-bit unguessable value that only ever appears in the
mint response and ElevenLabs' own request), so this isn't a change in what the route requires —
only in what the comment claims about it.

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/api/v1/endpoints/voice.py` | `POST /session/end` route: no rate-limit dependency, corrected docstring, metering call removed (now internal to the store) |
| `app/schemas/voice.py` | `VoiceSessionEndRequest` (`voice_token: str`), `VoiceSessionEndResponse` (`ok: bool = True`) — unchanged since round 1 |
| `app/services/voice/session_tokens.py` | `VoiceSessionContext.minted_at`; metering tick moved to its own key (`_TICK_KEY_PREFIX`); `mint()` seeds it; new `_meter_turn()` (atomic Lua `EVAL`, round 3) replaces round 2's `_meter_elapsed_since_last_tick`/`_last_metering_tick`; `resolve()`'s TTL-slide back to `EXPIRE` |
| `app/services/voice/metering.py` | New `VoiceMeter.key_and_ttl()` (round 3) — exposes the monthly-minutes key/TTL for `_meter_turn`'s script |
| `tests/voice/test_session_tokens.py` | 19 tests total: token round-trip (pre-existing) + `minted_at`/`resolve_and_revoke` (round 1) + 7 metering tests (single tick, tick advancement, final-delta-on-top-of-turn-billing, concurrent-double-bill (round 3), failure-does-not-lose-the-delta, migration-compat, resurrection (round 3)) |
| `tests/voice/test_metering.py` | Fixed a vacuous assertion (`key.count("-") >= 1`, satisfied by the tenant UUID's own hyphens) with a real `YYYY-MM` regex check |
| `tests/voice/test_session_end.py` | 9 tests: happy path, ceiling-then-freed-then-mint-succeeds, client-duration ignored, idempotent second call, unknown token, metering-fails-still-releases (patches `_meter_turn`, the real seam now), release-fails-still-meters, missing-field validation, no-rate-limit-dependency |
| `tests/voice/test_custom_llm.py` | 1 end-to-end test: a single real turn through `POST /llm/chat/completions` bills minutes with `/session/end` never called at all — the direct proof for the round-2 fix |

API is additive — no existing contract touched. No migration involved.

## Verification

Run with `REDIS_URL=redis://localhost:6379/0` — `.env`'s Docker-internal hostname doesn't resolve
from the host (same caveat as every prior voice-task SOP).

- `tests/voice/` — **94 passed** (92 post-round-2 + 2 new: `test_resolve_does_not_resurrect_a_key_deleted_mid_flight`
  and `test_concurrent_resolves_do_not_double_bill_the_same_delta`, both in `test_session_tokens.py`).
- Full suite — **1021 passed, 1 failed, 33 skipped** (1019 post-round-2 + 2 new). The 1 failure,
  `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`, is the
  same pre-existing dev-DB row-pollution failure documented in every prior voice-task SOP —
  unrelated to this work.
- `black`/`ruff` on the 5 files touched in round 3 — clean.
- **Break-it-then-revert, resurrection fix:** reverted `resolve()`'s TTL-slide from `EXPIRE` back to
  round 2's payload-rewriting `SET`. `test_resolve_does_not_resurrect_a_key_deleted_mid_flight`
  went red — the key it asserts is gone came back. Reverted; suite back to 94 passed.
- **Break-it-then-revert, atomicity fix:** replaced `_meter_turn`'s Lua `EVAL` with an equivalent
  non-atomic Python read/bill/write sequence (the literal round-2 shape).
  `test_concurrent_resolves_do_not_double_bill_the_same_delta` went red with
  `AssertionError: expected ~30s billed once (~0.5 min), got 1.000... min` — exactly the double-bill
  the fix exists to prevent, not a vague failure. Reverted; suite back to 94 passed.
- **Test-isolation fix along the way:** `test_resolve_tolerates_a_payload_minted_before_minted_at_existed`
  originally used a hardcoded token string; `_meter_turn` always writes a tick key with the full
  600s session TTL regardless of the test's own shorter 60s main-key TTL, so a second run within
  that window read a stale tick left by a previous run and failed on
  `assert 1.0166... < 1.0`. Fixed by making the token unique per run (`uuid4().hex` suffix) — the
  same discipline every other test in this suite already uses for exactly this reason.
- Round 2's earlier break-it-then-revert evidence (per-turn metering, final-delta metering,
  rate-limit removal, metering-key format) is unchanged by round 3 and still holds; see the task
  report for the full log.

## Operate / roll back

- No new env var and no feature flag. `/session/end` is unauthenticated like `/session/token` — the
  opaque voice token is the credential. It carries no rate-limit dependency (see §4 above) —
  deliberately different from round 1, which shared `/session/token`'s limiter and silently halved
  its effective budget.
- **Roll back the code:** revert this task's commit(s) on `feat/voice-chat-backend`. Nothing else in
  the codebase calls `resolve_and_revoke` or references `/session/end` yet. Rolling back reopens
  both gaps this task closed — not a safe rollback target once real traffic depends on it.
- Not yet deployed anywhere — this branch hasn't merged to `develop`.

## Follow-ups (documented per review, not fixed in this task)

- **`resolve_and_revoke`'s `GETDEL`-then-`_meter_turn` window is not fully crash-proof.** If the
  process dies between the `GETDEL` (which has already deleted the binding) and `_meter_turn`'s own
  `EVAL` completing, that small trailing delta is lost with no retry path — the token is already
  gone. Bounded to one small trailing delta, not the whole call (which per-turn metering already
  covers), but not zero.
- **A corrupt payload after `GETDEL` leaks the slot and meters nothing.** If `json.loads`/`UUID()`
  raises inside `resolve_and_revoke` (a payload corrupted after mint, not a normal path), the
  exception propagates out of the store method; the route's `except Exception` catches it and
  returns `{"ok": true}` without ever reaching the slot-release call. The slot still self-heals via
  its 2h TTL. Symmetric with `resolve()`'s existing, already-tested corrupt-payload behavior
  (`test_resolve_does_not_extend_ttl_for_a_corrupt_payload`), which was an accepted tradeoff before
  this task and is unchanged by it.
- **`db: Session = Depends(get_db)` in `end_voice_session` is injected but unused.** `VoiceGate`'s
  constructor requires a `db` argument even though `release_slot` never touches `self.db` — this
  predates Task 11 (see `gating.py`) and isn't this task's to fix; changing `VoiceGate`'s
  constructor signature is out of scope here.
- **Turn-time metering bills at turn granularity, not continuously.** A caller in a long silence
  between turns (human thinking, ASR debounce) is not billed for that gap until the NEXT turn
  arrives (or `/session/end` fires). This is a closer approximation of true call duration than
  round 1's whole-call `minted_at` gap, but still not a continuous meter — a periodic heartbeat or
  reconciliation against ElevenLabs' own call-duration data (see below) would close the remaining
  gap for audit-grade billing.
- **A second Redis pool per turn.** `VoiceSessionTokenStore.__init__` constructs its own `VoiceMeter`
  (which lazily opens its own Redis client) on top of the store's own client — a pre-existing
  per-request-object pattern in this codebase (see Task 3's SOP), now amplified onto the hot path
  since `resolve()` runs on every turn rather than once per mint. Flagged in review round 3 as
  "worth a follow-up PR, not a fix round" — not addressed here.
- **No Postgres flush yet.** `metering.py`'s original docstring mentions "a periodic Postgres flush
  for durable reporting" as a future task — Redis remains the only store for monthly minutes today
  (45-day TTL, comfortably past any billing cycle, but not a permanent record).

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
Task 10 SOP: `docs/sop/voice_chat_escalate_to_human_tool.md`
