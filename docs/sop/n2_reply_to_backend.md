# SOP — N2 reply-to / quote a message, backend (2026-10-14)

**Status:** PR to `develop` from `feat/n2-reply-to`, **stacked on #134** (`fix/customer-credential-hardening`): merge after it. Migration `n2replyto` (down_revision `n4agentrating`) must be re-pointed after #135 (presence) / #136 land first, see Operate.
**Spec:** `docs/specs/2026-10-14-n2-reply-to-quote-design.md` (workspace copy; N2-D1..D7) · **FE contract:** `docs/fe-integration-guide-reply-to.md` (captures in `docs/captures/n2-reply-to/`).

## What shipped
Every message send path accepts an optional `reply_to_message_id`. The reply stores a snapshot of the quoted message, and every serialized message (REST history, WS frames, agent-app transcript) carries `reply_to` (`null` when not a reply).

## Why
Field feedback N2 (triage 2026-10-10): agents and customers need to answer a specific earlier message in a long thread, on all four chat surfaces.

## How
| Decision | Detail |
|---|---|
| Storage | `messages.reply_to_message_id` (self FK, `ON DELETE SET NULL`, indexed) + `messages.reply_to_snapshot` (`EncryptedJSON`: `{sender_type, kind, snippet}`). Encrypted because the snippet is message content. Snapshot kept so the quote survives retention and out-of-window originals (N2-D6). |
| Wire shape | `reply_to: {message_id, sender_type: customer\|agent\|ai, kind: text\|image\|video, snippet<=200}`. `message_id` is `null` once the original is deleted (FE shows "Original message unavailable"). One nesting everywhere, via `MessageResponse`. No sender name inside the quote, so the same payload is safe for customer and staff surfaces. |
| Validation | `app/services/reply_to.py::resolve_reply_to`: session must not be `resolved` (409 `REPLY_SESSION_CLOSED`, checked first, and a reply never reopens a resolved chat); target must exist in the same session (422 `REPLY_TARGET_NOT_FOUND`, identical for other-session ids so ids cannot be probed); not a system notice or voice line (422 `REPLY_TARGET_NOT_QUOTABLE`). REST errors: `detail: {code, message}` (repo convention). WS errors: `{type:"error", code, message}` to the sender only; nothing saved. |
| Customer REST | `_send_message_impl` validates up front, then `ConversationService.queue_reply_to(...)`; `add_message` attaches it to the first USER message. This avoids threading it through the ~30 branch-specific `add_message(role=USER)` calls. REST reply requires `session_id`. |
| AI (N2-D5) | `quote_context()` prefixes the text passed to `RAGService.assemble_prompt` (the only prompt assembly); images are not re-sent to vision. |
| Greeting text | A reply whose text is a bare greeting (`hi`, `hello`, `start`) skips the default-welcome branch, so the USER message is stored with its quote instead of being dropped. |
| Voice lines | Not marked before. `voice_channel` ContextVar set around the voice pipeline's `send_message`; `add_message` stamps `meta.channel = "voice"` on USER/ASSISTANT rows of that turn; quoting those is refused. Pre-release voice lines stay unmarked (limit). |
| Rejected | Separate snapshot columns (3 more columns, no benefit); a `reply_to` relationship (not needed, and eager loads would touch every Message query); `reply_to` on non-bubble system frames (`rating_requested`, `ticket_missed`; their frame contracts are asserted exactly by N4 tests and they are not quotable). |

**AI scope limit (acceptable under N2-D5):** the quote reaches the model for that turn only (`quote_context` feeds `assemble_prompt`); it is not in later turns' history, which are rebuilt from message content.

## What's involved
* Model/migration: `app/db/models/conversation.py` (`Message.reply_to*`, `reply_to` property), `alembic/versions/n2_reply_to.py`.
* Service: `app/services/reply_to.py` (new), `app/services/conversation_service.py` (`add_message(reply_to=)`, `queue_reply_to`, `voice_channel`).
* Schemas: `app/schemas/reply_to.py` (`ReplyToOut`, own module to avoid a visitor/conversation import cycle), `app/schemas/visitor.py` + `app/services/visitor_service.py` (widget bootstrap transcript carries `reply_to`), `app/schemas/conversation.py` ( `MessageResponse.reply_to`, `MessageRequest.reply_to_message_id`), `app/schemas/agent_app.py` (`TranscriptMessage.id`, `.reply_to`).
* Endpoints: `app/api/v1/endpoints/chat.py` (customer send, `agent-message`, image fanout frame), `customer_conversations.py` (forwards the field), `websocket.py` (3 sockets + replay frames; edits localized to the message branches and one helper `_resolve_frame_reply`), `agent_app.py` (transcript), `voice.py` (marker).
* Tests: `tests/services/test_reply_to_service.py`, `tests/api/v1/test_reply_to_api.py`, `tests/api/v1/test_reply_to_websocket.py`, `tests/migrations/test_n2_reply_to.py`, `tests/agent_caller/test_agent_escalations_api.py::test_detail_transcript_carries_id_and_reply_to`.

## Verification
* `ruff 0.8.6` + `black 24.10.0` (poetry-pinned) clean on `app/`; full suite on throwaway Postgres 15 + Redis 7 green (baseline before the change: 1690 passed, 33 skipped).
* Alembic `upgrade head` from empty, `downgrade -1`, `upgrade head` round-trips; FK delete rule confirmed `SET NULL`.
* App run live (uvicorn); 35 captures of every send path, error, replay and history payload in `docs/captures/n2-reply-to/`.

## Operate / roll back
* Additive and nullable: old clients that ignore `reply_to` keep working; old clients that never send the field are unchanged.
* **Migration chain:** `down_revision = "n4agentrating"`. After the presence PRs (#135/#136) merge, `alembic heads` will show two heads; re-point `n2replyto` onto the new head (one-line edit before merging this branch).
* Roll back: `alembic downgrade -1` drops both columns and the index (reply quotes are lost, messages remain). Revert the code first or the ORM will select missing columns.

## Follow-ups
* AI-answered reply and attachment+reply combinations are covered by tests but were not captured live (no LLM/Cloudinary in the capture environment).
* WS error frames carry no correlation id; add one if the FE needs per-send attribution.
* Optional: backfill `meta.channel = "voice"` for historic voice lines if a reliable discriminator exists.
