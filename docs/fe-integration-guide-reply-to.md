# FE integration guide - Reply to / quote a message (N2)

Backend: branch `feat/n2-reply-to` (stacked on `fix/customer-credential-hardening`, PR #134). Spec: `docs/specs/2026-10-14-n2-reply-to-quote-design.md` (decisions N2-D1..D7, workspace copy). SOP: `docs/sop/n2_reply_to_backend.md`.

**Provenance.** Every JSON body below is pasted by script from `docs/captures/n2-reply-to/` (numbered `NN-name.json`, cited next to each block). They were captured on 2026-10-14 against the real app run **locally** (uvicorn on 127.0.0.1, Postgres 15, Redis 7, migrations at head `n2replyto`, `AGENT_ALERT_ENABLED=false`). JWTs are never written to the captures (the auth line names the role). UUIDs, the `user_identifier` strings and the image URL (`res.example.test`) are throwaway placeholders from a scratch database. Response blocks in section 3 and 6 that show only some keys of a larger object (for example `session_id` + `message` of a ChatResponse, or the last row of a list) say so in the text; the capture file has the full body. Nothing is retyped. Anything not exercised live is marked **NOT LIVE** inline and in section 10.

## 1. What the feature is (FE terms)

A participant can **quote one earlier message** when sending a new one: the customer (widget, hosted page, authed customer chat) and the agent (dashboard, Flutter app) on all four surfaces. The new message arrives with a `reply_to` object describing the quoted message, and you render it as a small quote block above the reply.

| Rule | Behaviour |
|---|---|
| Quotable | any customer, agent or AI message, including image/video messages (D2) |
| Not quotable | system notices (`role: "system"`, or `meta.system_notice` set) and voice-mode lines (`meta.channel == "voice"`) |
| Closed chats | cannot reply in a `resolved` conversation (D7); hide the Reply action there |
| Persistence | the quote is a **snapshot** frozen at send time (D6): it survives attachment expiry and originals outside the loaded window |
| AI | when the AI answers a customer reply, it is shown the quoted text as context (D5). Nothing for the FE to do. |

## 2. The contract in one table

| Thing | Value |
|---|---|
| Send field (REST body) | `reply_to_message_id` - UUID, optional, on `POST /chat/message`, `POST /customer/conversations/message`, `POST /chat/sessions/{id}/agent-message` |
| Send field (WebSocket frame) | `reply_to_message_id` on the `{"type":"message"}` frame, all three sockets (`/ws/chat/{id}`, `/ws/customer/{id}`, `/ws/agent/{id}`) |
| Attachment / video sends | same field, sent together with `attachment_ids` on the REST message routes (image/video upload endpoints themselves are unchanged) |
| Read field | `reply_to` on **every** serialized message, REST and WS, `null` when not a reply (never absent) |

```json
"reply_to": {
  "message_id": "uuid | null",
  "sender_type": "customer | agent | ai",
  "kind": "text | image | video",
  "snippet": "string, at most 200 characters, '' for image/video-only messages"
}
```

Where `reply_to` appears (all verified live in section 4-6):

* REST history: `GET /chat/sessions/{id}/messages` and `GET /chat/sessions/{id}` (staff), `GET /customer/conversations/{id}` and `.../messages` (authed customer), `GET /agent/escalations/{ticket_id}` (agent app, `transcript[]`).
* REST send responses: `POST /chat/sessions/{id}/agent-message` returns the new message (so `reply_to` is on the response root); `POST /chat/message` and `POST /customer/conversations/message` return a `ChatResponse` whose `message.reply_to` is that of **the message in `message`** (see trap T1).
* WS frames: `customer_message` and `agent_message`, both live and in the replay sent on connect.

### Field-nesting traps

* **T1 - `ChatResponse.message` is not always the customer's message.** When the AI answers, `message` is the AI reply and its `reply_to` is `null`; the customer's own message (with its quote) is only visible through history/WS. Render the quote on the customer's optimistic bubble from local state, not from this response. When a human owns the thread (escalated session) the response echoes the customer's message and `message.reply_to` is populated (capture 02). **NOT LIVE** for the AI-answered case (no LLM key in the capture environment): the shape is the existing `ChatResponse` with an assistant message, covered by `tests/api/v1/test_reply_to_api.py`.
* **T2 - `message_id` can be `null` while `reply_to` is not.** It means the original message no longer exists. `sender_type`, `kind` and `snippet` still hold (section 6). Do not treat a null `message_id` as "not a reply".
* **T3 - `snippet` is `""` for image/video messages.** Show the kind label ("Photo" / "Video"), not an empty line. A captioned image has `kind: "image"` and the caption as `snippet`.
* **T4 - the error shapes differ by transport** (section 5): REST uses `{"detail": {"code", "message"}}`, WebSockets use a `{"type":"error","code","message"}` frame, and a malformed UUID is the stock FastAPI validation list, not a coded error.
* **T5 - the quote carries no sender identity beyond `sender_type`.** There is no agent name and no customer name inside `reply_to`. Derive the label on the client (section 7.3).

## 3. REST - sending a reply

### 3.1 Customer, public widget / hosted page (`POST /api/v1/chat/message`)

Needs `session_id` (replying requires an existing conversation) and the usual customer credential (`user_identifier` or visitor token). Capture of the same send **without** a reply:

*Capture:* `docs/captures/n2-reply-to/01-customer-rest-plain-no-reply.json` - Customer send WITHOUT reply_to_message_id: response message.reply_to is null

`POST /api/v1/chat/message`  (auth: none (public customer route))

Request body:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "user_identifier": "widget-03da58fca9cf433195319ce51dd4485a",
  "message": "Here is the label, thanks"
}
```
Response **200**:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "message": {
    "role": "user",
    "content": "Here is the label, thanks",
    "id": "5396498b-f7b7-4675-a967-384e4011321e",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:25.836347Z"
  }
}
```

With `reply_to_message_id` (the session is human-owned, so no AI turn and the response echoes the stored customer message):

*Capture:* `docs/captures/n2-reply-to/02-customer-rest-reply-to-agent-text.json` - Customer replies to an agent text message. The ChatResponse echoes the persisted USER message (escalated session, no AI turn).

`POST /api/v1/chat/message`  (auth: none (public customer route))

Request body:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "user_identifier": "widget-03da58fca9cf433195319ce51dd4485a",
  "message": "Yes I can send it",
  "reply_to_message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950"
}
```
Response **200**:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "message": {
    "role": "user",
    "content": "Yes I can send it",
    "id": "e44d921f-8ced-4d9b-a52b-896f1d240ddb",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:25.935187Z"
  },
  "user_message_attachments": [],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

Quoting an image message (`kind: "image"`, blank snippet):

*Capture:* `docs/captures/n2-reply-to/03-customer-rest-reply-to-image.json` - Quoting an image message: kind=image, snippet is empty

`POST /api/v1/chat/message`  (auth: none (public customer route))

Request body:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "user_identifier": "widget-03da58fca9cf433195319ce51dd4485a",
  "message": "This photo again?",
  "reply_to_message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af"
}
```
Response **200**:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "message": {
    "role": "user",
    "content": "This photo again?",
    "reply_to": {
      "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
      "sender_type": "customer",
      "kind": "image",
      "snippet": ""
    }
  }
}
```

### 3.2 Authed customer (`POST /api/v1/customer/conversations/message`)

*Capture:* `docs/captures/n2-reply-to/15-authed-customer-rest-reply.json`

`POST /api/v1/customer/conversations/message`  (auth: customer JWT)

Request body:

```json
{
  "session_id": "0a258ea1-13f4-403a-99c0-931453fabf9f",
  "message": "I need help with my invoice",
  "reply_to_message_id": "ce1367d7-257b-4489-8063-f176aa88bb41"
}
```
Response **200**:

```json
{
  "session_id": "0a258ea1-13f4-403a-99c0-931453fabf9f",
  "message": {
    "role": "user",
    "content": "I need help with my invoice",
    "reply_to": {
      "message_id": "ce1367d7-257b-4489-8063-f176aa88bb41",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Welcome back Ada, how can I help?"
    }
  }
}
```

### 3.3 Agent REST (`POST /api/v1/chat/sessions/{id}/agent-message`)

Without a reply the response carries `"reply_to": null`:

*Capture:* `docs/captures/n2-reply-to/11-agent-rest-plain-no-reply.json`

`POST /api/v1/chat/sessions/66645e12-7da4-481a-a7c8-e19b88dd1308/agent-message`  (auth: agent JWT)

Request body:

```json
{
  "message": "Thanks, checking now"
}
```
Response **201**:

```json
{
  "role": "human_agent",
  "content": "Thanks, checking now",
  "id": "e9033751-6f7e-43ac-b026-45271a1c8467",
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "retrieved_chunk_ids": null,
  "token_count": null,
  "attachments": [],
  "reply_to": null,
  "meta": {
    "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
    "agent_name": "Sam Agent"
  },
  "created_at": "2026-10-14T01:30:26.301532Z"
}
```

With a reply:

*Capture:* `docs/captures/n2-reply-to/12-agent-rest-reply.json`

`POST /api/v1/chat/sessions/66645e12-7da4-481a-a7c8-e19b88dd1308/agent-message`  (auth: agent JWT)

Request body:

```json
{
  "message": "Got your first message about order 1042",
  "reply_to_message_id": "ce7f96a9-3906-4850-b24d-b5db037e0aba"
}
```
Response **201**:

```json
{
  "role": "human_agent",
  "content": "Got your first message about order 1042",
  "id": "00c166b8-d34a-4177-9acd-761026d27613",
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "retrieved_chunk_ids": null,
  "token_count": null,
  "attachments": [],
  "reply_to": {
    "message_id": "ce7f96a9-3906-4850-b24d-b5db037e0aba",
    "sender_type": "customer",
    "kind": "text",
    "snippet": "My order 1042 has not arrived"
  },
  "meta": {
    "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
    "agent_name": "Sam Agent"
  },
  "created_at": "2026-10-14T01:30:26.367160Z"
}
```

### 3.4 Attachments and video

`reply_to_message_id` and `attachment_ids` can be combined in one request on all three routes above (a reply that also carries a photo or a screen recording). **NOT LIVE:** attaching needs Cloudinary, which the capture environment does not have. Covered by `tests/api/v1/test_reply_to_api.py::test_reply_with_image_attachment_links_both`, `::test_image_fanout_frame_to_agents_carries_reply_to` and `::test_agent_reply_to_image_attachment_message`. The video route (`/chat/video/sign`, `/chat/video/register`) is unchanged: video is sent through the same message routes with `attachment_ids`.

## 4. WebSockets

Frames for a send are the same on the three sockets. Add `reply_to_message_id` to the existing message frame:

```json
{"type": "message", "content": "Sending it now", "reply_to_message_id": "<uuid>"}
```

The **receiving** side gets the usual frame with `reply_to` added (null for non-replies). Customer to agent:

*Capture:* `docs/captures/n2-reply-to/18-ws-customer-sends-reply.json`

`customer_socket_sent`:

```json
{
  "type": "message",
  "content": "Sending it now",
  "reply_to_message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950"
}
```
`agent_socket_received`:

```json
[
  {
    "type": "customer_message",
    "message_id": "94a4ece6-bcb0-4bbc-bfb3-0263c1aa68a8",
    "content": "Sending it now",
    "customer_label": "Visitor 66645e12",
    "timestamp": "2026-10-14T01:30:27.447484+00:00",
    "attachments": [],
    "reply_to": {
      "message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
    }
  }
]
```

A non-reply carries `"reply_to": null`:

*Capture:* `docs/captures/n2-reply-to/19-ws-customer-sends-plain.json`

`customer_socket_sent`:

```json
{
  "type": "message",
  "content": "plain message"
}
```
`agent_socket_received`:

```json
[
  {
    "type": "customer_message",
    "message_id": "861136de-0385-40c7-9906-942c0ddd405f",
    "content": "plain message",
    "customer_label": "Visitor 66645e12",
    "timestamp": "2026-10-14T01:30:28.708363+00:00",
    "attachments": [],
    "reply_to": null
  }
]
```

Agent to customer (the sender's own socket gets no echo; render your own bubble from local state or from the REST/ack you already use):

*Capture:* `docs/captures/n2-reply-to/20-ws-agent-sends-reply.json`

`agent_socket_sent`:

```json
{
  "type": "message",
  "content": "Understood, one moment",
  "reply_to_message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af"
}
```
`customer_socket_received`:

```json
[
  {
    "type": "agent_message",
    "message_id": "3e1831d0-d0c9-4a2c-be10-57bbf7074fd4",
    "content": "Understood, one moment",
    "agent_name": "Sam Agent",
    "timestamp": "2026-10-14T01:30:29.160426+00:00",
    "attachments": [],
    "reply_to": {
      "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
      "sender_type": "customer",
      "kind": "image",
      "snippet": ""
    }
  }
]
```

Authed customer socket (`/ws/customer/{id}`), agent side receives:

*Capture:* `docs/captures/n2-reply-to/28-ws-authed-customer-sends-reply.json`

`authed_customer_socket_sent`:

```json
{
  "type": "message",
  "content": "Following up on that",
  "reply_to_message_id": "ce1367d7-257b-4489-8063-f176aa88bb41"
}
```
`agent_socket_received`:

```json
[
  {
    "type": "customer_message",
    "message_id": "d0bf7ea4-32b6-46e4-ad6c-e5d598acfd4c",
    "content": "Following up on that",
    "customer_label": "Visitor 0a258ea1",
    "timestamp": "2026-10-14T01:30:38.430693+00:00",
    "attachments": [],
    "reply_to": {
      "message_id": "ce1367d7-257b-4489-8063-f176aa88bb41",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Welcome back Ada, how can I help?"
    }
  }
]
```

**Replay on connect.** Every replayed `customer_message` / `agent_message` frame now includes `reply_to`. Reconnecting agent socket after replies were sent (last 6 frames shown, trimmed to the tail of the replay):

*Capture:* `docs/captures/n2-reply-to/26-ws-agent-replay-after-replies.json` - Reconnect: replay now includes the replies sent above (reply_to populated) next to non-replies (null)

`agent_socket`:

```json
{
  "url": "ws://127.0.0.1:8812/api/v1/ws/agent/66645e12-7da4-481a-a7c8-e19b88dd1308?token=<agent JWT>",
  "received": [
    {
      "type": "customer_message",
      "message_id": "7ccf518a-8450-4d55-90a9-3a850572dc6e",
      "content": "This photo again?",
      "customer_label": "Visitor 66645e12",
      "timestamp": "2026-10-14T01:30:26.037169+00:00",
      "attachments": [],
      "reply_to": {
        "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
        "sender_type": "customer",
        "kind": "image",
        "snippet": ""
      }
    },
    {
      "type": "agent_message",
      "message_id": "e9033751-6f7e-43ac-b026-45271a1c8467",
      "content": "Thanks, checking now",
      "agent_name": "Sam Agent",
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "timestamp": "2026-10-14T01:30:26.301532+00:00",
      "attachments": [],
      "reply_to": null
    },
    {
      "type": "agent_message",
      "message_id": "00c166b8-d34a-4177-9acd-761026d27613",
      "content": "Got your first message about order 1042",
      "agent_name": "Sam Agent",
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "timestamp": "2026-10-14T01:30:26.367160+00:00",
      "attachments": [],
      "reply_to": {
        "message_id": "ce7f96a9-3906-4850-b24d-b5db037e0aba",
        "sender_type": "customer",
        "kind": "text",
        "snippet": "My order 1042 has not arrived"
      }
    },
    {
      "type": "customer_message",
      "message_id": "94a4ece6-bcb0-4bbc-bfb3-0263c1aa68a8",
      "content": "Sending it now",
      "customer_label": "Visitor 66645e12",
      "timestamp": "2026-10-14T01:30:27.447484+00:00",
      "attachments": [],
      "reply_to": {
        "message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
        "sender_type": "agent",
        "kind": "text",
        "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
      }
    },
    {
      "type": "customer_message",
      "message_id": "861136de-0385-40c7-9906-942c0ddd405f",
      "content": "plain message",
      "customer_label": "Visitor 66645e12",
      "timestamp": "2026-10-14T01:30:28.708363+00:00",
      "attachments": [],
      "reply_to": null
    },
    {
      "type": "agent_message",
      "message_id": "3e1831d0-d0c9-4a2c-be10-57bbf7074fd4",
      "content": "Understood, one moment",
      "agent_name": "Sam Agent",
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "timestamp": "2026-10-14T01:30:29.160426+00:00",
      "attachments": [],
      "reply_to": {
        "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
        "sender_type": "customer",
        "kind": "image",
        "snippet": ""
      }
    }
  ]
}
```

Frames that are not message bubbles (`agent_joined`, `agent_left`, `ticket_resolved`, `ticket_closed`, `ticket_missed`, `rating_requested`, typing, pong) are unchanged and have **no** `reply_to`; they are system notices and cannot be quoted.

## 5. Errors

| Code | HTTP | When | FE behaviour |
|---|---|---|---|
| `REPLY_TARGET_NOT_FOUND` | 422 | id unknown, or the message belongs to another conversation (deliberately indistinguishable), or REST send without `session_id` | drop the pending quote, keep the typed text, tell the user the message is gone |
| `REPLY_TARGET_NOT_QUOTABLE` | 422 | target is a system notice or a voice line | should not be reachable if Reply is hidden on those; if it happens, drop the quote and keep the text |
| `REPLY_SESSION_CLOSED` | 409 | conversation is `resolved` | disable the composer/Reply; treat like the existing closed-chat state |
| (FastAPI validation) | 422 | `reply_to_message_id` is not a UUID | client bug |

Nothing is persisted when a reply is rejected, and a rejected reply into a resolved conversation does **not** reopen it (the normal customer send does reopen a resolved conversation; a reply never will).

REST (all `detail` objects are `{"code","message"}`):

*Capture:* `docs/captures/n2-reply-to/04-err-target-not-found.json`

`POST /api/v1/chat/message`  (auth: none (public customer route))

Request body:

```json
{
  "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
  "user_identifier": "widget-03da58fca9cf433195319ce51dd4485a",
  "message": "x",
  "reply_to_message_id": "00000000-0000-4000-8000-000000000001"
}
```
Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/05-err-target-other-session.json` - A real message id from ANOTHER conversation: identical to not-found (no id probing)

Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/06-err-target-system-notice.json`

Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_QUOTABLE",
    "message": "This message cannot be replied to"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/07-err-target-voice-line.json` - Voice line seeded through ConversationService with voice_channel set (the same code path a voice turn uses); a real ElevenLabs call was not made

Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_QUOTABLE",
    "message": "This message cannot be replied to"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/08-err-session-resolved.json`

Response **409**:

```json
{
  "detail": {
    "code": "REPLY_SESSION_CLOSED",
    "message": "This conversation is closed; replies are disabled"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/09-err-reply-without-session.json`

Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/10-err-not-a-uuid.json` - Malformed id is a plain FastAPI 422 validation error (different shape from the coded errors)

Response **422**:

```json
{
  "detail": [
    {
      "type": "uuid_parsing",
      "loc": [
        "body",
        "reply_to_message_id"
      ],
      "msg": "Input should be a valid UUID, invalid character: found `n` at 0",
      "input": "not-a-uuid",
      "ctx": {
        "error": "invalid character: found `n` at 0"
      }
    }
  ]
}
```

Agent and authed-customer routes use the same envelope:

*Capture:* `docs/captures/n2-reply-to/14-agent-rest-err-session-resolved.json`

Response **409**:

```json
{
  "detail": {
    "code": "REPLY_SESSION_CLOSED",
    "message": "This conversation is closed; replies are disabled"
  }
}
```

*Capture:* `docs/captures/n2-reply-to/16-authed-customer-err.json` - Id from another conversation

Response **422**:

```json
{
  "detail": {
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
}
```

WebSocket error frame (sent only to the socket that sent the bad frame; nothing is saved or fanned out; the socket stays open). The frame has **no correlation id**, so assume it refers to the last message frame you sent:

*Capture:* `docs/captures/n2-reply-to/21-ws-customer-err-not-found.json`

`customer_socket_sent`:

```json
{
  "type": "message",
  "content": "rejected",
  "reply_to_message_id": "00000000-0000-4000-8000-000000000001"
}
```
`customer_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
]
```

*Capture:* `docs/captures/n2-reply-to/23-ws-customer-err-system-notice.json`

`customer_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_TARGET_NOT_QUOTABLE",
    "message": "This message cannot be replied to"
  }
]
```

*Capture:* `docs/captures/n2-reply-to/24-ws-customer-err-voice-line.json`

`customer_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_TARGET_NOT_QUOTABLE",
    "message": "This message cannot be replied to"
  }
]
```

*Capture:* `docs/captures/n2-reply-to/27-ws-customer-err-session-resolved.json`

`customer_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_SESSION_CLOSED",
    "message": "This conversation is closed; replies are disabled"
  }
]
```

*Capture:* `docs/captures/n2-reply-to/22-ws-customer-err-not-a-uuid.json`

`customer_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
]
```

*Capture:* `docs/captures/n2-reply-to/25-ws-agent-err-not-found.json`

`agent_socket_received`:

```json
[
  {
    "type": "error",
    "code": "REPLY_TARGET_NOT_FOUND",
    "message": "The message you are replying to was not found"
  }
]
```

## 6. History payloads and the deleted-original case

Staff (dashboard) message list, replies and non-replies together (the whole list is pasted, in order):

*Capture:* `docs/captures/n2-reply-to/29-history-staff-messages.json` - Staff (dashboard) message list. Tenant-admin route.

Response **200**:

```json
[
  {
    "role": "user",
    "content": "My order 1042 has not arrived",
    "id": "ce7f96a9-3906-4850-b24d-b5db037e0aba",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:23.315837Z"
  },
  {
    "role": "assistant",
    "content": "I'm sorry to hear that. Let me connect you with the team.",
    "id": "b9d67a4b-25f3-4db2-b952-ae66b85c9cba",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:23.349437Z"
  },
  {
    "role": "system",
    "content": "Sam Agent joined the conversation",
    "id": "8bdf05eb-f3c5-447b-9f5f-394166ca5c5b",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "event": "agent_joined"
    },
    "created_at": "2026-10-14T01:30:23.372901Z"
  },
  {
    "role": "human_agent",
    "content": "Hi, Sam here. Can you send a photo of the shipping label?",
    "id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "agent_name": "Sam Agent"
    },
    "created_at": "2026-10-14T01:30:23.394173Z"
  },
  {
    "role": "user",
    "content": "[image]",
    "id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [
      {
        "id": "0a19a8f3-aaaf-4b11-882a-a45f4154bd7d",
        "url": "https://res.example.test/placeholder-label.jpg",
        "content_type": "image/jpeg",
        "width": 800,
        "height": 600,
        "kind": "image",
        "duration_ms": null,
        "poster_url": null,
        "expired": false
      }
    ],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:23.414960Z"
  },
  {
    "role": "user",
    "content": "uh my parcel did not come",
    "id": "1f027138-df8e-4aec-913d-b4d73572b188",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "channel": "voice"
    },
    "created_at": "2026-10-14T01:30:23.442700Z"
  },
  {
    "role": "user",
    "content": "Here is the label, thanks",
    "id": "5396498b-f7b7-4675-a967-384e4011321e",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:25.836347Z"
  },
  {
    "role": "user",
    "content": "Yes I can send it",
    "id": "e44d921f-8ced-4d9b-a52b-896f1d240ddb",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:25.935187Z"
  },
  {
    "role": "user",
    "content": "This photo again?",
    "id": "7ccf518a-8450-4d55-90a9-3a850572dc6e",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
      "sender_type": "customer",
      "kind": "image",
      "snippet": ""
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:26.037169Z"
  },
  {
    "role": "human_agent",
    "content": "Thanks, checking now",
    "id": "e9033751-6f7e-43ac-b026-45271a1c8467",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "agent_name": "Sam Agent"
    },
    "created_at": "2026-10-14T01:30:26.301532Z"
  },
  {
    "role": "human_agent",
    "content": "Got your first message about order 1042",
    "id": "00c166b8-d34a-4177-9acd-761026d27613",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "ce7f96a9-3906-4850-b24d-b5db037e0aba",
      "sender_type": "customer",
      "kind": "text",
      "snippet": "My order 1042 has not arrived"
    },
    "meta": {
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "agent_name": "Sam Agent"
    },
    "created_at": "2026-10-14T01:30:26.367160Z"
  },
  {
    "role": "user",
    "content": "Sending it now",
    "id": "94a4ece6-bcb0-4bbc-bfb3-0263c1aa68a8",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "887a8f5d-35ba-4a45-9968-647e9ec53950",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:27.447484Z"
  },
  {
    "role": "user",
    "content": "plain message",
    "id": "861136de-0385-40c7-9906-942c0ddd405f",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {},
    "created_at": "2026-10-14T01:30:28.708363Z"
  },
  {
    "role": "human_agent",
    "content": "Understood, one moment",
    "id": "3e1831d0-d0c9-4a2c-be10-57bbf7074fd4",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
      "sender_type": "customer",
      "kind": "image",
      "snippet": ""
    },
    "meta": {
      "agent_id": "10e4365c-3be3-4de0-b31c-04d9cd2ef48b",
      "agent_name": "Sam Agent"
    },
    "created_at": "2026-10-14T01:30:29.160426Z"
  }
]
```

Staff session detail embeds `messages[]` with the same `reply_to`, plus `customer_label` and **no** `user_identifier` (credential hygiene, unchanged). Only the head of the object is shown:

*Capture:* `docs/captures/n2-reply-to/30-history-staff-session-detail.json` - Staff (dashboard) session detail with embedded messages; note customer_label and no user_identifier

Response **200**:

```json
{
  "customer_label": "Visitor 66645e12",
  "status": "escalated",
  "messages (first 2 of 14)": [
    {
      "role": "user",
      "content": "My order 1042 has not arrived",
      "id": "ce7f96a9-3906-4850-b24d-b5db037e0aba",
      "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
      "retrieved_chunk_ids": null,
      "token_count": null,
      "attachments": [],
      "reply_to": null,
      "meta": {},
      "created_at": "2026-10-14T01:30:23.315837Z"
    },
    {
      "role": "assistant",
      "content": "I'm sorry to hear that. Let me connect you with the team.",
      "id": "b9d67a4b-25f3-4db2-b952-ae66b85c9cba",
      "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
      "retrieved_chunk_ids": null,
      "token_count": null,
      "attachments": [],
      "reply_to": null,
      "meta": {},
      "created_at": "2026-10-14T01:30:23.349437Z"
    }
  ]
}
```

Agent mobile app, escalation detail. `transcript[]` rows now include `id` (new, so a quote can be located) and `reply_to`. Last row shown:

*Capture:* `docs/captures/n2-reply-to/31-history-agent-app-transcript.json` - Agent mobile app: escalation detail transcript (now has id + reply_to per row)

Response **200**:

```json
{
  "transcript (last row of 14)": {
    "id": "3e1831d0-d0c9-4a2c-be10-57bbf7074fd4",
    "role": "human_agent",
    "content": "Understood, one moment",
    "created_at": "2026-10-14T01:30:29.160426Z",
    "attachments": [],
    "reply_to": {
      "message_id": "5bcf1dee-ba26-4322-a8e0-274fcdc3a6af",
      "sender_type": "customer",
      "kind": "image",
      "snippet": ""
    }
  }
}
```

Authed customer inbox detail (the quoted message is last; `conversation.messages[]` shown from the end):

*Capture:* `docs/captures/n2-reply-to/32-history-customer-conversation.json` - Authed customer inbox detail

Response **200**:

```json
{
  "conversation.messages (last row)": {
    "role": "user",
    "content": "Following up on that",
    "id": "d0bf7ea4-32b6-46e4-ad6c-e5d598acfd4c",
    "session_id": "0a258ea1-13f4-403a-99c0-931453fabf9f",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": "ce1367d7-257b-4489-8063-f176aa88bb41",
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Welcome back Ada, how can I help?"
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:38.430693Z"
  }
}
```

Authed customer pagination endpoint (`/messages`): same `reply_to` on each row:

*Capture:* `docs/captures/n2-reply-to/33-history-customer-messages-page.json`

Response **200**:

```json
{
  "messages (first row of 3)": {
    "role": "human_agent",
    "content": "Welcome back Ada, how can I help?",
    "id": "ce1367d7-257b-4489-8063-f176aa88bb41",
    "session_id": "0a258ea1-13f4-403a-99c0-931453fabf9f",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "agent_name": "Sam Agent"
    },
    "created_at": "2026-10-14T01:30:23.586170Z"
  },
  "has_more": false
}
```

**The widget/hosted page has no history endpoint** that returns `reply_to`: it hydrates from the WebSocket replay (section 4; customer socket replays human-agent messages only). Customer messages typed in an earlier page load therefore come back without their quote in the widget. Authed customer chat (`luran-chat`) uses the history endpoints above and gets everything.

**Original deleted.** If the quoted message no longer exists, `message_id` is `null` and the rest of the quote survives:

*Capture:* `docs/captures/n2-reply-to/34-history-original-deleted.json` - Original agent message deleted directly in the DB (no API deletes a single message) to show the ON DELETE SET NULL shape: message_id becomes null, sender_type/kind/snippet survive. FE shows 'Original message unavailable'.

Response **200**:

```json
[
  {
    "role": "user",
    "content": "Yes I can send it",
    "id": "e44d921f-8ced-4d9b-a52b-896f1d240ddb",
    "session_id": "66645e12-7da4-481a-a7c8-e19b88dd1308",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "reply_to": {
      "message_id": null,
      "sender_type": "agent",
      "kind": "text",
      "snippet": "Hi, Sam here. Can you send a photo of the shipping label?"
    },
    "meta": {},
    "created_at": "2026-10-14T01:30:25.935187Z"
  }
]
```

(No API deletes a single message, so this was produced by deleting the row directly in the scratch database. It also happens when a conversation's original is purged by retention in future.)

## 7. What the FE must do, per surface

### 7.1 Starting a reply (gesture to field)

| Surface | Gesture | Then |
|---|---|---|
| Dashboard (web) | hover a message, click **Reply** | show a quote chip above the composer; on send add `reply_to_message_id` to the agent WS frame (or `agent-message` body) |
| Flutter agent app | long-press, then **Reply**; or swipe right on the bubble | same chip; send `reply_to_message_id` on the agent WS frame |
| Widget / hosted page (desktop) | hover, **Reply** | chip above input; add `reply_to_message_id` to the customer WS frame, or to the `POST /chat/message` body |
| Widget / hosted page (touch), authed `luran-chat` | long-press then **Reply**, or swipe right | same; authed chat uses `/ws/customer/{id}` or `POST /customer/conversations/message` |

Clear the chip after the send succeeds or when the user dismisses it. Only one quote per message.

### 7.2 Where Reply must be hidden

Hide the Reply action (and ignore swipe-to-reply) on:

* any message with `role == "system"`, or `meta.system_notice` set (screen-recording ack, missed-ticket notice, join notices);
* any message with `meta.channel == "voice"` (voice-mode transcript lines; `meta.channel` is `"voice"` on rows persisted during a voice turn - see capture 29, the row `uh my parcel did not come`). **Limit:** voice lines persisted before this release carry no marker and cannot be told apart; they will show Reply and sending will succeed.
* every message when the conversation `status == "resolved"` (the composer is already disabled; keep the actions consistent). The server answers `409 REPLY_SESSION_CLOSED` regardless.

A rating card message (`rating_requested`) is a system notice; do not offer Reply on it.

### 7.3 Rendering the quote

* Show the quote block above the reply text: sender label, then `snippet` (single line, ellipsised).
* Label from `sender_type`: customer surface - `customer` = "You", `agent` = "Support" (the snapshot has no agent name), `ai` = "Assistant". Staff surfaces - `customer` = the session's `customer_label`, `agent` = "Agent" (or "You" if the quoted message is one of the viewer's own, matched through the original when it is loaded), `ai` = "AI".
* `kind == "image"` / `"video"`: show a thumbnail when the original is loaded and its attachment is still present (`attachments[].expired == false`, use `url` / `poster_url`); otherwise show the kind label "Photo" / "Video". Show `snippet` next to it only if non-empty.
* A reply may itself contain an image or recording (its own `attachments[]`), rendered as today below the quote.

### 7.4 Tapping a quote (D4)

1. `reply_to.message_id` is `null` - show **"Original message unavailable"**; no jump.
2. The message with that id is in the loaded list - scroll to it and highlight briefly.
3. Not loaded (older than the window) - do not jump; show the snippet only (for example expand the quote in place). Do not auto-paginate to find it.

### 7.5 Optimistic sends and error frames

Render the quote on your optimistic bubble from the message you picked. On a WS `type: "error"` frame, roll back the last optimistic reply bubble, restore the typed text, and show the message from `message`. On REST, branch on `detail.code`.

## 8. UX consequences to surface

* A conversation resolved while the user is composing a reply: the send is refused with `REPLY_SESSION_CLOSED` (not auto-reopened, unlike a plain customer message). Show the closed-chat state, keep the draft.
* A quoted message that disappears later keeps its text in the quote on other devices but the jump target is gone ("Original message unavailable"). Do not hide the quote.
* Quotes are snapshots: if a quoted image expires after the 30-day retention the quote falls back to the "Photo" label.
* Customers see the quote's `snippet` for agent/AI messages they already saw; staff see customer snippets they already see. Nothing new is exposed, but never log snippets client-side (they are message content).

## 9. Not in the contract

Threads, quoting several messages, editing/forwarding, reactions (spec "Out of scope"). No `reply_count`, no "jump to reply" from the original.

## 10. Verification table

| Behaviour | Status |
|---|---|
| Customer REST reply (text, image quote), with and without `reply_to` | verified live (captures 01-03) |
| All validation errors on REST (not found, other session, system notice, voice line, resolved, no session, malformed id) | verified live (04-10, agent 13-14, authed 16) |
| Agent REST send with/without reply, 409 in resolved | verified live (11-14) |
| Authed customer REST reply + inbox history | verified live (15, 32-33) |
| Customer WS, agent WS, authed customer WS frames both directions, error frames, resolved rejection | verified live (17-28) |
| WS replay on connect carries `reply_to` | verified live (17, 26) |
| Staff history (list and session detail), agent-app transcript | verified live (29-31) |
| Deleted original: `message_id` null, quote survives | verified live, row deleted directly in DB (34); FK is `ON DELETE SET NULL`, also proven in Postgres by `tests/migrations/test_n2_reply_to.py` |
| Voice line rejected | verified live against a row marked through the same code path a voice turn uses; **NOT LIVE** with a real ElevenLabs call |
| AI-answered reply (D5 prompt context, ChatResponse shape T1) | **NOT LIVE** (no LLM key in the capture environment); verified by `tests/api/v1/test_reply_to_api.py::test_ai_prompt_receives_quoted_snippet_but_not_images` |
| Reply combined with image / video attachment | **NOT LIVE** (needs Cloudinary); verified by tests listed in 3.4 |
| Voice lines created before this release are marked | not possible (no historic marker); documented limit in 7.2 |
