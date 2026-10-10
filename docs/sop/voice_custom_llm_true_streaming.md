# Voice custom-LLM: true SSE streaming + duplicate-turn dedupe (2026-10-10)

Updates `voice_chat_custom_llm_endpoint.md` (which described a "materialise then emit" design).

## What shipped
`POST /api/v1/voice/llm/chat/completions` now streams: the role frame is flushed immediately, the
pipeline runs while the response is open, then reply frames follow. Branch `fix/voice-streaming`.

## Why
Staging 2026-10-10: after the token fix the endpoint returned 200 in 2-3.6 s with real replies, yet
ElevenLabs (EL) spoke nothing after its greeting. The endpoint awaited the whole pipeline before
returning, so the first byte (and the "... " buffer word that exists to cover latency) arrived after
2-3.5 s. EL treated that as a stall, abandoned the request and re-issued it ~1 s later; requests
overlapped, and every abandoned one still ran the pipeline and persisted a user message + AI reply.

## How
| Concern | Decision |
|---|---|
| First bytes | `_stream_pipeline_turn` async generator yields the `{"role":"assistant"}` frame immediately. |
| Buffer word | Sent only if the pipeline has not answered within `_BUFFER_GRACE_SECONDS` (0.35 s); otherwise sent just before the reply words, so fast turns keep the exact old wire shape. |
| Tool call (escalate) | The escalate decision comes from the pipeline (`should_escalate`), so it cannot be known up front. EL docs do not say whether content followed by `tool_calls` in one stream is supported (not addressed; checked 2026-10-10). Chosen: never speak before the decision for fast turns; for slow turns the only content ahead of the tool call is the near-silent "... " buffer word (valid OpenAI stream). Tool-call wire shape (`tool_calls` delta, `finish_reason: "tool_calls"`) is unchanged. If live testing shows EL mishandles content+tool_calls, raise the grace or make it 0 to drop the buffer word on slow tool turns. |
| Headers | `Cache-Control: no-cache`, `X-Accel-Buffering: no` on every response from this endpoint (nginx otherwise buffers SSE). |
| Disconnect | `request.is_disconnected()` is checked before the pipeline starts; if EL already left, the pipeline is not run. Once started, the pipeline runs as its own task and is NOT cancelled on disconnect (no half-persisted turn); its outcome is cached. |
| DB session | FastAPI 0.115.14 closes yield-dependencies (the request `db`) BEFORE a StreamingResponse body runs (verified with a probe). Reusing it would check out an unclosed pooled connection per turn. The pipeline task therefore opens its own `SessionLocal()` (`_open_stream_session`) and closes it in `finally`, including on disconnect/exception; the endpoint no longer takes `get_db` at all. |
| Dedupe | `app/services/voice/turn_dedupe.py`: Redis key = sha256(session id, `len(messages)`, final user text), 60 s TTL. First request claims (SET NX), stores the outcome; a re-issued identical request replays it (waits up to 10 s if still pending) instead of re-running `send_message`. Pipeline errors release the claim so a retry re-runs. All Redis errors fail open (= old behaviour). |

Rejected: cancelling the pipeline on disconnect (risks a persisted user message with no AI reply);
dedupe on user text alone (would swallow a caller legitimately repeating themselves - history length
disambiguates).

## Files
`app/api/v1/endpoints/voice.py` (generator, `_run_turn`, `_run_pipeline`, headers),
`app/services/voice/sse.py` (`SSEFramer`), `app/services/voice/turn_dedupe.py` (new).
No migrations, no config.

## Verification
`tests/voice/test_custom_llm_streaming.py` (first frame before the pipeline is released, all reply
branches, tool-call shape fast and slow, headers, disconnect, dedupe incl. abandoned-then-retried),
`tests/voice/test_turn_dedupe.py` (real Redis). Full suite green locally against Postgres 15 +
Redis 7. Live check still needed on staging.

## Known limits / follow-ups
- EL's retry may carry a MERGED user turn ("Hello ... Hello?"), whose text differs, so dedupe will
  not match it; the early first byte is what is expected to stop the retries in the first place.
- Roll back: revert the PR; no data changes. Redis keys `voice:turn:*` expire on their own.

# Companion fix: terminal ticket status wins in 409 reason
`/chat/sessions/{id}/takeover` and `/agent/escalations/{id}/acknowledge` now answer
`reason: "resolved"` whenever the ticket is RESOLVED/CLOSED, even when it is assigned to the caller
(previously `"claimed"` with the caller's own name; acknowledge also treated it as an idempotent
win). Tests in `tests/agent_caller/test_missed_ticket_lifecycle.py`.

> Updated 2026-10-10: tool-call turns that are known in advance now suppress the buffer word; see `voice_escalation_ux.md`.
