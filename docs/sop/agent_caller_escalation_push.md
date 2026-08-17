# SOP — Agent Caller: escalation push infrastructure

**Status:** Shipped (branch, pre-PR) · `feat/agent-caller-push` · commits `1bdbc80..195ce5b` (17 commits) · 2026-08-16/17
**Area:** `app/services`, `app/workers`, `app/api/v1/endpoints/agent_app.py`, `app/db/models`, one migration

---

## What shipped

A push-based escalation ring for the Agent Caller app: the moment Luran's AI escalates a
conversation, every available agent's phone rings via FCM. First to tap "Take Over" atomically
claims the ticket and the live text chat; everyone else's ring is cleared. If nobody accepts
within the wait window, a single supervisor-fallback tier rings once. Supplements — never
replaces — the existing email escalation.

Design spec: `docs/superpowers/specs/2026-08-15-agent-caller-backend-escalation-push-design.md`
Plan: `docs/superpowers/plans/2026-08-16-agent-caller-backend-push.md`

## Why

Email escalation alone leaves an escalated conversation sitting unseen in an inbox — there is
no guarantee any agent notices it before the customer gives up. Broadcasting a push ring to
every available agent, plus one supervisor-fallback timer if nobody claims it, gives a "never
missed" guarantee without building an N-tier ordered escalation chain (more coordination
surface, more failure modes, no evidence a 2-tier system needs the extra tiers).

## How it works

**First-accept-wins via an atomic conditional UPDATE.** There is no lock, no coordinator
process, no in-memory mutex. Claiming a ticket is a single
`UPDATE tickets SET assigned_to = :agent, status = 'active' WHERE id = :id AND assigned_to IS
NULL` — the unique write itself *is* the coordination primitive. Under concurrent claims,
Postgres serializes the writers; exactly one `UPDATE` reports `rowcount == 1`, every other
concurrent attempt reports `rowcount == 0`. No `SELECT ... FOR UPDATE`, no advisory lock, no
retry loop needed.

**An idempotent deferred fallback, not cancellation.** Rather than scheduling the
supervisor-fallback job and then racing to cancel it when someone claims the ticket, the
fallback job always runs at `T+90s` and simply no-ops if the ticket is already
claimed/resolved/missed by the time it fires. This trades a small amount of wasted ARQ work for
eliminating an entire class of cancel-race bugs (job already dequeued when the cancel arrives,
cancel silently dropped, etc.).

**Three distinct `NotifEvent` values, not two.** `ESCALATION_ALERT` (broadcast tier),
`ESCALATION_FALLBACK` (supervisor tier), `ESCALATION_MISSED` (post-timeout notice) are separate
enum values specifically because a `TENANT_ADMIN` who is also `available` sits in *both* the
broadcast tier and the supervisor tier for the same ticket. `NotificationLog` dedupes on
`(user_id, ticket_id, event)` — collapsing broadcast and fallback into one event value would let
the dedupe index silently swallow that admin's supervisor-tier ring after they'd already been
notified (and ignored/missed) the broadcast ring. Three values keep the two rings independently
dedupeable for the same recipient.

**Recipient resolution at send time, not at escalation time.** Who is "available" can change
during the 90-second wait window (an agent goes off-shift, another comes online). Each job
(`alert_escalation`, `alert_escalation_fallback`) re-queries `AgentAlertService.devices_for` at
the moment it actually sends, rather than the routing being fixed by whoever was available when
the AI first escalated. `devices_for` requires an explicit `tenant_id` argument (not inferred
from context) because that argument decides which physical phones ring — a missing or wrong
tenant scope there is a cross-tenant notification leak, not a cosmetic bug.

**Rejected alternatives:**
- *A parallel `Escalation` table* — tracking claim state as a new row type, joined back to
  `tickets`. Rejected: `tickets.assigned_to` / `tickets.status` / `tickets.acknowledged_at`
  already model exactly this state; a second table would need to stay in lockstep with the one
  that's the actual source of truth for "who owns this conversation," inviting drift.
- *An N-tier ordered escalation chain* (tier 1 agents, then tier 2, then tier 3, ...) —
  rejected as unjustified complexity for the current agent-count/shift-pattern; broadcast +
  one fallback tier covers the "never missed" requirement with far less state machine.
- *`google-auth` as a new dependency* — the FCM HTTP v1 client mints its own OAuth2 bearer
  token in-process via a JWT-bearer grant signed with `python-jose` (already a project
  dependency), deliberately avoiding a `poetry.lock` change and the Docker rebuild that would
  trigger.
- *CallKit / Critical Alerts (iOS)* — rejected for v1; ships as a normal FCM push (respects the
  device's silent switch). iOS is pending an APNs auth key upload regardless (see Follow-ups).

## What's involved

| File | Change |
|---|---|
| `alembic/versions/ac30_agent_caller_push.py` | migration `ac30agentcaller` (down-revision `ab20blockedquality`) |
| `app/db/models/agent_device.py` | new `AgentDevice` model (device token registry) |
| `app/db/models/user.py` | `users.availability_status` (+`AvailabilityStatus` enum) |
| `app/db/models/conversation.py` | `TicketStatus.MISSED`, `tickets.acknowledged_at` |
| `app/db/models/notification_log.py` | `NotifChannel.PUSH`; `ESCALATION_ALERT` / `ESCALATION_FALLBACK` / `ESCALATION_MISSED` |
| `app/core/config.py` | `FIREBASE_CREDENTIALS_JSON`, `AGENT_ALERT_ENABLED`, `AGENT_ALERT_WAIT_SECONDS`, `AGENT_ALERT_ANDROID_CHANNEL_ID` |
| `app/services/fcm_client.py` | FCM HTTP v1 client; in-process JWT-bearer OAuth2 mint |
| `app/services/agent_alert_service.py` | recipient-tier resolution, push payload builders, `NotificationLog` dedupe, stale-token pruning |
| `app/services/notification_enqueue.py` | ARQ enqueue helpers for the alert jobs |
| `app/services/conversation_service.py` | `escalate_session` re-escalation dedup guard |
| `app/workers/agent_alerts.py` | ARQ jobs: `alert_escalation`, `alert_escalation_fallback`, `clear_agent_alert` |
| `app/workers/document_processor.py` | job registration in `WorkerSettings.functions` |
| `app/api/v1/endpoints/agent_app.py` | `/api/v1/agent/*` router: identity, device register/unregister, availability, escalation list/detail, atomic acknowledge |
| `app/api/v1/endpoints/chat.py` | ring wired into all six escalation call sites |
| `app/api/deps.py` | `get_current_agent` dependency |
| `app/api/v1/api.py` | router mount |
| `app/schemas/agent_app.py` | request/response Pydantic models for the agent router |
| `tests/agent_caller/` (11 files) | dedicated suite — models, FCM client, alert service, jobs, devices/availability/escalations/acknowledge endpoints, chat ring wiring, dedup guard |

## Verification

All commands run from the worktree root, `af4a038`, poetry env `luran-TZkEOXYU-py3.11`.

### 1. Lint gates (CI's exact commands)

```
$ poetry run ruff check app/
All checks passed!

$ poetry run black --check app/
All done! ✨ 🍰 ✨
218 files would be left unchanged.
```

### 2. Agent-caller suite

```
$ poetry run pytest tests/agent_caller -q --no-cov
======================= 75 passed, 36 warnings in 1.21s ========================
```

### 3. CI regression files (chat flow router + action-call regressions)

```
$ poetry run pytest tests/api/v1/test_chat_flow_router.py tests/api/v1/test_flow_action_call_regressions.py -q --no-cov
======================= 12 passed, 9 warnings in 10.60s ========================
```

### 4. OpenAPI surface

```
$ poetry run python -c "from app.main import app; print('\n'.join(sorted(p for p in app.openapi()['paths'] if '/agent' in p)))"
/api/v1/agent/availability
/api/v1/agent/devices
/api/v1/agent/escalations
/api/v1/agent/escalations/{ticket_id}
/api/v1/agent/escalations/{ticket_id}/acknowledge
/api/v1/agent/me
/api/v1/analytics/agents
/api/v1/analytics/agents/{agent_id}
/api/v1/chat/sessions/{session_id}/agent-message
```

The six agent-caller endpoints are exactly the ones spec'd. The `'/agent' in p` filter is a
substring match, so it also picks up three **pre-existing, unrelated** paths that happen to
contain the substring `agent` (`analytics/agents`, `analytics/agents/{agent_id}`,
`chat/.../agent-message`) — confirmed via `git log` that these predate this branch. Not a
finding.

### 5. Worker job registration

```
$ poetry run python -c "
from app.workers.document_processor import WorkerSettings
n={f.__name__ for f in WorkerSettings.functions}
assert {'alert_escalation','alert_escalation_fallback','clear_agent_alert'} <= n, n
print('registered OK')
"
registered OK
```

### 6. Migration round-trip against real Postgres

Disposable container `luran_migtest`, host port 55432, schema pre-loaded to head.

```
$ DATABASE_URL="postgresql://luran:luran@localhost:55432/luran_migtest" poetry run alembic downgrade -1
Running downgrade ac30agentcaller -> ab20blockedquality, agent caller: agent_devices, availability, ticket acknowledgement, push channel

$ DATABASE_URL="postgresql://luran:luran@localhost:55432/luran_migtest" poetry run alembic upgrade head
Running upgrade ab20blockedquality -> ac30agentcaller, agent caller: agent_devices, availability, ticket acknowledgement, push channel

$ DATABASE_URL="postgresql://luran:luran@localhost:55432/luran_migtest" poetry run alembic current
ac30agentcaller (head)
```

Clean round-trip: down, up, and `current` lands back on head with no manual intervention.

### 7. Full-suite context

```
$ export ENCRYPTION_KEY=<from .env>
$ DATABASE_URL="postgresql://luran:luran@localhost:55432/luran_migtest" poetry run pytest tests -q --no-cov
999 passed, 33 skipped, 246 warnings in 158.36s (0:02:38)
```

**The full suite is clean — zero failures, zero errors — when pointed at a Postgres that is
actually at migration head.** This branch leaves the suite in no degraded state.

Two earlier invocations produced misleading counts, both purely artifacts of which database was
targeted, not of anything in this branch:

- **Forcing `DATABASE_URL=sqlite:///:memory:`** produces `psycopg2`-flavored failures around
  `no such function: gen_random_uuid` (and similar) — SQLite cannot provide a Postgres
  `server_default`. This is a dialect limitation of running Postgres-shaped tests against SQLite,
  not a regression.
- **Running with no `DATABASE_URL` override** falls back to `tests/conftest.py`'s default,
  `postgresql://luran_user:luran_password_dev@localhost:5432/luran_db` — whichever local Postgres
  happens to be at that address. If that database has not had migration `ac30agentcaller` applied,
  every test touching a column this branch added (e.g. `users.availability_status`) fails with
  `psycopg2.errors.UndefinedColumn: column "availability_status" of relation "users" does not
  exist`. This is a missing migration on that particular database, not a regression.

Both failure modes are diagnostics about the database the suite happened to hit, not about the
branch. The next person to run `pytest tests` locally will likely hit one of the two unless they
either apply `ac30agentcaller` to their local dev DB first, or point `DATABASE_URL` at a Postgres
already at head (as done here, and as Step 6 already does). The dedicated `tests/agent_caller` run
(Step 2) and the two named CI regression files (Step 3) additionally confirm the feature in
isolation, and both are green.

### Controller-run checks (already performed against real Postgres, cited here)

Per the task brief, the following were already verified during development and are cited rather
than re-run:
- The migration applies cleanly and round-trips (re-confirmed independently in Step 6 above).
- `NotificationLog`'s `(user_id, ticket_id, event)` dedupe correctly ignores a replayed broadcast
  send while still granting the same user's distinct supervisor-tier (`ESCALATION_FALLBACK`)
  claim — proving the three-value `NotifEvent` design (see How, above) rather than colliding them.
- The atomic claim (`acknowledge`) under three concurrent connections racing the same ticket
  yielded rowcounts `[0, 0, 1]`, with `tickets.assigned_to` matching the single winner — the
  first-accept-wins guarantee under real concurrency, not just in a single-threaded test.

## Fix wave — final whole-branch review (2026-08-17)

A final whole-branch review found six issues (one critical) that per-task review
had missed, plus three named test gaps. Full per-finding detail, root cause and
verbatim command output:
`.superpowers/sdd/2026-08-16-agent-caller-backend-push/final-fix-wave-report.md`.
Commits `f1b64f6`, `0be75bf`, `195ce5b`.

| # | Severity | Issue | Fix |
|---|---|---|---|
| F1 | Critical | Re-escalating an already-`ESCALATED` session minted a second ticket and rang every agent's phone a second time | `TicketService.get_open_ticket_for_session` + `chat.py`'s `_get_or_create_escalation_ticket` reuse the live ticket at all six escalation sites; ring/email only fire on an actual creation |
| F2 | Important | The fallback's clear job id was fixed, so a claim landing mid-supervisor-push could never be cleared | `enqueue_clear_alert_async` gained a `source` suffix (`claim` vs `fallback`); a fresh pre-push re-read narrows the window, a late clear covers what's left |
| F3 | Important | The `MISSED` status write was read-then-write, not atomic | Conditional `UPDATE ... WHERE assigned_to IS NULL AND status = 'open'`, gated on `rowcount`, matching the claim endpoint's own pattern |
| F4 | Minor | `acknowledge`'s conditional UPDATE didn't check status, so a `RESOLVED`/`CLOSED` unassigned ticket (from `auto_resolve_stale_tickets`) could be resurrected | Added `status IN (OPEN, MISSED)` to the WHERE clause — `MISSED` stays claimable |
| F5 | Minor | `enqueue_agent_alerts_async` sits on the customer chat-reply path; arq's default retry loop could add several seconds during a Redis blip | `conn_retries = 0` on that one enqueue path only |
| F6 | Minor | `prune_token` deleted by `device_token` alone, but the unique constraint is `(user_id, device_token)` — two agents can share a token after a device handover | Scoped the delete to `(user_id, device_token)` |

This wave also closed three test gaps the original review named: a real
two-request escalation through `POST /api/v1/chat/message` (not
`escalate_session` in isolation), a claim landing during the fallback (both
mid-gap and mid-push variants), and a negative assertion that `device_token`
never appears in any `/api/v1/agent/*` response body.

Updated verification (supersedes the "999 passed" figure in Step 7 below):
`tests/agent_caller` is now 81 passed (was 75); the full suite against a
migrated Postgres is 1006 passed / 33 skipped / 0 failed (was 999/33/0) — net
+7 for the 7 new tests this wave added, zero regressions.

## Operate / roll back

- **Kill switch:** `AGENT_ALERT_ENABLED=false` disables all pushes instantly, no deploy needed —
  every send path checks the flag before calling FCM.
- **Roll back schema:** `alembic downgrade -1` reverses migration `ac30agentcaller` cleanly
  (verified in Step 6).
- **Before deploying:** `FIREBASE_CREDENTIALS_JSON` must be added to the encrypted
  `.env.staging` / `.env.production` bundles via `scripts/env.sh` **before** this branch reaches
  those environments. Without it, every send attempt logs `FCMConfigError` — the ring silently
  never fires (email escalation still works, so this fails safe, but silently).

## Follow-ups

- **Live FCM smoke test** — the client has never yet been exercised against real Google
  credentials/device tokens; only unit-tested against a faked HTTP layer. First deploy to
  staging should include one manual end-to-end ring.
- **iOS** — pending the APNs auth key upload to the Firebase project; until then the ring only
  reaches Android devices.
- **Device-upsert TOCTOU race** — two near-simultaneous `POST /agent/devices` calls for the same
  token could both see "no row" and both insert; worth an `ON CONFLICT` upsert like
  `docs/sop/analytics_visit_idempotent_upsert.md`'s fix for the same class of bug.
- **N+1 on the escalation list endpoint** — `GET /api/v1/agent/escalations` is the
  highest-traffic endpoint in this feature (polled by every agent's phone); the session lookup
  per ticket should move to eager loading.
- **No alert on enqueue failure** — the clear-enqueue failure path only logs; consider wiring it
  to Sentry/an on-call alert so a stuck "phone still ringing after claim" doesn't go unnoticed.
- **Per-tenant wait window** — `AGENT_ALERT_WAIT_SECONDS` is a single global value; larger
  tenants with more agents may want a shorter fallback window.
- **Time-to-acknowledge metrics** — `tickets.acknowledged_at` is now recorded but nothing yet
  reads it into a dashboard metric.
