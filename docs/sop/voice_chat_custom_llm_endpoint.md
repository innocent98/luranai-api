# SOP — Voice chat: custom-LLM SSE endpoint (Slice 2)

**Status:** Shipped on branch `feat/voice-chat-backend` (not yet merged) · code commits `9f66322`
through `247babf` (`git log be59687..247babf` for the full range — `be59687` is the last commit of
Slice 1, excluded) · 2026-08-17
**Area:** `app/services/voice/auth.py`, `app/services/voice/sse.py`,
`app/api/v1/endpoints/voice.py`, `app/services/voice/session_tokens.py`,
`app/utils/http_rate_limiter.py`

This is a **new document**, not an extension of `voice_chat_gating_and_token_mint.md` (Slice 1).
Slice 1 covers a different endpoint (`POST /session/token`) with its own gating/concurrency/
tenant-isolation story; this slice covers the endpoint ElevenLabs calls *back into* mid-call
(`POST /llm/chat/completions`), with a distinct security model (shared-secret bearer auth, not
tenant gating) and distinct failure semantics (never a non-200, because silence on a live call is
unrecoverable). The two files already follow the project's one-doc-per-slice pattern used
elsewhere in `docs/sop/` (e.g. `auditor_blocked_state_and_dns_perf.md` vs
`auditor_bot_protection_resilience.md` for adjacent but distinct auditor concerns). Cross-links
are added at the bottom of both documents.

---

## What shipped

The other half of the voice-chat backend: `POST /api/v1/voice/llm/chat/completions`, the
OpenAI-compatible "custom LLM" endpoint ElevenLabs Agents calls once per conversational turn.
Three pieces, one per task:

- **Task 6** (`9f66322`, `ce88f23`) — `verify_elevenlabs_request`
  (`app/services/voice/auth.py`), a route dependency that 401s unless the request carries
  `settings.VOICE_SHARED_SECRET` as a bearer token, checked with `hmac.compare_digest`. Review
  returned 0 Critical, 0 Important — the first backend task on this branch to clear review with
  no fix round — plus 3 deferred Minors, none blocking (e.g. the two 401 branches return
  distinguishable `detail` strings, "voice auth not configured" vs "invalid credentials", a
  low-severity reconnaissance signal for a service-to-service caller; `.superpowers/sdd/2026-08-16-voice-chat-backend/progress.md:389-411`).
- **Task 7** (`5207653`, `022e08a`, `cf51ed4`) — `stream_reply` / `openai_chunk`
  (`app/services/voice/sse.py`), OpenAI chat-completion SSE chunk framing, plus the
  `BUFFER_WORD = "... "` constant.
- **Task 8** (`f575d5d`, `8fe4960`, `247babf`) — the endpoint itself
  (`app/api/v1/endpoints/voice.py:337-424`), wiring auth, SSE framing, and the chat pipeline
  together. Went through two full external review rounds after the initial implementation:
  round 2 (commit `8fe4960`) found 7 Important issues plus 2 Minors, round 3 (commit `247babf`)
  found 5 more (the report is explicit that none of these 5 were new Critical/Important — three
  were the same failure *shapes* from round 2 recurring one layer down, e.g. the TTL-render race
  in `session_tokens.py::resolve()` below). Full finding-by-finding detail:
  `.superpowers/sdd/2026-08-16-voice-chat-backend/task-8-report.md`.

## Why

Slice 1 proved a tenant can be gated onto voice and handed a token. This slice is what makes the
call actually *say something back*: ElevenLabs' Agents platform supports a "custom LLM" mode where
it POSTs an OpenAI-shaped chat-completions request to a URL we control instead of using its own
model, and expects an OpenAI-shaped SSE stream back. Luran already has a chat pipeline with the
product's actual behavior (KB grounding, intent handling, flow logic, lead scoring, the action
engine) — the only question was how to connect that pipeline to ElevenLabs' expectations without
duplicating it.

## How it works

### The design bet: no second brain

The endpoint's core logic is one call:

```python
chat_response = await send_message(
    MessageRequest(message=user_text, session_id=context.luran_session_id,
                   tenant_id=context.tenant_id),
    db,
)
```

`send_message` is the exact function `POST /chat/message` calls (`app/api/v1/endpoints/chat.py`).
Voice gets parity with text chat *by construction*, not by keeping two implementations in sync:
same KB retrieval, same intent/flow logic, same lead scoring, same action engine. There is no
voice-specific prompt, no voice-specific pipeline, nothing that can silently drift from what a
text user experiences. The tradeoff is that voice inherits every latency and behavior
characteristic of the text pipeline too — including that it doesn't stream.

### Why it re-emits rather than streams — and why the buffer word is load-bearing

Nothing in this codebase streams. `send_message` returns a complete `ChatResponse` after the full
pipeline run (RAG, LLM call, action engine), not a token stream. Refactoring the product's most
critical endpoint to grow a streaming seam, just to enable voice, was judged the larger risk and
explicitly rejected — see the plan's "Spec Corrections" #1. So `voice_custom_llm` **awaits the
finished reply** and `_speak()` (`voice.py:325-334`) re-frames it as OpenAI SSE via
`stream_reply()` afterward — nothing is progressive; the frames are pre-materialized into a list
before the `StreamingResponse` is even constructed.

That means the caller hears **dead air** for the entire pipeline latency unless something fills
it. `stream_reply()`'s `buffer_word` parameter (default `True`) yields `BUFFER_WORD = "... "` as
the very first content frame, before the real reply is ready — the trailing space is required by
ElevenLabs so its TTS has something to give prosody to while the rest of the completion streams
in (`sse.py:13-15`, spec §3). **This is load-bearing, not decoration.** A future refactor that
"tidies" that trailing space, or reorders `stream_reply()` so the buffer word isn't strictly
first, silently reintroduces dead air on every voice turn — and would likely pass unnoticed by
tests written as loose substring checks. Round-3 finding #3 (`task-8-report.md:537`) is exactly
that failure shape, one layer over: three of this endpoint's own tests asserted things like
`"sorry" in resp.text.lower()`, which stays green even if the wrong fixed reply were spoken
entirely — tightened to exact full-frame string equality (e.g.
`spoken == "... I am sorry, ... Could you say that again? "`) so the buffer word's position is
actually part of what's asserted, not incidental to it.

### Where tenant identity comes from

The endpoint never reads a tenant or session id ElevenLabs might put in the request body. Identity
comes exclusively from resolving Slice 1's opaque `voice_token` server-side:

- `_extract_voice_token()` (`voice.py:220-239`) pulls the token from whichever carrier ElevenLabs
  used — `elevenlabs_extra_body.voice_token`, `dynamic_variables.voice_token`, or `user`, in that
  order — because Spike 1 hasn't pinned down which one production EL actually sends. All three are
  opaque-token carriers, never trusted ids.
- `VoiceSessionTokenStore().resolve(token)` (`session_tokens.py:54-81`) is the only source of
  `tenant_id`/`luran_session_id` used downstream. An unresolvable token is a `401` before anything
  else runs (`voice.py:372-378`).

This mirrors Slice 1's pattern almost exactly: `/session/token`'s tenant-isolation boundary loads
the `ConversationSession` filtered on both `id` and `tenant_id` and returns an identical 404 for
"no such session" and "wrong tenant" so there's no existence oracle. This endpoint doesn't need
that specific check (there's no client-supplied session id to validate against a tenant — the
resolved context *is* the tenant), but the underlying discipline is the same: never trust an
identity the caller could shape, only what our own server minted and can look up.

### The exception structure

This is the subtlest part of the endpoint and the part most likely to be "simplified" incorrectly
in a future change, so it's worth spelling out exactly (`voice.py:370-384`):

```python
try:
    token = _extract_voice_token(payload)
    context = await VoiceSessionTokenStore().resolve(token)
    if context is None:
        raise HTTPException(status_code=401, detail="unknown voice session")
    user_text = _last_user_message(payload)
except HTTPException:
    raise
except Exception:
    return _speak(_FALLBACK_REPLY, model=model)
```

The 401 raise sits *inside* the same `try` block as the resolution and extraction logic that can
raise ordinary exceptions (a Redis outage, a malformed body) — but it survives as a real 401
because `except HTTPException: raise` is checked **before** the broad `except Exception` that
turns everything else into a 200-with-fallback-audio. Get the ordering or the specificity wrong —
collapse both into one `except Exception` catch, say — and the 401 silently becomes
`_speak(_FALLBACK_REPLY, ...)` instead.

Further down, the same shape repeats around `send_message` (`voice.py:393-416`), and here the
*reason* it matters is concrete, not theoretical: `send_message`'s own `HTTPException`s are
policy/entitlement rejections that will **never** succeed on retry — a lapsed subscription
(402 conversation-limit-reached) or a vanished session (404). An earlier version of this endpoint
caught those the same way as a generic pipeline failure and spoke `_FALLBACK_REPLY` ("could you
say that again?") — which invites the caller to retry, and `send_message` rejects again, forever,
burning ElevenLabs minutes on a call that can never succeed while the actual problem (an unpaid
invoice) stays completely invisible to the caller. The fix (round-2 finding #3,
`task-8-report.md:250`) is `except HTTPException` catching those specifically, speaking the
distinct `_TERMINAL_REPLY` ("please check your account or contact support. Goodbye"), and revoking
the voice token so the *next* turn 401s cleanly instead of repeating the same unfixable rejection.
Genuine pipeline failures still fall through to the broad `except Exception` → `_FALLBACK_REPLY`,
where "could you say that again?" is the right invitation because the failure might genuinely be
transient.

Revoking the token ends **our ability to answer further turns**, not the call — the WebRTC session
stays open and keeps billing until the caller or the widget disconnects; the next turn just 401s
into a dead line. Round-3 finding #5 (`task-8-report.md:588`) exists specifically because three
comments got this framing wrong ("ends the call") and were corrected — worth knowing precisely
because a reader skimming an older comment would draw the wrong conclusion about what revocation
actually does.

### The three-way reply split

Three distinct "nothing useful happened" situations get three distinct fixed replies, deliberately
not collapsed into one generic error string, because silence is the worst possible outcome on a
voice call and each situation should tell the caller something true about what happened:

| Situation | Constant | When |
|---|---|---|
| No usable transcript reached the pipeline at all | `_NO_INPUT_REPLY` | `_last_user_message()` found no `role: "user"` entry, or it was empty/whitespace-only (a realistic partial-ASR artifact) |
| Pipeline ran and returned nothing speakable | `_EMPTY_REPLY` | `send_message` succeeded but `chat_response.message.content` is empty/whitespace — the pipeline shrugging, not breaking |
| Pipeline raised | `_FALLBACK_REPLY` | any non-`HTTPException` from `send_message`, or a resolution/extraction failure before the pipeline was even reached |

Plus two more fixed replies for cases layered on top: `_TERMINAL_REPLY` for the unfixable-rejection
case above, and `_RATE_LIMITED_REPLY` ("Sorry, one moment please.") when the per-turn throttle
below trips. All five paths route through the same `_speak()` → `stream_reply(..., buffer_word=True)`
call, so the buffer-word guarantee is never bypassed by any of them, and every path returns `200`
— the endpoint's one hard rule, because ElevenLabs has no way to render a non-200 as audio to the
caller. The **only** exception is auth/identity failures (missing shared secret, unresolvable
token): those stay real 401s, because ElevenLabs itself — not the end caller — is on the other end
of that failure.

### Per-turn rate limiting, keyed by token not IP

`voice_llm_turn_rate_limiter` (`app/utils/http_rate_limiter.py:56-60`, 30 calls/60s) is a
dedicated limiter checked inline via `_turn_allowed()` (`voice.py:292-322`), not a route-level
`Depends(...)` built from `create_rate_limit_dependency`. That helper only keys on `"ip"` or
`"user"` (`http_rate_limiter.py:198-206`) — and ElevenLabs calls this endpoint server-to-server for
every tenant's every turn, almost certainly from a small, shared pool of egress IPs. An IP-keyed
dependency would have bucketed every unrelated tenant's calls together and let one noisy tenant
throttle everyone else on the platform. Keying on the already-resolved, unguessable `voice_token`
instead scopes the limit to the one call actually spamming turns.

`_turn_allowed()` also has its own timeout: `asyncio.wait_for(..., timeout=_TURN_RATE_LIMIT_TIMEOUT_SECONDS)`
(2.0s, `voice.py:74`) around the limiter's `acquire()`, because that call's internal retry loop can
itself sleep up to `window_seconds + 1` (61s) if two concurrent turns on the same token race for
the same slot — and a stuck acquire is worse than a false-positive throttle on the one endpoint
whose entire purpose is preventing silence. On timeout, the turn is throttled (`_RATE_LIMITED_REPLY`),
not hung. On any other infra error, `_turn_allowed()` **fails open** (returns `True`) — a Redis
hiccup must never turn into a silent-feeling throttled turn.

### The sliding token TTL — and the trap it defused, and the one it left behind

`VoiceSessionTokenStore.resolve()` (`session_tokens.py:54-81`) renews the token's Redis TTL to
`VOICE_SESSION_TOKEN_TTL_SECONDS` (600s) on every successful resolve, **after** parsing the stored
payload, not before (`session_tokens.py:62-80`). The ordering matters for the same "unfixable
retry loop" reason as the `HTTPException` split above: a corrupt stored payload (round-3 finding
#1, `task-8-report.md:496`) must be allowed to age out on its original countdown. Renewing the TTL
*before* the `json.loads`/`UUID()` calls that can fail on it would mean every failing turn on a
broken token revived its own lifetime forever — an infinite `_FALLBACK_REPLY` loop that never
self-heals via expiry.

Sliding the TTL forward is also what makes a call longer than 600s keep working at all — without
it, any call running past the mint TTL would go silent mid-conversation the instant a later turn's
`resolve()` 401s, with no way for the caller to know why. But it removed what used to be an
accidental ceiling: before this change, a token's fixed 600s TTL from `mint()` was the *only* thing
bounding how long a voice call could run. With sliding renewal, nothing bounds call duration
anymore except genuine inactivity. Nothing in this slice replaces that ceiling — see Follow-ups.

## Known-open items

These are real gaps, not implementation details. Anyone reading this SOP should treat them as
still-open, not resolved by this slice:

- **`dynamic_variables` is accepted but not transmitted to ElevenLabs.** The mint endpoint
  (Slice 1) calls `ElevenLabsClient().mint_conversation_token(..., dynamic_variables={"voice_token": voice_token})`,
  but the parameter carries a `TODO(spike-1)` (`app/services/voice/elevenlabs_client.py:21`) — the
  real wire shape for attaching dynamic variables to a WebRTC token mint is still unresolved, so
  the opaque token this slice's `_extract_voice_token()` looks for in `dynamic_variables` does not
  currently arrive there in practice. `_extract_voice_token()`'s fallback to `elevenlabs_extra_body`
  and `user` exist precisely because this carrier isn't confirmed live yet. **Do not treat this
  slice as functionally complete against a real ElevenLabs agent until Spike 1 resolves this and
  the mint call site is updated.**
- **Nothing releases a concurrency slot at normal call end.** `VoiceGate.release_slot()` is only
  called from `/session/token`'s own failure cleanup (Slice 1); this endpoint never calls it. Every
  call that completes normally holds its concurrency slot until the sorted-set's 2h eviction window
  passes, not until the caller actually hangs up. That's Task 11 (session lifecycle + metering).
- **`VoiceMeter.add_minutes` has no caller anywhere in the codebase.** There is no mid-call
  minute-cap enforcement today — the monthly cap Slice 1's gating checks is read-only against
  whatever was previously metered, and nothing in this slice or Slice 1 writes new minutes. Task 11
  must add both the caller and, per the point above, a real duration ceiling now that the sliding
  TTL removed the old accidental one.
- **Cross-repo contract drift risk:** `VoiceTokenResponse.expires_in_seconds` (Slice 1) now
  describes an *idle* timeout given the sliding-TTL change, not an absolute token lifetime. The
  shipped frontend maps this field but doesn't act on it (no timer, no re-mint call) — harmless
  today, but a latent trap for whoever implements client-side re-minting later: that implementer
  needs to know the field's meaning changed underneath them.
- **Considered, not built: a terminal flag instead of revoke-and-401.** Under `_TERMINAL_REPLY`,
  the current shape revokes the token so the next turn 401s. A reviewer suggested a terminal
  *flag* on the token binding instead, so `resolve()` still succeeds and a later turn can speak an
  actual goodbye instead of just failing auth. Deferred pending a check of how ElevenLabs' client
  renders a non-200 mid-conversation (task-8-report.md:601-611) — if EL already gracefully ends the
  turn on a failed custom-LLM call, revoke-and-401 is already fine; if it produces dead air, the
  flag becomes clearly worth building.
- **Two deferred Minors**, neither blocking: (1) a narrow rate-limiter budget leak if
  `asyncio.wait_for` cancels `RedisRateLimiter.acquire()` after it has already `zadd`ed but before
  the timeout fires — the slot would be counted without the caller ever seeing "allowed"; (2)
  `_TURN_RATE_LIMIT_TIMEOUT_SECONDS = 2.0` (`voice.py:74`) is an unvalidated guess, not derived from
  a measured p99 — 300-500ms would likely be enough given the fast path is a handful of Redis
  round-trips, not a sleep.

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/services/voice/auth.py` | new — `verify_elevenlabs_request`, fail-closed shared-secret bearer check |
| `app/services/voice/sse.py` | new — `openai_chunk`, `stream_reply`, `BUFFER_WORD` |
| `app/api/v1/endpoints/voice.py` | new route `POST /llm/chat/completions` + all extraction/reply/rate-limit helpers |
| `app/services/voice/session_tokens.py` | `resolve()` now slides the TTL forward, parse-before-renew ordering |
| `app/utils/http_rate_limiter.py` | new `voice_llm_turn_rate_limiter` (30 calls/60s, keyed by voice token) |
| `tests/conftest.py` | new `minted_voice_token` fixture |
| `tests/voice/test_voice_auth.py` | new — 6 tests |
| `tests/voice/test_sse.py` | new — 9 tests |
| `tests/voice/test_custom_llm.py` | new — 15 tests (grew from an initial 7 across both review rounds) |
| `tests/voice/test_session_tokens.py` | extended — sliding-TTL and corrupt-payload-TTL tests added |

Full path: `POST /api/v1/voice/llm/chat/completions`. API is additive — new route on the existing
`voice` router, no existing contract touched.

## Verification

Run this session, `REDIS_URL=redis://localhost:6379/0 poetry run pytest ...` — the value in `.env`
(`redis://redis:6379/0`) is the Docker-internal hostname and doesn't resolve from the host; running
without the override produces spurious failures unrelated to this work.

- `tests/voice/` — **62 passed** (6 `test_voice_auth.py` + 9 `test_sse.py` + 15 `test_custom_llm.py`
  + 7 `test_session_tokens.py` + the rest of Slice 1's voice suite).
- Full suite — **985 passed, 1 failed, 33 skipped**. The 1 failure,
  `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`, is the
  same pre-existing dev-DB row-pollution failure documented in the Slice 1 SOP — unrelated to this
  work, reproduced independent of this branch's changes.
- `ruff check` on every file touched across the three tasks — clean.
- TDD + break-it-then-revert discipline per task, including two full external review rounds on
  Task 8 (7 Important + 2 Minor findings in round 2, 5 further findings — explicitly no new
  Critical/Important — in round 3): full finding-by-finding detail with actual command output in
  `.superpowers/sdd/2026-08-16-voice-chat-backend/task-{6,7,8}-report.md`.

## Operate / roll back

- **`VOICE_SHARED_SECRET` must be set, or every call to `POST /llm/chat/completions` 401s by
  design.** `verify_elevenlabs_request` fails closed on an unset or empty-string secret
  (`auth.py:29-35`) — this is deliberate, not a bug: an unconfigured secret must never degrade into
  an open, unauthenticated endpoint that accepts arbitrary "pipeline turns" from anyone. It
  defaults to `None` and, like the other two voice env vars, is not yet in `.env.example` — add it
  there once ElevenLabs' custom-LLM auth wire shape (Spike 1) is confirmed and a real secret is
  provisioned. Note this is a **separate** switch from Slice 1's `ELEVENLABS_AGENT_ID` /
  `ELEVENLABS_API_KEY`, which gate `/session/token` only — clearing this one has no effect on
  minting, and clearing those has no effect on this endpoint.
- **To disable this endpoint specifically without touching the mint endpoint:** unset
  `VOICE_SHARED_SECRET`. Every callback 401s immediately at the dependency, before the route body
  runs at all.
- **`auth.py` and `_extract_voice_token()` are deliberate swap points, not finished code.**
  `verify_elevenlabs_request`'s module docstring (`auth.py:1-11`) names the exact spot to add an
  HMAC-signature branch (e.g. an `xi-signature: t=…,v0=…` header) if Spike 1 shows ElevenLabs sends
  one instead of a bearer secret — every caller depends only on the function's contract ("raises
  401 unless the request is genuinely from ElevenLabs"), never on how that's checked, so the swap
  is isolated to this one file. `_extract_voice_token()` similarly checks three carriers because
  the real one isn't confirmed (see Known-open items) — expect this function to shrink to a single
  branch once Spike 1 lands.
- **Roll back the code:** revert the 8 commits `9f66322`..`247babf` on `feat/voice-chat-backend`.
  No migration involved in this slice. Nothing else in the codebase imports
  `app/services/voice/auth.py` or `app/services/voice/sse.py`, or references
  `/llm/chat/completions`.
- Not yet deployed anywhere — this branch hasn't merged to `develop`.

## Follow-ups

- **Task 11 (usage metering + session lifecycle)** is what closes the two biggest gaps this slice
  leaves open: releasing the concurrency slot at real call end (not just mint failure), and
  replacing the duration ceiling the sliding TTL removed with a real one — likely wired through the
  currently-uncalled `VoiceMeter.add_minutes`.
- **Task 10 (escalate client-tool handler)** is the other consumer of this endpoint's context —
  out of scope here.
- Spike 1 still open — see Known-open items above for everything gated on it.

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Spike notes: `docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
Slice 1 SOP: `docs/sop/voice_chat_gating_and_token_mint.md`
