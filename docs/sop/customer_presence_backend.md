# SOP - Customer presence, backend (2026-10-14)

**Status:** PR to `develop` from `feat/customer-presence` (not merged). **Spec:** `docs/specs/2026-10-14-customer-presence-design.md` in the workspace repo (PR-D1..D7). **FE contract:** `docs/fe-integration-guide-customer-presence.md` (live captures in `docs/captures/customer-presence/`).

## What shipped
Agents can see whether a customer is `active`, `away` or `offline` (with `last_seen_at`) on the REST lists/details of the agent surfaces, and receive a live `customer_presence` frame on the agent socket when it changes. Customer sockets report visibility with a `presence` frame.

## Why
Field observation #3 (triage 2026-10-10): agents cannot tell whether the customer is still there, especially on missed tickets. Serves the Tenant Agent persona (PRD 1.3, handle escalations). Presence informs and never blocks (PR-D5).

## How
| Decision | Detail |
|---|---|
| Redis is the source of truth | Per session, ZSET `presence:conn:{session_id}`, member `"{conn_id}|{state}"`, score = epoch second the entry stops counting. State is **derived on read** (any `active` -> active, else any `away` -> away, else offline). No stored "online" flag exists, so a dead worker cannot leave a stale one. |
| Grace without timers in the read path | A closed socket's entry is not deleted; its score is pushed to `now + 30 s`. Reads inside the window report the prior state; after it, offline. Reload inside the window emits nothing. |
| TTL 45 s + server keepalive | Each open socket's entry is refreshed every 15 s by the serving worker, and by any `presence` frame. If the worker dies, entries lapse in at most 45 s. The client's ~20 s heartbeat is therefore optional for liveness (old clients keep working) and only repairs dropped state frames. Uvicorn's own WS ping closes half-open peers. |
| State-change-only frames | Lua compare-and-set on `presence:pub:{session_id}` (missing = offline). Whoever recomputes first (connect, `presence` frame, grace end on any worker) emits; the loser sees "unchanged" and stays silent. Safe across workers and simultaneous grace expiry. |
| Delivery | Existing `connection_manager.fanout_event(..., to_agents=True)`: local sockets plus the Redis pub/sub channel, so agent sockets on any worker get it. Tenant isolation is the existing agent-socket session/tenant check; there is no new list socket (lists poll REST). |
| Offline transition | The task spawned when a socket closes releases the entry, sleeps grace + 0.5 s, recomputes, and on a change to offline copies Redis `presence:last:{sid}` to `conversation_sessions.customer_last_seen_at`. It runs as its own task so a cancelled handler still completes it. |
| REST | `customer_presence: {state, last_seen_at}` added to `StaffSessionResponse`, `TicketResponse`, `EscalationSummary` only. `read_presence_many` = one Redis pipeline for the whole list (+ one `IN` query for offline sessions' DB last-seen). Redis down -> `null` (unknown), never a 500 and never a wrong "offline". |
| Last seen | Offline `last_seen_at` = max(DB column, Redis `last` key), so a crashed worker still yields a recent value. Only set when offline. |
| Abuse guard | Per socket: changes less than 1 s apart and same-state refreshes less than 5 s apart are dropped. |

Rejected: Postgres-only presence (write per heartbeat); a stored online flag (stale after a crash); `HEXPIRE` per-field TTL (needs Redis 7.4); an unsolicited presence frame on agent-socket connect (breaks existing clients' first-frame assumptions and an existing test; the spec says FEs re-read over REST).

## What's involved
| Area | Files |
|---|---|
| Migration | `alembic/versions/cp1customerpresence.py` (revision `cp1customerpresence`, down `n4agentrating`): nullable `conversation_sessions.customer_last_seen_at timestamptz` |
| Model | `app/db/models/conversation.py` (`customer_last_seen_at`) |
| Service | `app/services/customer_presence.py` (tracker, connection handle, `read_presence_many`, frame builder) |
| Schema | `app/schemas/presence.py`; field added in `app/schemas/conversation.py`, `ticket.py`, `agent_app.py` |
| Sockets | `app/api/v1/endpoints/websocket.py`: `/chat/{id}` and `/customer/{id}` start presence before the receive loop, handle the `presence` frame, close in `finally` |
| REST | `app/api/v1/endpoints/chat.py` (session list/detail/takeover), `tickets.py`, `agent_app.py`; `ConversationService.to_staff_session(session, presence)` |
| Lifecycle | `app/main.py` shutdown closes the tracker |
| Tests | `tests/services/test_customer_presence.py`, `tests/presence/` (REST + WS); one existing test adjusted (`tests/rating/test_staff_credential_hygiene.py`, counts only `customer_message` fanouts) |

Config: none. Constants live in `customer_presence.py` (`CONN_TTL_S=45`, `GRACE_S=30`, `KEEPALIVE_S=15`, `HEARTBEAT_S=20` documented for FEs).

## Verification
- Local CI reproduction (pinned `ruff 0.8.6`, `black 24.10.0` via `poetry run`; full `pytest tests/` on throwaway Postgres 15 and Redis 7): see PR for final counts. Baseline before the change: 1658 passed, 33 skipped.
- Migration `upgrade head` -> `downgrade -1` -> `upgrade head` on Postgres 15, column is `timestamptz`, nullable.
- Live run (uvicorn + Postgres + Redis): connect, away, back, heartbeat (0 frames), close -> offline after ~30.5 s, reload within grace (0 frames), tenant isolation, SIGKILL then TTL lapse. Captures in `docs/captures/customer-presence/`.
- Not verified live: cross-worker aggregation (one uvicorn worker; covered by a two-tracker pytest sharing only Redis), authed customer socket, takeover response field.
- Local-only gotcha: `Base.metadata.create_all` does not add columns to an existing test DB; a Postgres test DB created before this change needs to be recreated (CI's fresh service container is unaffected).

## Operate / roll back
- Deploy: `alembic upgrade head` (additive nullable column, no rewrite). No new env vars.
- Roll back code: revert the PR; the column and the `presence:*` Redis keys are inert (keys expire: conn ZSET 5 min, last/pub 1 day). `alembic downgrade -1` drops the column only after the code that reads it is gone.
- Redis outage: chat is unaffected; presence frames stop and REST returns `customer_presence: null`.

## Follow-ups
- A graceful worker shutdown cancels pending grace tasks, so a deploy skips the live `offline` frame and the DB write for customers who were mid-grace. Reads still resolve offline; the persisted `customer_last_seen_at` stays at its previous value (the Redis `last` key covers it for 1 day). A reaper or persisting on shutdown would close this.
- `GET /tickets/unassigned` is declared after `/{ticket_id}`, so the literal path is shadowed (pre-existing, unrelated); presence is wired on it but unreachable until the route order is fixed.
- Customer clients (`luran-widget`, `luran-chat`) must start sending `presence`; dashboard and agent app must render it (FE guide sections 2-6).
- Optional: persist presence on agent-socket connect if FEs prefer a socket snapshot to a REST re-read.
