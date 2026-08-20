# SOP — Voice chat: escalate-to-human client-tool handler (Task 10)

**Status:** Shipped on branch `feat/voice-chat-backend` (not yet merged) · commit `99f8322` ·
2026-08-17
**Area:** `app/api/v1/endpoints/voice.py`, `app/services/ticket_service.py`

This is a new document, not an extension of `voice_chat_custom_llm_endpoint.md` (Task 8). Task 8
covers the mid-call custom-LLM turn endpoint (SSE, never a non-200). This task covers a different
endpoint on the same router — a client-tool callback ElevenLabs invokes once, out of band from the
conversational turn loop, that returns plain JSON rather than SSE. Cross-link added at the bottom
of both documents.

---

## What shipped

`POST /api/v1/voice/tools/escalate_to_human` (`app/api/v1/endpoints/voice.py:430-493`) — the
server half of the voice → human handoff. When the caller asks for a person, the ElevenLabs agent
invokes the `escalate_to_human` client tool (tool name is case-sensitive and must match the EL
Agent config exactly); the widget's half (already shipped, Task 8's frontend counterpart) ends the
EL session and drops the UI back to text on the same conversation. This endpoint is what makes
that handoff mean something on the backend: it marks the `ConversationSession` `ESCALATED` and
creates (or reuses) a `Ticket` so a human can pick the conversation up.

Also added: `TicketService.get_open_ticket_for_session` (`app/services/ticket_service.py:191-222`)
— did not exist before this task; required for idempotency (see below).

## Why

A caller saying "get me a person" mid-call is a money/trust-sensitive path — if the tool fires
more than once (EL retries, a flaky network round-trip, the caller repeating themselves), it must
not open a second ticket for a human to duplicate-work. The brief's pre-flight review had already
identified that the obvious existing method for this, `TicketService.get_ticket_by_session`, is
unsuitable on two independent counts (see "The `get_ticket_by_session` bug" below), so a new method
was required rather than reusing it.

## How it works

### Idempotency: reuse the session's open ticket, not the newest ticket regardless of status

```python
existing = ticket_service.get_open_ticket_for_session(context.luran_session_id)
if existing is not None:
    return {"switch_to_text": True, "ticket_id": str(existing.id)}

ConversationService(db).escalate_session(context.luran_session_id, reason)
ticket = ticket_service.create_ticket(
    TicketCreate(tenant_id=context.tenant_id, session_id=context.luran_session_id, summary=reason)
)
```

`get_open_ticket_for_session` (new) filters to `TicketStatus.OPEN` and `TicketStatus.IN_PROGRESS`
— confirmed from the `TicketStatus` enum itself (`app/db/models/conversation.py:35-41`: `OPEN`,
`IN_PROGRESS`, `RESOLVED`, `CLOSED`) — orders by `created_at` descending, and takes `.limit(1)`
before `scalar_one_or_none()`. A session whose only ticket was already `RESOLVED`/`CLOSED` has no
*open* ticket and is free to escalate again; a session with an open ticket reuses it and skips
re-calling `escalate_session` (the session is already `ESCALATED` from the first call).

### The `get_ticket_by_session` bug (not fixed here — logged as a follow-up)

`TicketService.get_ticket_by_session` (`ticket_service.py:170-189`) looks like the obvious tool for
this and was deliberately **not** reused, for two independent reasons:

1. **No status filter.** It returns the newest ticket for a session regardless of status, including
   `CLOSED`/`RESOLVED` ones. Reusing it for idempotency would mean a caller could never escalate
   again after their first ticket was closed — the endpoint would keep "succeeding" while silently
   handing back a dead ticket ID no human is looking at.
2. **A real, load-bearing bug:** the docstring promises "Most recent ticket for session," and the
   query does `order_by(desc(Ticket.created_at))` — but calls `scalar_one_or_none()` with no
   `.limit(1)`. `scalar_one_or_none()` raises `MultipleResultsFound` the moment more than one row
   matches, so the *ordering itself is dead code* — the method cannot return "the most recent" of
   more than one ticket; it throws instead. Any session that accumulates a second ticket (e.g. one
   resolved, one reopened) breaks every caller of this method, not just this one.

`get_open_ticket_for_session` was written from scratch with its own `.limit(1)` and its own status
filter rather than patching the sibling in place — the brief was explicit that "fixing" it is out
of this task's scope because other, unknown callers may depend on its current (buggy) shape.
**This is a real, reproducible defect independent of this task and should be tracked as a
follow-up**, not treated as resolved by this SOP.

### Never leave the caller in silence — but this route isn't SSE

Task 8's "always 200, never silence" rule was written for a route ElevenLabs renders directly as
audio (SSE). This route is a client-tool JSON callback, so the applicable version of the rule is
narrower: **auth stays a real 401** (unresolvable/missing voice token — ElevenLabs itself is the
caller here, not the end user, mirroring Task 8's one exception), but every other failure degrades
to a sane 200 rather than an unhandled 500:

- **Token-resolution infra failure** (e.g. a Redis outage during `resolve()`) — caught separately
  from the "token resolved to nothing" branch, mirroring `voice_custom_llm`'s own resolve-step
  try/except above it in the same file. Returns `{"switch_to_text": True, "ticket_id": None}`.
- **Business-logic failure inside the escalate/create-ticket path** (e.g. the tenant's
  `TenantCapabilities.can_create_tickets` is `False` — `TicketService.create_ticket` raises
  `ValueError` in that case — or the session vanishes mid-call, or a DB hiccup) — caught by a
  broad `except Exception`, logged, and degrades to the same
  `{"switch_to_text": True, "ticket_id": None}` shape. No ticket ID in that case: there is
  genuinely no ticket for a human to pick up, but the caller still gets told to drop to text rather
  than hitting a 500 EL cannot do anything sane with.

### `TicketService` stays synchronous

Every `TicketService`/`ConversationService` method used here is `def`, not `async def`, operating
on the sync `Session` from `Depends(get_db)` — not awaited, matching every other DB call already in
this file (`voice.py`'s own `mint_voice_session_token` does the same).

### Not yet built: ringing a human

```python
# TODO(agent-caller): ring the on-call agent app here once it exists
```

The Agent Caller app (`project_agent_caller_app` in memory — design approved, not built) is meant
to actively ring an on-call human. Today this endpoint only opens a ticket for asynchronous pickup
— there is no live paging. The TODO references that project rather than inventing an interface for
a system that doesn't exist yet.

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/api/v1/endpoints/voice.py` | new route `POST /tools/escalate_to_human`, 3 new imports (`TicketCreate`, `ConversationService`, `TicketService`) |
| `app/services/ticket_service.py` | new `get_open_ticket_for_session` method |
| `tests/voice/test_escalate.py` | new — 3 tests (happy path + session status, 401 on bad token, idempotency) |
| `tests/services/test_ticket_service.py` | new — 4 tests for `get_open_ticket_for_session` (no tickets, only-closed/resolved, ignores-closed, newest-of-multiple without `MultipleResultsFound`) |

API is additive — new route on the existing `voice` router, no existing contract touched. No
migration involved.

## Verification

Run with `REDIS_URL=redis://localhost:6379/0` — `.env`'s Docker-internal hostname doesn't resolve
from the host and produces ~17 unrelated spurious failures without the override (same caveat as
every prior voice task's SOP).

- `tests/voice/` + `tests/services/test_ticket_service.py` — **69 passed** (62 pre-existing voice
  suite + 3 new `test_escalate.py` + 4 new `test_ticket_service.py`).
- Full suite — **992 passed, 1 failed, 33 skipped** (985 pre-existing + 7 new). The 1 failure,
  `tests/auditor/test_start_audit_redirect.py::test_different_host_requires_confirmation`, is the
  same pre-existing dev-DB row-pollution failure documented in every prior voice-task SOP —
  unrelated to this work.
- `black`/`ruff` on the 4 files touched — clean.
- **Break-it-then-revert on the idempotency test specifically:** temporarily deleted the
  `get_open_ticket_for_session` reuse branch in the endpoint (replaced with a comment, no other
  change) and re-ran `test_escalation_is_idempotent_for_one_session` alone — it failed with two
  distinct ticket IDs (`AssertionError: assert '195d105b-...' == 'ed139062-...'`), proving the test
  actually exercises the reuse path rather than passing vacuously. Reverted via the pre-edit
  backup and re-ran the full `tests/voice/` + `tests/services/test_ticket_service.py` suite to
  confirm the revert was clean (69 passed).

## Operate / roll back

- No new env var and no feature flag — this route is gated by the same
  `VOICE_SHARED_SECRET`/`verify_elevenlabs_request` dependency as `voice_custom_llm`; see that
  endpoint's SOP for how to disable voice-facing routes wholesale.
- **Roll back the code:** revert commit `99f8322` on `feat/voice-chat-backend`. Nothing else in the
  codebase calls `get_open_ticket_for_session` or references `/tools/escalate_to_human` yet.
- Not yet deployed anywhere — this branch hasn't merged to `develop`.

## Follow-ups

- **`get_ticket_by_session`'s `MultipleResultsFound` bug** (`ticket_service.py:170-189`, detailed
  above) is real and independent of this task — any session that accumulates more than one ticket
  breaks every existing caller of that method today. Worth its own fix + regression test; out of
  scope here per the brief's explicit instruction not to touch it in this task.
- **No live paging.** The `TODO(agent-caller)` marker is the only trace of this — see
  `project_agent_caller_app` in memory for the (approved, unbuilt) design.
- **Task 11 (usage metering + session lifecycle)** still owns releasing the voice concurrency slot
  at real call end; an escalated call handled by this endpoint doesn't change that story — the
  WebRTC session still needs Task 11's cleanup once the caller/widget actually disconnects.

Design spec: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`
Plan: `docs/superpowers/plans/2026-08-16-voice-chat-backend.md`
Task 8 SOP: `docs/sop/voice_chat_custom_llm_endpoint.md`
