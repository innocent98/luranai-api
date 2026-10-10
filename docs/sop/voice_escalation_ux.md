# Voice escalation UX: async notifications, silent tool-call turns, no echo (2026-10-10)

Follows `voice_custom_llm_true_streaming.md`. Branch `fix/voice-escalation-ux` (PR to `develop`; not merged until a live staging test).

## What shipped
1. Escalation emails no longer run inside the escalating turn (voice and text chat).
2. A voice turn that will tool-call never emits the "... " buffer word first.
3. A voice turn on an already-escalated session no longer echoes the caller's words; it returns the `escalate_to_human` tool call.

## Why (live evidence, staging 2026-10-10 14:40 UTC, `7575d5c`)
Caller asked for a human -> `should_escalate` AUTO_ESCALATE -> ticket -> three escalation emails sent inline (~2.6 s each, ~8 s) -> pipeline returned at +8 s. Past `_BUFFER_GRACE_SECONDS` (0.35 s) the buffer word had already gone out, so ElevenLabs (EL) never invoked the client tool and the widget never switched to text. Every later turn hit chat.py "Skipping AI generation: session is escalated" (commit cc39f21), whose `ChatResponse.message` is the persisted USER message; the voice endpoint spoke it back.

## How
| Concern | Decision |
|---|---|
| Emails | `chat.py`: six identical inline loops replaced by `_queue_escalation_emails` -> `_schedule_escalation_emails` -> `asyncio.create_task(_send_escalation_emails)`. Recipients/tenant name are resolved inline (two cheap reads) and snapshotted into plain values, so the task needs no DB session. Same recipients, same email content. Failures are logged (`logger.exception`, address masked) and never raised; scheduling itself never raises. Tasks are held in `_background_notifications` (strong refs). Pattern matches `_background_turns` in voice.py. Chosen over ARQ: the existing workers carry id-based jobs (`alert_escalation`), not ad-hoc email payloads, and the agent-ring is already ARQ (`_ring_agents`, unchanged). Tradeoff: a process restart mid-send drops that email (the phone ring is the primary channel and is on ARQ). |
| Explicit-human predicate | `RAGService.is_explicit_human_request` (classmethod). The satisfaction and human-request pattern lists were lifted out of `should_escalate` into class constants; `should_escalate` uses the same helpers, so there is one list. Tenant `escalation_keywords` are not checked (need the tenant AI config = a DB read). |
| Silent turns | `_stream_pipeline_turn`: if the predicate matches (or the tool already ran this call) the buffer word is suppressed entirely; the stream is role frame, then (when the pipeline finishes) `tool_calls`. If the pipeline does not escalate after all (e.g. contact-capture question), the reply is spoken normally. |
| Escalated session | `_run_pipeline` detects the echo (`message.role == USER`) and returns the tool call, text empty. Stateless "already invoked" signal: a `role: "tool"` message in EL's history (`_tool_already_invoked`); then `_ESCALATED_CHAT_REPLY` ("A teammate will continue with you in the chat.") is spoken before the tool call. The caller's message is still persisted by `send_message` (the human sees it). Outcome (incl. tool name) is stored in the dedupe cache as before. |

## EL content -> tool_calls finding
EL docs (custom LLM, client tools; re-checked 2026-10-10) say only that a custom LLM must return function calls in standard OpenAI format and that SSE chunks end with `[DONE]`. They do not say whether text followed by `tool_calls` in one stream is supported, and show no streaming tool-call example. Live evidence (the failure above) suggests the tool is not invoked after content. Chosen behaviour: never put content ahead of a tool call when we can know in advance (explicit request, tool already ran, escalated session echo is fast). The one case with spoken text before the tool call is the second+ turn after handoff (`_ESCALATED_CHAT_REPLY` then tool call); there the first invocation already happened, so a dropped repeat call is harmless.

Residual risk (documented, not fixed): escalations decided late by the pipeline (tenant escalation keywords, flows, contact-capture) on a turn slower than 0.35 s can still emit the buffer word before the tool call. With emails off the request path these turns are no longer multi-second, but they are not impossible. If live testing shows it, set `_BUFFER_GRACE_SECONDS` higher or move the keyword check ahead of the pipeline.

## Blocking SMTP (review fix)
`app/utils/email.py::send_email` is `async def` but called the synchronous `emails` `message.send(...)` on the event loop, so a background task alone still froze the whole worker (and the voice SSE stream) for ~2.6 s per mail. It now runs `await asyncio.to_thread(message.send, ...)`; return value, logging and error handling are unchanged. This benefits every caller of `send_email`. Tests fake `emails.Message.send` with a BLOCKING `time.sleep` (a mocked async mail would hide the problem): the heartbeat test requires >= 10 ticks of a 50 ms loop during a 1 s send.

## Files
`app/api/v1/endpoints/chat.py` (email helpers; six call sites), `app/api/v1/endpoints/voice.py`, `app/services/rag_service.py` (predicate extraction), `app/utils/email.py` (one line). No migrations, no config.

## Verification
- `tests/voice/test_escalation_ux.py`: predicate table; slow explicit-human turn has zero content frames; ordinary slow turn keeps the buffer word; explicit request the pipeline does not escalate still speaks; escalated-session echo -> tool call only, no echo text; after a tool result -> spoken line then tool call; echo outcome cached without the user's words.
- `tests/api/v1/test_escalation_notifications.py`: scheduler returns immediately and sends to every recipient in order; one failing recipient does not stop the rest; scheduling failure is swallowed; end-to-end `/chat/message` escalation with 3 recipients x 2 s mail returns in under 2 s (6.3 s on the previous code, 0.9 s now).
- Existing voice test helpers gained `role` on the mocked reply (production replies always carry it).

## Operate / roll back
Revert the PR; no data changes. To watch it live: logs `Escalation email to x***@domain failed` on failure; `Voice turn on escalated session ...; switching`.

## Follow-ups
- Widget: show the handoff notice and switch to text on the tool call (fix-list item 4, other repo).
- Tenant escalation keywords on voice are still decided inside the pipeline (see residual risk).
- Text chat: other work in the escalation path (`_ring_agents` enqueue) is still awaited inline; it is a Redis enqueue with fail-fast retries.
