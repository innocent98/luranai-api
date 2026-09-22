# SOP — Agent-joined notice: web/mobile takeover parity

**Status:** Shipped (branch, pre-PR) · `fix/agent-joined-notice-mobile-parity` · PR → `develop` · 2026-09-22
**Area:** `app/services/conversation_service.py`, `app/api/v1/endpoints/chat.py`, `app/api/v1/endpoints/agent_app.py`, `tests/agent_caller/`

---

## What shipped

The customer now sees the **"X joined the conversation"** notice whether the human agent takes
over from the **web dashboard** or the **mobile Agent Caller app**. Previously only the dashboard
path emitted it; a mobile takeover left the customer's widget silent.

No schema change, no migration.

## Why

The join notice is two customer-facing side effects — a persisted **SYSTEM message** (rendered in
the transcript) and an **`agent_joined` WebSocket fanout** (which the widget renders live, see
`luran-chat/app/hooks/useWebSocket.ts:125-131`). Both lived *inline inside the dashboard endpoint*
`agent_takeover` (`app/api/v1/endpoints/chat.py`). The mobile app takes over through an entirely
different endpoint — `POST /agent/escalations/{id}/acknowledge` → `acknowledge_escalation` →
`_finish_claim` (`app/api/v1/endpoints/agent_app.py`) — which set `session.agent_id` /
`session.agent_joined_at` but never posted the message or fanned out the event. So the widget
received nothing on a mobile takeover.

## How

Extracted the two operations into one shared helper and called it from **both** paths:

- **`emit_agent_joined(db, session, agent)`** — new module-level `async` function in
  `app/services/conversation_service.py` (co-located with `ConversationService`, which it uses to
  persist the message). Reproduces the dashboard's **exact** message content
  (`f"{agent.full_name} joined the conversation"`, `role=SYSTEM`,
  `meta={"event":"agent_joined","agent_id":...}`) and the **exact** fanout event shape
  (`type="agent_joined"`, `agent_name`, `agent_id`, `timestamp`), so the widget renders identically.
  `session` is duck-typed — accepts the ORM `ConversationSession` (mobile) or a `SessionResponse`
  (dashboard); both expose `.id` and `.agent_joined_at`.
- **`agent_takeover`** (dashboard) now calls the helper in place of the inline block — externally
  unchanged (same message + event as before).
- **`_finish_claim`** (mobile) calls the helper **inside** the existing
  `if session is not None and session.agent_id != current_agent.id:` guard, after the session is
  committed. The guard is what makes it **idempotent**: `acknowledge_escalation` deliberately
  re-runs `_finish_claim` on a retry (to repair a lost response), and on that retry
  `session.agent_id` already equals the agent, so the helper is skipped — no double-posted notice,
  no re-ring.

**Alternatives rejected:** a `ConversationService` *method* — rejected because the fanout is async
while the service is otherwise sync/instance-bound; a free async function keeps the async boundary
explicit and reusable. Circular import was checked: `app.core.websocket` does not import
`conversation_service`, so the top-level `connection_manager` import is safe.

## What's involved

| File | Change |
|---|---|
| `app/services/conversation_service.py` | New `emit_agent_joined()` helper + `connection_manager` import |
| `app/api/v1/endpoints/chat.py` | `agent_takeover` calls helper (inline block removed); import updated |
| `app/api/v1/endpoints/agent_app.py` | `_finish_claim` calls helper inside the idempotency guard; import added |
| `tests/agent_caller/test_agent_joined_notice.py` | New — 3 tests (see below) |

## Verification

- **New tests** (`tests/agent_caller/test_agent_joined_notice.py`):
  - `test_acknowledge_posts_join_notice_and_fans_out_to_customer` — a winning acknowledge persists
    exactly one SYSTEM `agent_joined` message (exact content + meta) and fans out to the customer
    only (`to_customer=True`, `to_agents=False`, exact event shape).
  - `test_reacknowledge_does_not_double_post_the_join_notice` — a second acknowledge/retry by the
    same agent creates no second message and no second fanout (idempotent).
  - `test_losing_the_claim_posts_no_join_notice` — a losing (409) claimant emits nothing.
  - Fanout asserted by patching `connection_manager.fanout_event` with an `AsyncMock`.
- Existing `tests/agent_caller/test_agent_acknowledge_api.py` still green (behaviour unchanged).
- **CI reproduced locally** (pinned toolchain via poetry — ruff 0.8.6, black 24.10.0):
  - `poetry run ruff check app/` → clean
  - `poetry run black --check app/` → clean
  - `poetry run pytest tests/` (against local Postgres 15-equiv + Redis 7) → **1181 passed, 33 skipped**
  - automation-flow regression job (SQLite env) → 14 passed

## Operate / roll back

Pure application change, no migration and no config. Roll back by reverting the branch's commit —
the dashboard path returns to its prior inline emission; mobile returns to silent (the pre-fix bug).

## Follow-ups

- None functional. Local full-suite run used Python 3.14 venv vs CI's 3.11; all touched paths are
  version-neutral. No FE change required — the widget already renders on the `agent_joined` event;
  this fix simply makes the mobile path emit it.
