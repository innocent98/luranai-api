# FE integration guide: conversation counting (6-hour same-conversation window)

Every body below is copied from `docs/fe-integration-captures/conversation-counting/` (script
`scripts/capture_conversation_counting.py`): the real FastAPI app on a local Postgres + Redis, with **only the
model call stubbed**. Ids are throwaway local values; the visitor token is replaced by a placeholder.

## What it is, in the FE's terms
Plans cap "conversations per month". A conversation is one chat session. A visitor or signed-in customer who
comes back within **6 hours** of the last message continues the same conversation (same `session_id`) and is not
counted again; after more than 6 hours the first message starts a **new** conversation with a **new `session_id`**.
**Nothing about request or response shapes changed.** Three behaviours are new for the client:

1. The `session_id` in a `POST /api/v1/chat/message` response can differ from the one you sent.
2. `POST /api/v1/chat/sessions` can now return 402 (it never did).
3. A message into an old (abandoned/resolved) session can now return 402 (it never did).

## 1. A changed `session_id` (capture 04)
Request (widget still holds the old id; the old conversation was resolved 6.1 hours ago):
`POST /api/v1/chat/message` `{"message":"Another question","session_id":"<old-id>","tenant_id":"...","user_identifier":"capture-...","metadata":{"visitor_token":"<placeholder>","entry_point":"widget"}}`
Response 200: `session_id` is a **new** id (and `message.session_id` equals it). The old session is untouched.

**FE instruction:** after every response, if `response.session_id !== storedSessionId`, replace the stored id,
clear/transition the local transcript (it is a new conversation), and keep using the new id. `luran-chat`'s
`useChat.ts` currently only adopts the id from the first response: **that needs changing**. Until it does, the
server routes an old id to the visitor's current conversation (capture 05: same new `session_id` comes back, no
third conversation), so nothing is double-counted, but the transcript on screen will not reset.

Inside the window (capture 03, abandoned 5.9 h, no `session_id` sent) the **same** `session_id` returns and the
session is reopened (`active`). Same for a resolved session inside the window: it is reopened, so a customer can
reply to a just-resolved chat. A bare widget greeting (`start`, `hi`, `__init__`, ...) does not reopen a resolved
session.

## 2. The 402 (captures 06, 07, 09)
Status 402, body (identical on `/chat/message`, `/chat/sessions`, and the stale-session case):
```json
{"detail":{"error":"conversation_limit_reached","message":"Monthly conversation limit of 2 reached.","definition":"A conversation is one continuous exchange between a customer and your AI or team, from their first message until they have been quiet for 6 hours. It is not counted per message: one conversation can hold any number of messages, images and replies. If the same customer comes back within 6 hours it is still the same conversation; after 6 hours it is a new one. A conversation handed to a human agent stays one conversation until it is resolved. Conversations are counted per calendar month (UTC) and the count resets on the 1st. WhatsApp follows the same rule per chat.","current_count":2,"limit":2,"resets_at":"2026-11-01T00:00:00"}}
```
`definition` (added 2026-10-29, additive) is the owner's plain-words meaning of "conversation"; show it with the limit message so nobody reads the limit as a message count. It uses the 6-hour setting (`CONVERSATION_REUSE_WINDOW_HOURS`); the other fields are unchanged. `resets_at` is a naive UTC ISO string (no `Z`); treat it as UTC. It is the same shape the widget already handled on
`/chat/message`.

## 3. Traps
| Trap | Detail |
|---|---|
| Continuing is never blocked at the cap | At the cap, a visitor continuing inside the window gets 200 (capture 08). Only something that would create a conversation returns 402. |
| 402 for an old session | A message to a session idle more than 6 h, at the cap, is refused (capture 09) even though the client "just continued". Show the limit message; do not retry. |
| `POST /chat/sessions` | No frontend in this repo set calls it; any integration that does must now handle 402. |
| Voice | `POST /voice/session/token` returns the same 402 body when its session is past the window and the tenant is at the cap. Otherwise unchanged. |
| Escalated threads | An escalated session is continued whatever its age (a human owns it); it never changes `session_id`. |
| Dashboard usage numbers | Counts drop after this ships for tenants whose visitors returned within 6 h; do not read that as lost traffic. |

## Verification table
| Behaviour | Status |
|---|---|
| Same `session_id` inside the window, session reopened (capture 03) | verified live (local app, model stubbed) |
| New `session_id` beyond the window (capture 04) | verified live |
| Old-id client lands in the new conversation (capture 05) | verified live |
| 402 body on new visitor, `POST /chat/sessions`, stale session (captures 06, 07, 09) | verified live |
| Continuing inside the window at the cap returns 200 (capture 08) | verified live |
| Authed customer window, WhatsApp window, voice mint 402 | verified by automated tests only (`tests/conversation_counting/`, `tests/voice/test_token_mint.py`, `tests/whatsapp/`), not captured live |
| Real widget behaviour with a changed `session_id` | **not verified** (widget not changed in this PR) |
