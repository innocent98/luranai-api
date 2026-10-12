# FE integration guide — "Who answers first" (`first_responder`)

Backend PR: `feat/n3-who-answers-first` → `develop` (luranai-api). Spec: `docs/superpowers/specs/2026-10-10-n3-who-answers-first-design.md`. SOP: `docs/sop/n3_who_answers_first.md`.

**Provenance.** Every JSON body below is pasted from `docs/fe-integration-captures/who-answers-first/` — requests/responses captured against the real app (uvicorn + ARQ worker, Postgres 15, Redis 7) run **locally** on 2026-10-10, not staging. Placeholders: `<admin-jwt-placeholder>`, `<widget-api-key-placeholder>`, `<visitor_token-placeholder>`, `<session_token-placeholder>`; test tenant/user data is fake (`Acme Test Co`, `customer@example.test`). Items that could not be exercised live are marked **NOT LIVE** inline and listed in the verification table. Local env differences: `AGENT_ALERT_WAIT_SECONDS=5` (prod default 90) so a real missed ticket could be captured; no ElevenLabs credentials; no agent devices registered; no knowledge sources.

## 1. What the feature is (FE terms)

A tenant admin chooses **who answers a customer first**. Three values, exactly these strings:

| Value | Label to show | Behaviour for customers |
|---|---|---|
| `ai` | AI first | AI answers from the knowledge base using the existing grounding dropdowns. |
| `ai_with_handoff` | AI with human handoff | AI answers what it knows, and offers the team whenever it cannot (server forces both "when nothing is found" and "when unsure" actions to `escalate`). |
| `human` | Human only | The AI never replies. The customer gets a fixed message and the team is rung immediately. Automation flows are skipped. Voice is not offered. |

Plus a fix that applies in **every** mode: when an escalation is *missed* (no agent claims it within `AGENT_ALERT_WAIT_SECONDS`, default 90 s), the customer now gets a persisted assistant message "Our team couldn't get to you right now. We'll reply right here as soon as we can." and a `ticket_missed` WebSocket frame.

New tenants start as `human`. Existing tenants with **zero ACTIVE knowledge sources** were flipped to `human` by the migration (count logged on deploy); everyone else stays `ai`. There is no silent runtime switch and no email to admins.

## 2. Endpoints

All admin endpoints are under `/api/v1`, `Authorization: Bearer <token>`, tenant-admin only for PUT (unchanged permission).

### 2.1 `GET /tenant/ai-config`

Capture: `01-ai-config-get-default-new-tenant.json`

Request:
```
GET /tenant/ai-config
```

Response: **200**
```json
{
  "brand_voice": {},
  "allowed_topics": null,
  "blocked_topics": null,
  "escalation_keywords": [],
  "max_response_tokens": 450,
  "grounding_requirement": true,
  "grounding_mode": "balanced",
  "low_confidence_action": "clarify",
  "no_retrieval_action": "clarify",
  "first_responder": "human",
  "require_email_before_escalation": false,
  "require_name_before_escalation": false,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "custom_instructions": null,
  "id": "dc63b968-657b-49f2-be0b-1915609f5762",
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "active_knowledge_sources": 0,
  "created_at": "2026-10-10T22:18:02.055116Z",
  "updated_at": "2026-10-10T22:18:02.055116Z"
}
```

- `first_responder` is one of `ai | ai_with_handoff | human`. `active_knowledge_sources` is **read-only**, computed server-side (count of ACTIVE sources only; PROCESSING/INACTIVE/FAILED do not count). It appears **only here**.
- No token → 401 (`09-ai-config-get-no-auth-401.json`):

```json
{
  "detail": "Could not validate credentials"
}
```

### 2.2 `PUT /tenant/ai-config` — preset rules

The body is partial (only send what changed). Behaviour, all verified live:

| Sent | Stored `no_retrieval_action` / `low_confidence_action` |
|---|---|
| `{"first_responder":"ai"}` | unchanged |
| `{"first_responder":"human"}` | unchanged (not read in this mode) |
| `{"first_responder":"ai_with_handoff", ...anything}` | **forced `escalate` / `escalate`**, even if the same body sends `clarify` |
| `ai_with_handoff` → `ai` | stays `escalate` / `escalate` (the FE must not assume they reset) |
| `{"first_responder":null}` | treated as "unchanged" (never clears; field is NOT NULL) |

Forced preset (note the request sent `clarify` for both, response has `escalate`):

Capture: `04-ai-config-put-handoff-with-clarify-forced.json`

Request:
```
PUT /tenant/ai-config
{
  "first_responder": "ai_with_handoff",
  "no_retrieval_action": "clarify",
  "low_confidence_action": "clarify"
}
```

Response: **200**
```json
{
  "brand_voice": {},
  "allowed_topics": null,
  "blocked_topics": null,
  "escalation_keywords": [],
  "max_response_tokens": 450,
  "grounding_requirement": true,
  "grounding_mode": "balanced",
  "low_confidence_action": "escalate",
  "no_retrieval_action": "escalate",
  "first_responder": "ai_with_handoff",
  "require_email_before_escalation": false,
  "require_name_before_escalation": false,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "custom_instructions": null,
  "id": "dc63b968-657b-49f2-be0b-1915609f5762",
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "active_knowledge_sources": 0,
  "created_at": "2026-10-10T22:18:02.055116Z",
  "updated_at": "2026-10-10T22:18:26.635930Z"
}
```

Switching back to `ai` leaves the knobs at `escalate` (`05-ai-config-put-ai-after-handoff-keeps-escalate.json`):

```json
{
  "first_responder": "ai",
  "no_retrieval_action": "escalate",
  "low_confidence_action": "escalate"
}
```

Explicit null over a stored `ai_with_handoff` (`08-ai-config-put-null-over-handoff.json`) → 200, value unchanged:

```json
{
  "first_responder": "ai_with_handoff",
  "no_retrieval_action": "escalate",
  "low_confidence_action": "escalate"
}
```

Other single-mode PUTs (all 200, same response shape as 2.1): `02-ai-config-put-ai.json`, `03-ai-config-put-human.json`.

#### Error: unknown mode

Capture: `06-ai-config-put-unknown-mode-422.json`

Request:
```
PUT /tenant/ai-config
{
  "first_responder": "robot"
}
```

Response: **422**
```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "body",
        "first_responder"
      ],
      "msg": "Input should be 'ai', 'ai_with_handoff' or 'human'",
      "input": "robot",
      "ctx": {
        "expected": "'ai', 'ai_with_handoff' or 'human'"
      }
    }
  ]
}
```

This is FastAPI's standard 422 `detail` array, not a custom `code`. Dropdowns cannot produce it; treat as a bug if seen.

### 2.3 Public widget config (embedded widget)

`GET /widget/public/config/{tenant_id}` with header `X-API-Key: <public key>`.

Capture: `15-widget-public-config-human.json`

Request:
```
GET /widget/public/config/93c27b29-d555-4314-93de-444abb5ef265
```

Response: **200**
```json
{
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "company_name": "Acme Test Co",
  "company_description": null,
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": null,
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": true,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "first_responder": "human"
}
```

With `ai_with_handoff` the same endpoint returns `"first_responder": "ai_with_handoff"` (`11-widget-public-config-ai_with_handoff.json`).

### 2.4 Hosted support page / visitor bootstrap

`POST /public/visitor/bootstrap` returns the tenant profile under `tenant`. Widget-style bootstrap by `workspace_id` (`35-visitor-bootstrap-private-tenant-by-workspace-id-human.json`), trimmed to the relevant keys; the rest is verbatim in the capture:

```json
{
  "tenant": {
    "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
    "display_name": "Acme Test Co",
    "is_private": false,
    "first_responder": "human"
  }
}
```

Bootstrap by `workspace_slug` for a publicly listed tenant (`38-visitor-bootstrap-listed-tenant-by-slug-human.json`) → `tenant.first_responder` = `"human"`; after switching the tenant to `ai` (`40-visitor-bootstrap-listed-tenant-by-slug-ai.json`) it reads `"ai"`.

Bootstrap by slug for a **private** tenant is refused (`36-visitor-bootstrap-private-tenant-by-slug-403.json`):

```json
{
  "status": 403,
  "body": {
    "detail": "Anonymous chat is not available for this business"
  }
}
```

`GET /public/business/{slug}` for a publicly listed tenant (`37-public-business-profile-listed-tenant-human.json`) returns the full profile with `first_responder` correct.

## 3. Field-nesting traps (read these)

1. **`first_responder` lives in three places with different nesting:** `GET/PUT /tenant/ai-config` → top level; public widget config → top level; visitor bootstrap → `tenant.first_responder` (nested under `tenant`).
2. **Private tenants report `"ai"` on `GET /public/business/{slug}` regardless of the real mode.** That endpoint returns a minimal identity-only profile for private tenants (`is_private: true`); `first_responder` there is the schema default, not the stored value. Live proof (`17-public-business-profile-human.json`, tenant stored as `human`):

```json
{
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "public_slug": "n3-live-tenant-a",
  "display_name": "Acme Test Co",
  "is_private": true,
  "description": null,
  "long_description": null,
  "support_headline": null,
  "support_subheadline": null,
  "support_hours": null,
  "support_response_time": null,
  "support_footer_note": null,
  "widget_primary_color": null,
  "widget_accent_color": null,
  "widget_logo_url": null,
  "widget_greeting_message": null,
  "allow_file_upload": true,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "first_responder": "ai"
}
```

   Use the **bootstrap** payload (`tenant.first_responder`) or the widget config for anything that gates behaviour, never the business-profile endpoint of a private tenant. New tenants are private by default.
3. **`active_knowledge_sources` exists only on `/tenant/ai-config`** (not in widget config or bootstrap). The nudge/banner logic must use the admin config.
4. **The preset writes two *other* fields.** After saving `ai_with_handoff`, the `no_retrieval_action` / `low_confidence_action` in the response differ from what you sent. Always replace form state with the PUT response.
5. **Welcome trigger is not a message that reaches a human.** `hi`/`hello`/`start`/`__welcome__` get the human-mode welcome and create no ticket (`should_escalate: false`, `ticket_id: null`).
6. **After escalation, later customer messages echo the customer's own message.** The response `message` has `role: "user"` and `should_escalate: false` (see 4.3). Do not render it as an assistant reply or treat `should_escalate:false` as "no longer waiting".
7. **Token count is `null`, not `0`,** on the fixed human-only reply.
8. **Voice denial reason is the generic `voice_disabled`** (also used for tenants without the voice grant or with voice turned off). The FE cannot tell "human-only" apart from "no voice plan" by the reason; hide the button using `first_responder` instead.

## 4. Customer chat behaviour in Human-only mode

All via the existing `POST /chat/message` (no auth). Tenant: `human`.

### 4.1 Welcome trigger — no ticket, no AI

Capture: `18-chat-human-welcome-trigger.json`

Request:
```
POST /chat/message
{
  "message": "hi",
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "user_identifier": "customer@example.test"
}
```

Response: **200**
```json
{
  "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "message": {
    "role": "assistant",
    "content": "Hi! Welcome to Acme Test Co. Send us a message and someone from our team will reply.",
    "id": "1f4d3243-82bc-4899-ac78-bc4a28a43adc",
    "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {
      "auto_welcome": true,
      "no_flow_available": true
    },
    "created_at": "2026-10-10T22:18:27.518355Z"
  },
  "user_message_attachments": [],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

### 4.2 First real message — fixed reply, ticket created, team rung

Capture: `19-chat-human-first-message.json`

Request:
```
POST /chat/message
{
  "message": "My order has not arrived",
  "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "user_identifier": "customer@example.test"
}
```

Response: **200**
```json
{
  "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "message": {
    "role": "assistant",
    "content": "Thanks for reaching out — connecting you with someone from our team now.",
    "id": "d01e8c8d-bb44-4900-b12b-816d95d110e6",
    "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {
      "human_first": true
    },
    "created_at": "2026-10-10T22:18:27.621674Z"
  },
  "user_message_attachments": [],
  "should_escalate": true,
  "escalation_reason": "Human-only mode",
  "ticket_id": "990eb61e-2704-4940-905c-96830c45567a"
}
```

- Exact reply text: `Thanks for reaching out — connecting you with someone from our team now.` (em dash). `meta.human_first: true`.
- `should_escalate: true`, `escalation_reason: "Human-only mode"` (this string is the human-readable label; agent surfaces show it as-is), `ticket_id` set.
- The ticket summary in the tickets list reads `"<customer message> | Reason: Human-only mode"` (`27-tickets-list-after-missed.json`, `status: "missed"` there because that capture was taken after the missed fallback; a new ticket is `open`).

### 4.3 Subsequent messages (session already escalated)

Capture: `20-chat-human-second-message-escalated.json`

Request:
```
POST /chat/message
{
  "message": "Hello? Anyone there?",
  "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "user_identifier": "customer@example.test"
}
```

Response: **200**
```json
{
  "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "message": {
    "role": "user",
    "content": "Hello? Anyone there?",
    "id": "98fb8568-d1f0-45b6-ba3f-b48a9a8eab5a",
    "session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {},
    "created_at": "2026-10-10T22:18:27.647943Z"
  },
  "user_message_attachments": [],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

### 4.4 Contact capture first (tenant has `require_email_before_escalation` = true)

First message gets the capture prompt, **not** the fixed reply, and no ticket yet:

Capture: `30-b-chat-human-first-message-contact-prompt.json`

Request:
```
POST /chat/message
{
  "message": "I want to cancel my subscription",
  "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
  "tenant_id": "719107e8-a348-446e-af1e-9b1c06a1192e",
  "user_identifier": "customer3@example.test"
}
```

Response: **200**
```json
{
  "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
  "message": {
    "role": "assistant",
    "content": "Before I connect you with a support agent, please share the best email address for follow-up.",
    "id": "6fcf85aa-f9f9-4955-a92a-2d02f224b8f9",
    "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {
      "contact_capture_required": true,
      "contact_capture_fields": [
        "email"
      ]
    },
    "created_at": "2026-10-10T22:18:33.730301Z"
  },
  "user_message_attachments": [],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

The customer's reply completes capture and escalates (no AI turn in between):

Capture: `31-b-chat-human-contact-capture-reply.json`

Request:
```
POST /chat/message
{
  "message": "customer@example.com",
  "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
  "tenant_id": "719107e8-a348-446e-af1e-9b1c06a1192e",
  "user_identifier": "customer3@example.test"
}
```

Response: **200**
```json
{
  "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
  "message": {
    "role": "assistant",
    "content": "Thanks. Connecting you with our support team now.",
    "id": "4a4d823e-bab0-411c-a2ed-c18fb490ed2a",
    "session_id": "c4842ed5-8c93-495c-a38e-ea06170eee45",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {
      "contact_captured": true
    },
    "created_at": "2026-10-10T22:18:33.850231Z"
  },
  "user_message_attachments": [],
  "should_escalate": true,
  "escalation_reason": "Human-only mode",
  "ticket_id": "d3a5fa06-2216-4e37-af0c-2d3b7772e03f"
}
```

(Name-required tenants get the analogous prompt with `contact_capture_fields: ["name"]`; **NOT LIVE** for name — only the email variant was captured.)

### 4.5 Voice is not offered

Hide the voice button when `first_responder === "human"`. If the FE calls the token endpoint anyway, a granted tenant in `human` mode is refused. Live (`22-voice-token-human-denied-409.json`; the tenant had voice granted, so the refusal is caused only by `human`):

Capture: `22-voice-token-human-denied-409.json`

Request:
```
POST /voice/session/token
{
  "tenant_id": "93c27b29-d555-4314-93de-444abb5ef265",
  "luran_session_id": "d70651a5-4665-4550-89bc-51ad7ae51043",
  "text_out": false
}
```

Response: **409**
```json
{
  "detail": {
    "reason": "voice_disabled"
  }
}
```

**NOT LIVE — the success path.** For a control tenant in `ai` mode the same call passes the gate and then fails at the ElevenLabs call because this local env has no provider credentials (`33-b-voice-token-ai-mode-passes-gate.json`, 503 `voice_provider_unavailable`). A real minted token/`conversation_token` was never captured. Other voice reasons (`minute_cap_reached`, `concurrency_limit`, `voice_not_configured`, `session_not_found`) come from code, not captures.

## 5. Missed-ticket notice (all modes)

Trigger: ticket still unclaimed `AGENT_ALERT_WAIT_SECONDS` after escalation (default **90 s** in production; **5 s** in this capture). The server (a) persists an assistant message, (b) sends a WebSocket frame to the **customer** socket only (not agents). Exactly once per missed ticket; a retry of the job does not repeat it.

### 5.1 WebSocket frame (live)

Customer socket: `ws://<host>/api/v1/ws/chat/{session_id}?user_identifier=<identifier>`. Captured with a real WebSocket client connected before the escalation; this was the only frame received before it arrived (`25-ws-customer-frames-missed-ticket.json`):

```json
[
  {
    "type": "ticket_missed",
    "message_id": "7136482c-d991-4b6a-8f20-234d3836a4dc",
    "content": "Our team couldn't get to you right now. We'll reply right here as soon as we can.",
    "timestamp": "2026-10-10T22:18:33.227476+00:00"
  }
]
```

Note this frame type is `ticket_missed` (not `agent_message`/`message`), has **no `role` and no `agent_name`**, and its `message_id` equals the persisted message id. The frame type's timestamp is ISO-8601 with `+00:00`; the REST message below uses `Z`.

### 5.2 Persisted message (live)

`GET /chat/sessions/{session_id}/messages` (admin). The last element of `26-session-messages-after-missed.json`:

```json
{
  "role": "assistant",
  "content": "Our team couldn't get to you right now. We'll reply right here as soon as we can.",
  "id": "7136482c-d991-4b6a-8f20-234d3836a4dc",
  "session_id": "98b076c9-78b9-40ea-b461-26878837765c",
  "retrieved_chunk_ids": null,
  "token_count": null,
  "attachments": [],
  "meta": {
    "system_notice": "ticket_missed"
  },
  "created_at": "2026-10-10T22:18:33.227476Z"
}
```

It is an ordinary `assistant` message, identified by `meta.system_notice == "ticket_missed"`. It therefore also arrives in history reloads — the FE must dedupe by `id` against the WebSocket frame's `message_id`.

Missed ticket in the tickets list shows `"status": "missed"` (`27-tickets-list-after-missed.json`).

## 6. UX consequences the FE must surface

1. **Switching to Human only needs a confirm:** "The AI will stop replying to all new conversations." Only call the PUT after confirmation.
2. **Locked knobs in `ai_with_handoff`:** show the two action dropdowns read-only ("Set by AI with human handoff"); the server overwrites them anyway (2.2).
3. **Human only hides the dropdowns** and shows: "Customers are connected to your team immediately. Make sure your agents have the Luran agent app so they get rung." If nobody has the app, every customer ends in the missed notice after 90 s.
4. **Nudge** (mode `human` and `active_knowledge_sources >= 1`): dismissible "Your knowledge base has N active sources — let AI answer first?" → saves `ai_with_handoff`.
5. **Banner** (mode `ai` or `ai_with_handoff` and `active_knowledge_sources == 0`): "Your knowledge base is empty — the AI has nothing to answer from. Add sources or switch to Human only."
6. **Tell admins what the migration did:** tenants with zero ACTIVE sources were moved to `human` on deploy; the card must make the current mode obvious so no admin is surprised that the AI went quiet.
7. **Widget:** hide the voice button in `human`; render the missed message as a normal system/assistant bubble; dedupe by id with history replay.
8. **Non-admins** get 403 on PUT as today (not re-captured; unchanged permission).

## 7. State machine (customer side, Human-only)

| State | Entered by | FE shows | Next |
|---|---|---|---|
| Idle | session start | human-mode welcome (4.1) | first message |
| Contact capture | first message, tenant requires name/email, field missing | capture prompt (4.4) | capture reply |
| Waiting for team | `should_escalate: true` | fixed reply + waiting state | agent `agent_message` frames, or `ticket_missed` |
| Missed | `ticket_missed` frame | notice bubble; keep the chat open and keep listening | agent reply later; customer may keep typing (4.3 shape) |
| Reopened | ticket resolved/closed and customer writes again | same as first message | per existing ticket-reopen rules (**NOT LIVE**) |

Polling: none required; the WebSocket is the push channel. If the socket is down, reload history with the messages API and dedupe by `id`.

## 8. Verification table

| Behaviour | Status | Evidence / reason |
|---|---|---|
| GET `/tenant/ai-config` shape incl. `first_responder`, `active_knowledge_sources` | verified-live (local) | 01 |
| PUT each mode (`ai`, `human`, `ai_with_handoff`) | verified-live (local) | 02, 03, 04 |
| handoff + `clarify` in same body forced to `escalate` | verified-live (local) | 04 |
| handoff → ai keeps `escalate` | verified-live (local) | 05 |
| unknown mode 422 body | verified-live (local) | 06 |
| `null` over stored handoff = unchanged | verified-live (local) | 08 |
| No-token 401 | verified-live (local) | 09 |
| Public widget config `first_responder` (handoff, human) | verified-live (local) | 11, 15 |
| Bootstrap `tenant.first_responder` (human, ai), private-slug 403 | verified-live (local) | 35, 36, 38, 40 |
| Private-tenant business profile reports `ai` | verified-live (local) | 17 |
| Human welcome, first message, escalated follow-up | verified-live (local) | 18, 19, 20 |
| Contact-capture prompt + reply (email) | verified-live (local) | 30, 31 |
| Contact capture for name | NOT LIVE | code path only; same mechanism as email |
| Voice denied for human-only (409 `voice_disabled`) | verified-live (local) | 22 |
| Voice token success body | NOT LIVE | no ElevenLabs credentials locally |
| Missed ticket: persisted message + `ticket_missed` WS frame | verified-live (local, 5 s timer) | 25, 26, 27; production wait is 90 s, only the timer value differs |
| Missed notice exactly once on job retry | NOT LIVE | covered by unit test `test_fallback_posts_missed_notice_once` |
| Agent-side display of `Human-only mode` | NOT LIVE | agent apps render `escalation_reason` as plain text; ticket summary string captured (27) |
| Agent actually rung on a device | NOT LIVE | no FCM devices locally |
| Ticket reopen after resolve in human mode | NOT LIVE | unit tests only |
| Non-admin PUT 403 | NOT LIVE | unchanged existing permission |
| Staging behaviour and the migration's affected count | NOT LIVE | to be recorded from the staging deploy log after merge |
