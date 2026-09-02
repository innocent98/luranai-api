# SOP — Voice chat backend: branch hand-off + full-suite verification (Task 12)

**Status:** `feat/voice-chat-backend` complete through Slice 3, **not merged**, **not
production-ready** · branch = `develop` (`c28283d`) + 31 commits, `97d5a80` through `25dc640`
(`HEAD`) · 2026-08-17
**Area:** whole-branch index, not a single-file change. No source or test files touched by this
document.

This is not a fifth per-change SOP. Tasks 5, 9, 10 and 11 already wrote one each as they shipped
(`voice_chat_gating_and_token_mint.md`, `voice_chat_custom_llm_endpoint.md`,
`voice_chat_escalate_to_human_tool.md`, `voice_chat_usage_metering_and_session_end.md`) — each is
still the authoritative record of *how* its slice works and *why* it was built that way. This
document is the thing none of them are: a map across all four, the full-suite verification gate
before PR, and a single place that states plainly what this branch does **not** finish. Read this
first if you're arriving cold; read the slice SOP it points you to for the mechanism.

---

## Full-suite verification

Tests run on the host against dockerized Postgres + Redis (ports exposed to `localhost`), the same
way every prior voice-task SOP on this branch verified — not `docker exec ... poetry run pytest`
inside the `api` container, which ships a production image with no dev dependencies (`poetry`
itself isn't on `PATH`, and `pytest` isn't installed — confirmed by trying it first: `OCI runtime
exec failed: exec: "poetry": executable file not found`, then `No module named pytest`). `pyproject.toml`
+ `poetry.lock` are present at the repo root and `poetry run` resolves the local virtualenv.

**`REDIS_URL` must be overridden.** `.env:34` is `REDIS_URL=redis://redis:6379/0` — the Docker
Compose service hostname, which does not resolve from the host. Every prior voice-task SOP carries
this same caveat; confirmed again here by reading `.env` directly rather than assuming.

Command, run from the repo root with `luran_db` and `luran_redis` healthy:

```
$ REDIS_URL=redis://localhost:6379/0 poetry run pytest -q --no-cov
```

```
=== 1 failed, 1021 passed, 33 skipped, 182353 warnings in 101.35s (0:01:41) ====
```

Voice suite alone:

```
$ REDIS_URL=redis://localhost:6379/0 poetry run pytest -q --no-cov tests/voice/
====================== 94 passed, 7319 warnings in 2.49s =======================
```

**Matches expectation exactly: 94 voice tests, 1021 full-suite passing, 33 skipped, 1 pre-existing
failure.** No discrepancy to report.

The one failure:

```
FAILED tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation
AssertionError: assert 11 == 0
 +  where 11 = count()
 +    where count = query(AuditReport).count
```

Confirmed to be the documented pre-existing dev-DB row-pollution failure, not a new regression:
the test asserts zero `AuditReport` rows exist after a redirect-confirmation-required path, and
gets 11 — rows left over in the shared dev database from unrelated prior manual/test runs, not
from anything this branch touches. Every prior voice-task SOP on this branch names this same test
and the same root cause. `tests/auditor/` is untouched by any commit on this branch
(`git log --oneline -- tests/auditor/ c28283d..25dc640` returns nothing).

The 182k warnings are entirely `DeprecationWarning` noise (`datetime.utcnow()`,
`asyncio.get_event_loop_policy()`) pre-existing across the suite — not voice-branch related, not
new, not actionable here.

---

## Document map

| Slice | Commits (`git log A..B`, A excluded) | SOP | What it covers |
|---|---|---|---|
| Plan + specs | `c28283d..606757c` | *(no SOP — planning docs)* | `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`, `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`, `docs/superpowers/specs/2026-08-15-voice-chat-spikes.md` |
| Slice 1 — gating + token mint | `606757c..be59687` (Tasks 1–5) | `docs/sop/voice_chat_gating_and_token_mint.md` | `POST /session/token`: feature flag → monthly minute cap → concurrency ceiling, opaque server-minted session token |
| Slice 2 — custom-LLM SSE endpoint | `be59687..dc9ab5f` (Tasks 6–9) | `docs/sop/voice_chat_custom_llm_endpoint.md` | `POST /llm/chat/completions`: EL→Luran auth, OpenAI SSE framing, buffer word, sliding-TTL token resolve |
| Slice 3 — escalation + metering | `dc9ab5f..25dc640` (Tasks 10–12, this doc) | `docs/sop/voice_chat_escalate_to_human_tool.md` + `docs/sop/voice_chat_usage_metering_and_session_end.md` | `POST /tools/escalate_to_human`, `POST /session/end`, per-turn atomic metering |
| Slice 4 — write-action tool webhooks | *(not started)* | — | Blocked on Spike 2; see below |

Full branch range: `c28283d..25dc640` (31 commits). `c28283d` is `develop`'s current tip
(`git rev-parse develop` = `git merge-base develop feat/voice-chat-backend`, confirmed identical —
this branch is a clean fast-forward off `develop`, no rebasing needed before PR).

---

## What is NOT finished — read this before trusting this branch as complete

Everything below was checked directly against the code on this branch, not carried over from the
plan on trust.

### 1. `dynamic_variables` is accepted but not transmitted to ElevenLabs — the single most important gap

`app/api/v1/endpoints/voice.py:179` calls `ElevenLabsClient().mint_conversation_token(...,
dynamic_variables={"voice_token": voice_token})`, and `elevenlabs_client.py:21` carries a
`TODO(spike-1)` on that exact parameter: the real wire shape for attaching dynamic variables to a
WebRTC token mint is unconfirmed, so the opaque token this branch's session-binding design depends
on does not currently arrive at ElevenLabs in practice.

This matters more than a normal follow-up because **both callback endpoints this branch built
identify the caller by that token**: `voice_custom_llm`'s `_extract_voice_token()` and
`escalate_to_human` both resolve `VoiceSessionTokenStore` state from it. Until Spike 1 resolves the
transmission mechanism, neither endpoint has a confirmed way to know which tenant/session a live
EL callback belongs to against a real agent. `_extract_voice_token()`'s fallback to
`elevenlabs_extra_body` and `user` exist precisely because this carrier isn't confirmed live —
they're a hedge, not a second working path.

**Nothing about this branch should read as production-ready while this is open.** It is the
reason Slice 2's SOP says outright: "Do not treat this slice as functionally complete against a
real ElevenLabs agent until Spike 1 resolves this."

### 2. Two de-risking spikes never ran

- **Spike 1 — EL custom-endpoint auth wire shape** (`docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`).
  Gates item 1 above. Also decides whether `verify_elevenlabs_request`'s constant-time
  shared-secret bearer check (`app/services/voice/auth.py`) stays as-is or becomes an HMAC
  signature check — the module docstring names this as a deliberate swap point ("SWAP POINT...
  every caller depends only on this function's contract, never on how that's checked").
- **Spike 2 — write-action dead-air UX** (`docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`).
  Gates Slice 4 entirely. `send_message()` runs the action engine inline today, so a write action
  during a voice call already completes inside the pipeline call with no dead-air mitigation
  beyond the buffer word. Slice 4 (diverting writes to an async tool-webhook pattern) was
  **deliberately not built** — see the plan's decision table
  (`docs/superpowers/plans/2026-08-16-voice-chat-backend.md:1959`): the spike's verdict on
  tolerable dead air decides whether Slice 4 gets built, gets replaced by a spoken pre-ack pattern,
  or gets dropped outright with writes staying in-pipeline. None of those three outcomes has been
  chosen yet.

### 3. `expires_in_seconds` now describes an idle timeout, not an absolute lifetime

Confirmed in `app/services/voice/session_tokens.py`: `mint()` sets the token key's TTL to
`VOICE_SESSION_TOKEN_TTL_SECONDS` (600s), and `resolve()` slides that same TTL back to 600s on
**every** turn (`redis.expire(key, settings.VOICE_SESSION_TOKEN_TTL_SECONDS)`, line 237). The
600s the mint response reports in `expires_in_seconds` (`app/api/v1/endpoints/voice.py:224`) is
therefore the window before the *next* turn, not a ceiling on total call length — a call with
turns spaced under 10 minutes apart can run indefinitely.

Per Slice 2's SOP: the shipped frontend widget types and maps this field but never acts on it (no
timer, no re-mint call) — harmless today because nothing reads the field as a deadline, but a
latent trap for whoever builds client-side re-minting later. That implementer needs to know the
field's meaning changed underneath them from Slice 1's original design (a fixed absolute
lifetime) to Slice 2's sliding-TTL idle timeout.

### 4. A crashed browser still under-meters, just less than before

Per-turn metering (`voice_chat_usage_metering_and_session_end.md` §1–§2) bills the wall-clock
delta since the token's last metering tick on every `resolve()` call — every completed turn is
billed regardless of whether `/session/end` ever fires. That closed the original whole-call gap
(round 1 billed nothing at all for a call that never called `/session/end`). What remains: the
silence between a call's **last** completed turn and hang-up — a few seconds to tens of seconds —
goes unbilled if `/session/end` itself never fires (crash, closed tab, lost network). Bounded to
one partial turn, not the whole call, but not zero.

### 5. `TicketService.get_ticket_by_session` has a live `MultipleResultsFound` bug — deliberately untouched

`app/services/ticket_service.py:170-189`: `order_by(desc(Ticket.created_at))` combined with
`.scalar_one_or_none()` and no `.limit(1)`. Any session with more than one ticket raises
`MultipleResultsFound` instead of returning the newest one. Confirmed live on this branch, not
introduced by it. Task 10's SOP (`voice_chat_escalate_to_human_tool.md`, "The `get_ticket_by_session`
bug") is why `escalate_to_human` uses a new method, `get_open_ticket_for_session`, instead of this
one — but the buggy method itself was left exactly as found, per the brief's explicit instruction
not to touch it here. Needs its own fix + regression test in a separate PR; every existing caller
of `get_ticket_by_session` today breaks the moment a session accumulates a second ticket.

### 6. Deferred minors, none blocking, all confirmed against the code

- **`revoke()` leaves the metering tick behind.** `session_tokens.py:283-285` deletes only
  `voice:session_token:{token}`, never `voice:session_tick:{token}`. Bounded orphan: the tick key
  carries the same 600s TTL as the main key (`_meter_turn`'s `tick_ttl` argument is
  `VOICE_SESSION_TOKEN_TTL_SECONDS`), so it self-expires within 10 minutes of the last turn.
- **`_meter_turn`'s Lua `EVAL` spans two keys with no shared hash tag** — `tick_key` (per-token) and
  `meter_key` (per-tenant-per-month, `voice:minutes:{tenant_id}:{YYYY-MM}`). Confirmed in
  `_METER_TURN_SCRIPT` (`session_tokens.py:76-100`): `KEYS[1]`/`KEYS[2]` are unrelated key
  namespaces. Redis Cluster routes multi-key `EVAL`s by hash slot and raises `CROSSSLOT` unless all
  keys share a slot (via `{...}` hash-tagging); this script would break outright under Cluster as
  written. Not an issue on the single-node Redis this deploys against today.
- **`metering.py`'s module docstring still points at "Task 9"** for work that actually landed in
  Task 11: "Task 9 extends this same class with session-lifecycle metering... keep this minimal
  until then" (`app/services/voice/metering.py:7-9`). Confirmed — the plan's Task 9 was the Slice 2
  SOP task, not a metering task; per-turn metering landed in Task 11. Stale, harmless
  (comment-only), worth a one-line fix whenever this file is next touched.
- **The `"user"` rate-limiter strategy may collapse all external callers into one bucket — flag
  this one for operations.** `app/utils/http_rate_limiter.py:147-151`: when
  `request.state.user` is unset (true for both voice endpoints — neither requires
  authentication), the `"user"` strategy falls back to `request.client.host`. Confirmed nowhere in
  the repo sets `FORWARDED_ALLOW_IPS` or configures uvicorn's `--proxy-headers` /
  `forwarded_allow_ips` (`grep -rn FORWARDED_ALLOW_IPS` returns nothing). If prod publishes the API
  container behind a host nginx the way `voice_chat_gating_and_token_mint.md`'s follow-ups describe,
  `request.client.host` on every request would be the nginx hop's own address, not the visitor's —
  collapsing `voice_rate_limit`'s 10/min ceiling from per-visitor to **global** for all voice-mint
  traffic. Pre-existing behavior shared with `/chat/message`, not introduced by this branch, but it
  becomes a real production concern the moment voice traffic is real. Verify client-IP resolution
  end-to-end before enabling voice for real tenants.

---

## Slice 4 status: not started, correctly so

`send_message()` executes write actions inline inside the pipeline call today. Slice 4 would
divert those writes out to an async EL tool-webhook pattern — real added complexity, only
justified if Spike 2 shows in-pipeline write latency produces intolerable dead air. The plan's
decision table (`docs/superpowers/plans/2026-08-16-voice-chat-backend.md:1959`) names three
possible outcomes once the spike lands (drop Slice 4 entirely / build the tool-webhook endpoint /
switch to a spoken pre-ack pattern instead) — none chosen yet. Do not start Slice 4 implementation
without first writing that decision up as its own plan slice, per the brief.

---

## Operate / roll back

- **This branch has never been deployed anywhere and has not merged to `develop`.** Nothing in
  `docs/sop/` for this branch describes a live rollback of running infrastructure — every "roll
  back" note in the four slice SOPs means reverting the branch's own commits, not undoing a
  production change.
- **To revert the entire branch's work:** `git revert --no-commit c28283d..25dc640` (31 commits,
  `97d5a80` through `25dc640`) — `c28283d` is `develop`'s tip and is correctly excluded by `..`.
  Double-check this exact form before running it: an inverted range (`25dc640..c28283d`) silently
  produces an empty commit list rather than an error, which is exactly the mistake this branch's
  own SOPs flag as having happened twice already during this work.
- **No env vars are new from this task.** `VOICE_SHARED_SECRET`, `ELEVENLABS_AGENT_ID`,
  `ELEVENLABS_API_KEY` (Slices 1–2) are the only voice-specific settings, and none are yet in
  `.env.example` — add them once Spike 1 lands and a real secret is provisioned, per Slice 2's SOP.
- **Before this branch goes to PR:** the reader of this document is the gate. If the answer to "is
  `dynamic_variables` transmission resolved" is still no, this branch should not be described to
  anyone downstream as feature-complete against a real ElevenLabs agent — only as
  spike-independent groundwork that is code-complete and tested against mocks.

## Follow-ups

Everything in "What is NOT finished" above is the actual follow-up list for this branch. Nothing
new is being deferred by this document itself — it verifies and indexes, it doesn't ship code.

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Spike notes: `docs/superpowers/specs/2026-08-15-voice-chat-spikes.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
Slice 1 SOP: `docs/sop/voice_chat_gating_and_token_mint.md`
Slice 2 SOP: `docs/sop/voice_chat_custom_llm_endpoint.md`
Slice 3 SOPs: `docs/sop/voice_chat_escalate_to_human_tool.md`, `docs/sop/voice_chat_usage_metering_and_session_end.md`
