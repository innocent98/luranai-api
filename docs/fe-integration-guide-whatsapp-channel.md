# FE integration guide: WhatsApp channel (quick connect, demo slice)

Backend PR: `feature/whatsapp-channel-backend` → `develop` (luranai-api). SOP: `docs/sop/whatsapp_channel_backend.md`. Plan: `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md`.

**Provenance.** Every JSON body below is pasted from `docs/fe-integration-captures/whatsapp-channel/`, recorded on 2026-10-27 against the real app (uvicorn + arq worker, Postgres 15, Redis 7, real Alembic-migrated schema) run **locally**, driven by `tests/fakes/live_capture.py`. Three things differ from production and are marked inline:
1. **The sidecar is a FAKE** (`tests/fakes/fake_sidecar.py`), not Baileys. Pairing codes look like `FAKE5673` (the real ones are 8 characters; the exact display grouping comes from the real sidecar and has not been seen through this backend yet). No real WhatsApp number or phone was involved.
2. **The LLM is stubbed.** AI replies read `[stubbed AI] …`. Everything else in the pipeline (routing, DB, arq, sidecar client) is real.
3. Local env: `WHATSAPP_MAX_CONNECTIONS=2`, IDs/tokens are throwaway (`<admin-jwt-placeholder>`), phone numbers are fake (`2348000000001`…), people are fake (`Acme Alpha Test Co`, `Ada`).

Anything not exercised live is marked **NOT LIVE** and listed in the table at the end.

**Section 8 (images + rating)** was captured the same way on 2026-10-28 into `docs/fe-integration-captures/whatsapp-media-rating/` (driver `tests/fakes/live_capture_media.py`). Extra stand-ins there: **moderation and Cloudinary are stubbed** (image URLs look like `https://res.cloudinary.com/demo/image/upload/luran/chat_images/<id>.jpg` but nothing was uploaded), and the sidecar's media store and image send are the fake's.

## 1. What the feature is (FE terms)

A tenant admin connects **a WhatsApp number they own** to Luran. They type the number, tick a consent box, and get an **8-character pairing code**. On their phone they open *WhatsApp → Linked devices → Link a device → Link with phone number instead* and type the code. Luran then answers customers who message that number, using the same AI/human pipeline as the website widget. Agents see the conversations in the normal inbox, tagged `source: "whatsapp"`.

Things the user can do and should be told:
- **Keep using their phone / WhatsApp Web alongside the AI.** When the owner types a reply from their phone, the AI goes quiet on that chat for **2 hours** (default) and the owner's message appears in the transcript. Say this on the connect screen: it is a selling point.
- Use an **aged business number** that is only used for business. Fresh numbers get restricted quickly. (Copy suggestion, not a backend rule.)
- Show the consent text verbatim and send back its `version`. Do **not** add your own "unofficial" or "not affiliated" wording on this screen (product decision; liability wording lives in the Terms of Service).

Gating: the tenant needs the **platform grant** (`can_use_whatsapp` + `max_whatsapp_numbers`, set by a Luran platform admin) and the global switch `WHATSAPP_ENABLED` must be on. Otherwise every create call returns `403 whatsapp_not_enabled`: show an "ask Luran to enable WhatsApp" state, not an error.

## 2. Conventions and traps (read first)

| Trap | Detail |
|---|---|
| **Error body shape** | WhatsApp endpoints return the flat contract shape `{"code": "...", "message": "..."}` at the top level. It is **not** wrapped in `{"detail": ...}` like some other endpoints. Auth failures (`401`/`403` from login or role checks) still use `{"detail": "..."}`: handle both. |
| **Pairing code is shown once** | It is in the `201`/`200` response body only. It is never stored, never logged and cannot be fetched again. Keep it in component memory (not localStorage, not the URL). If the user reloads, call `POST …/pairing-code` for a new one. |
| **Status is read-only and webhook-driven** | `awaiting_pair → connecting → connected` is flipped by the sidecar, not by any call you make. After creating, **poll `GET /whatsapp/connections/{id}`**. Suggested: every 2 s while `awaiting_pair`/`connecting` (stop after the code's 180 s expires and offer "new code"), every 15 s otherwise. |
| `last_disconnect_reason` is history | After a new code is issued the status returns to `awaiting_pair` but `last_disconnect_reason` keeps its old value (capture 29). Only show it as a reason while `status` is `logged_out`, `reconnecting` or `failed`. |
| Timestamps | `last_connected_at` is ISO-8601 UTC with `Z`, or `null`. |
| `phone_masked` | e.g. `+234••••••0001`. The full number is never returned after creation. Phone input is **digits with country code and no `+`** (`+2348…` is rejected with `invalid_phone`). |
| **Unpaired attempts expire** | A connection still `awaiting_pair` 30 minutes after it was created may be removed by the backend when anyone creates a connection (to free the number and the capacity slot); the next `GET` is then `404 not_found`. Treat a 404 while pairing as "attempt expired, start over". |
| Cross-tenant / unknown id | Always `404 {"code":"not_found",…}`, never `403`. Do not special-case. |
| **The AI only answers if "who answers first" allows it** | New tenants start with `first_responder: "human"` (see `docs/fe-integration-guide-who-answers-first.md`). In that mode WhatsApp customers get the fixed "connecting you with someone from our team" message and a ticket, never an AI answer. Capture 22 shows the tenant switching to `ai`. Surface this on the WhatsApp screen when the tenant is human-only. |
| Images and other media | **Images now work both ways** (section 8). Voice notes, video, documents and stickers still get one fixed reply, *"Sorry, I can only read text messages and images for now. Please type your question and I'll help."*, **except while a human owns the chat** (agent assigned, escalated, owner paused, AI off): then nothing is sent and the item shows in the transcript as a placeholder line. |

## 3. Endpoints

All under `/api/v1`, `Authorization: Bearer <token>`, **tenant admin only** (`get_current_tenant_admin`), always scoped to the caller's tenant.

### 3.1 `GET /whatsapp/consent`
**Updated 2026-10-28** (activation track). Live capture `docs/fe-integration-captures/activation/36-whatsapp-consent.json` (the older `whatsapp-channel/01-consent.json` shows the superseded draft1 text). Response **200**:
```json
{
  "version": "2026-10-28-draft2",
  "text": "Luran connects your WhatsApp number as a linked device so it can answer customers who message you. WhatsApp can change its policies, restrict access, or disconnect the linked device. Luran replies only to customers who message you first. You can keep using WhatsApp on your phone or WhatsApp Web at the same time: when you type a reply yourself, the AI pauses on that chat for a while. You can disconnect at any time."
}
```
The text is the approved copy (no draft marker); the `version` changes whenever the text does. Always fetch it, render `text` verbatim next to the checkbox, and echo `version` back; a stale version is rejected (below).

### 3.2 `POST /whatsapp/connections`
Request (shape from capture `08-create-201.json`; `consent.version` updated to the current `2026-10-28-draft2`, the capture itself was recorded with draft1):
```json
{
  "phone_number": "2348000000001",
  "label": "Main line",
  "consent": {"version": "2026-10-28-draft2", "accepted": true}
}
```
Response **201**:
```json
{
  "id": "0219adb0-41b9-4e65-a3c7-c3dc1d27d878",
  "status": "awaiting_pair",
  "pairing_code": "FAKE5673",
  "expires_in_s": 180
}
```
(`id` and the code in the file differ run to run; the shape is what matters.) `label` is optional (max 100 chars). Show the code large, grouped, with a 180 s countdown (`expires_in_s`).

Errors (all captured, exact strings):

| Status | `code` | `message` | Capture | What to show |
|---|---|---|---|---|
| 403 | `whatsapp_not_enabled` | `WhatsApp is not enabled for this account.` | 02 | "Ask Luran to enable WhatsApp" |
| 422 | `consent_required` | `Accept the current WhatsApp terms (version 2026-10-28-draft2) to continue.` (built from the current version; captures 05/06 pre-date the change and show `2026-10-27-draft1`) | 05 (missing), 06 (stale version) | Re-fetch consent, re-ask. Also returned when `accepted` is false |
| 422 | `invalid_phone` | `Enter the number as digits with country code and no '+', e.g. 2348012345678.` | 07 | Inline field error |
| 409 | `number_in_use` | `This number is already connected to a Luran account.` | 14 | Do not reveal which account |
| 409 | `connection_limit` | `Your plan allows 2 WhatsApp number(s).` | 16 | Number reflects the grant |
| 503 | `capacity_exceeded` | `WhatsApp is at capacity right now. Please try again later.` | 17 | Retry later; this is a platform-wide cap, not the tenant's fault |

Check order: gating → consent → phone format → `number_in_use` → `connection_limit` → capacity. **NOT LIVE:** the `502 sidecar_unavailable` (`Could not reach WhatsApp. Please try again.`) and the sidecar-originated `503 capacity_exceeded` were exercised by tests only (`tests/whatsapp/test_connections_api.py`), not in the live run; their bodies come from the service code.

### 3.3 `GET /whatsapp/connections` and `GET /whatsapp/connections/{id}`
Single (captures `09`, `11`, `27`). While waiting for the code:
```json
{
  "id": "0219adb0-41b9-4e65-a3c7-c3dc1d27d878",
  "label": "Main line",
  "status": "awaiting_pair",
  "phone_masked": "+234••••••0001",
  "ai_enabled": true,
  "last_connected_at": null,
  "last_disconnect_reason": null
}
```
After the phone is linked (capture 11):
```json
{
  "id": "801152e2-0db2-42bf-946d-a3219009ff90",
  "label": "Main line",
  "status": "connected",
  "phone_masked": "+234••••••0001",
  "ai_enabled": true,
  "last_connected_at": "2026-10-27T18:16:57.003243Z",
  "last_disconnect_reason": null
}
```
The list endpoint returns an array of the same object (capture `13-list.json`; empty `[]` when none).

### 3.4 `POST /whatsapp/connections/{id}/pairing-code`
Request: no body. Response **200** (capture 10):
```json
{"pairing_code": "FAKE1910", "expires_in_s": 180}
```
Only valid while `status` is `awaiting_pair` or `logged_out`. Otherwise **409** (capture 12):
```json
{"code": "not_pairable", "message": "This connection can only be paired while it is waiting."}
```
After a successful call on a `logged_out` connection the status goes back to `awaiting_pair` (captures 28, 29).

Other errors on this route: `403 whatsapp_not_enabled` if the grant was revoked, and `503 capacity_exceeded` when re-pairing a `logged_out` connection while the platform is at capacity (re-pairing makes it live again). **Rate limits** (HTTP `429` with the app's standard rate-limit body `{"detail": {"error": "Rate limit exceeded", "limit": …, "window_seconds": …, "retry_after_seconds": …}}` and a `Retry-After` header): 5 codes per 10 minutes per connection and per IP. Creating connections is limited to 10 per 10 minutes per tenant and 5 per day per phone number across all tenants (each create makes WhatsApp notify that number's owner). Show a "try again later" message using `retry_after_seconds`. **NOT LIVE:** the 429 responses for these routes (unit-tested with an in-memory counter and against real Redis for the limiter itself; their exact body is the shared rate-limit shape, not captured here).

### 3.5 `PATCH /whatsapp/connections/{id}`
Request (capture 26): `{"ai_enabled": false, "label": "Support line"}`. Both fields optional; unknown fields are rejected with 422. Response **200**: the connection object (3.3). With `ai_enabled: false` the number stays linked, customer messages are stored and shown to agents, and the AI does not answer.

### 3.6 `DELETE /whatsapp/connections/{id}`
Response **204**, empty body (capture 30). Logs the number out of the sidecar, wipes contact data and removes the connection; conversations stay in the inbox. Subsequent `GET` → 404 (capture 31). If the sidecar cannot be reached the call fails with `502 sidecar_unavailable` and nothing is deleted (**NOT LIVE**; tests only): show "couldn't disconnect, try again".

### 3.7 Not found (any `{id}` route, other tenant's or unknown id)
Captures 18-21, **404**:
```json
{"code": "not_found", "message": "WhatsApp connection not found"}
```

## 4. Status state machine

| `status` | Meaning | FE shows | Suggested polling |
|---|---|---|---|
| `awaiting_pair` | Code issued, waiting for the phone | Code + countdown + the three phone steps; "new code" button when expired | 2 s |
| `connecting` | Phone accepted the code, finishing the link | Spinner | 2 s |
| `connected` | Live. AI/agents can answer | Green pill, `last_connected_at`, AI toggle, disconnect | 15 s |
| `reconnecting` | Dropped; the sidecar is retrying by itself | Amber "reconnecting"; no action needed | 5 s |
| `logged_out` | The owner unlinked the device on the phone (or WhatsApp did). **No automatic re-pair** | Red banner "WhatsApp was disconnected from the phone" + "Reconnect" → `POST …/pairing-code` | 15 s |
| `failed` | Unrecoverable sidecar error | Error state + contact support / delete and re-add | 15 s |

`last_disconnect_reason` values seen: `logged_out_from_phone` (live, capture 27), `closed_<code>` for other close codes, `connection_replaced`, `restart_required` (**NOT LIVE**: derived from the sidecar close codes 401/440/515 in code and unit tests).

The live run covered `awaiting_pair → connecting → connected → logged_out → awaiting_pair`. `reconnecting` and `failed` were not driven live (the unit tests cover the status handler).

## 5. What the inbox shows (agents and admins)

These are existing staff endpoints, shown here only for what is new for WhatsApp. Capture `23-staff-session-detail-after-ai.json` (`GET /chat/sessions/{id}`, trimmed):
```json
{
  "source": "whatsapp",
  "status": "active",
  "meta": {"entry_point": "whatsapp", "contact_name": "Ada"},
  "visitor": {"id": "681e524d-a347-444d-8293-636a62e93c8b", "name": "Ada", "email": null, "phone": null, "first_seen_at": "2026-10-27T18:16:58.917276Z", "last_seen_at": "2026-10-27T18:16:58.917276Z", "is_blocked": false},
  "customer_label": "Ada",
  "messages": [
    {"role": "user", "content": "What are your opening hours?", "meta": {"channel": "whatsapp"}},
    {"role": "assistant", "content": "[stubbed AI] Our opening hours are 9am to 5pm, Monday to Friday.", "meta": {"channel": "whatsapp", "retrieval_hit": false, "…": "…"}}
  ]
}
```
- **`source` is new on session responses** (`"whatsapp"`, `"widget"`, …, or `null` on older rows). Use it for a channel badge. The customer's WhatsApp number is **not** exposed (`visitor.phone` is `null`); the label is their WhatsApp profile name (`push_name`) or the generic customer label.
- `meta.channel == "whatsapp"` on customer and AI messages.
- **Owner typed from their phone** (capture 24): a `human_agent` message with `meta: {"source": "owner_phone"}`:
```json
{"role": "human_agent", "content": "Hi Ada, it's the owner. I'll take it from here.", "meta": {"source": "owner_phone"}, "…": "…"}
```
  Render it as the business's own message (not as an agent's name). While the pause is on, the next customer message is stored (`role: "user"`, `meta.channel: "whatsapp"`) and **no AI reply follows** (verified live: the sidecar received no send for it).
- **Agent replies from the dashboard reach WhatsApp.** `POST /chat/sessions/{id}/agent-message` (capture 25, `201`, unchanged response) is forwarded to the customer's WhatsApp automatically; the fake sidecar received exactly that text (`sidecar-sent-after-agent.json`). Ticket resolved/closed notices are forwarded as fixed text.

## 6. UX consequences the FE must surface

1. **Pairing code is shown once.** Never persist it. Offer "new code" on expiry or reload.
2. **A number can be restricted by WhatsApp.** Show the consent text verbatim; do not soften it. The text is a draft until approved: do not ship the final connect screen before the wording is approved.
3. **The AI pauses when the owner types.** Explain on the connected screen: "If you reply from your phone, the AI stays quiet on that chat for 2 hours." There is no manual resume button in this slice.
4. **`ai_enabled: false`** means customers get **no automatic reply at all** (messages are stored for agents). Make the toggle's consequence explicit.
5. **A plan/conversation limit makes WhatsApp go silent** (customers get nothing, by design). The tenant is only alerted in server logs in this slice: show the usual plan-limit banner if you already have one.
6. **Human-only tenants don't get AI answers on WhatsApp** (section 2).
7. **`logged_out` needs a new code**; nothing reconnects automatically.
8. **Capacity is platform-wide** (`capacity_exceeded`): not the tenant's quota. Disconnecting an unused connection frees a slot immediately.

## 7. Webhook (not an FE surface)

`POST /api/v1/webhooks/whatsapp/linked` is called by the sidecar, never by the browser. Captured for the sidecar track: valid → `200 {"status":"accepted"}` (`zz-webhook-valid.json`), bad or missing signature → `401 {"error":"invalid_signature"}` (`zz-webhook-bad-signature.json`, `zz-webhook-missing-header.json`). Unknown `session_key` → `200 {"status":"ignored"}` and nothing is processed (**NOT LIVE**; tests only). Session keys and signatures in those files are replaced by placeholders.

## 8. Images and rating over WhatsApp (added 2026-10-28)

Backend PR: `feature/whatsapp-media-rating-backend` → `develop`. SOP: `docs/sop/whatsapp_media_and_rating.md`. **No new FE endpoint**: everything below arrives through endpoints and frames you already render; this section is what changed in their payloads and what the UI must do.

### 8.1 Customer images in the inbox
A WhatsApp image is a normal `role: "user"` message with `attachments` (the same shape web-chat uploads have). The caption is the message `content` (`"[image]"` when there is none). Capture `03-staff-session-detail-after-image.json` (`GET /chat/sessions/{id}`, trimmed):
```json
{"role": "user", "content": "Is this the right model?", "attachments": [{"id": "6e97385e-4d75-4d4c-8a68-d7ebc9c40e7f", "url": "https://res.cloudinary.com/demo/image/upload/luran/chat_images/e8c3881bf8e6.jpg", "content_type": "image/png", "width": 24, "height": 24, "kind": "image", "duration_ms": null, "poster_url": null, "expired": false}], "reply_to": null, "meta": {"channel": "whatsapp"}}
{"role": "assistant", "content": "[stubbed AI] I can see 1 image(s) you sent.", "attachments": [], "meta": {"channel": "whatsapp", "…": "…"}}
```
- Render attachments exactly as for the widget (thumbnail, open full size). `expired: true` / `url: null` after the retention window (30 days).
- **`content_type` is the type of the original upload** (`image/png` above) while the stored file is re-encoded to JPEG by the Cloudinary upload settings (the capture's stub URL only mimics that): do not infer the file extension from it.
- **The AI answers the image** (the stub's reply above says how many images the model received: 1). If the chat is owner-paused, the connection has AI off, or a human owns it (session `escalated` or an agent assigned), the image is stored and shown with **no AI reply** (capture `07`: the `"here is the receipt"` message has its attachment, no assistant message follows, the sidecar received no send).
- Agents watching live get the existing `customer_message` frame with the same `attachments` array (`message_id`, `content`, `customer_label`, `attachments`, …). **NOT LIVE** (no socket client in the capture run); shape asserted by `tests/whatsapp/test_media_inbound.py`.
- Trouble cases are visible as transcript lines, not errors (capture `07`/`09`):
  - Image that could not be received (expired in the sidecar, corrupted, over 10 MB, not a real image): user message `"[The customer sent an image that could not be received.]"` plus, unless a human owns the chat, an assistant line `"Sorry, I couldn't open that image. Could you send it again?"` (`meta.source: "whatsapp_media_fallback"`). Over the size cap the reply is `"Sorry, that image is too large for me to open. Please send a smaller one."`
  - **Moderated image: nothing appears in the transcript at all** and the customer is told `"Sorry, I can't accept that image. Please send a different one."` (capture `04`). The moderation reason is never exposed anywhere.
  - Voice note / video / document / sticker: user message `"[The customer sent a voice note, video, document or sticker. Luran can't read these on WhatsApp yet.]"`. While nobody owns the chat the customer gets the fixed text-only line (capture `10`, sent once); **while a human owns it nothing is sent** (capture `08`: the fake sidecar received no send; the placeholder is in the transcript, `09`, session `status: "escalated"`).

### 8.2 Agent images reach the customer
Unchanged endpoints. Upload with `POST /chat/sessions/{id}/upload` (capture `11`, `200`):
```json
{"attachment_id": "89fdcf68-ea55-4deb-9447-229491338c16", "url": "https://res.cloudinary.com/demo/image/upload/luran/chat_images/3175312101e7.jpg", "content_type": "image/png", "width": 50, "height": 50, "kind": "image", "duration_ms": null, "poster_url": null, "bytes": 160}
```
then `POST /chat/sessions/{id}/agent-message` with `{"message": "Here is the setup diagram", "attachment_ids": ["89fdcf68-…"]}` (capture `12`, `201`, same response shape as before). **The customer's WhatsApp receives a real image with the agent's text as its caption** (capture `13`: the fake sidecar got `{"to_ref": "…", "image": {"url": "https://res.cloudinary.com/…/3175312101e7.jpg", "caption": "Here is the setup diagram"}}`).
- The old "Your support agent sent an image, which can't be shown in WhatsApp yet." notice is **gone**; do not mention it in UI copy.
- Several images: caption on the first only. An agent text longer than 1024 characters is sent as its own message after the images.
- Only images uploaded through the endpoint above are forwarded. Videos and anything else are not sent to WhatsApp (the agent sees no error). **Delivery is asynchronous and silent**: if the number is disconnected, rate-capped or the image is refused, the agent's message still shows as sent in the dashboard (same as text today).

### 8.3 Rating request on WhatsApp
When an agent requests a rating (`POST /tickets/{id}/rating-requests`, capture `14`, `201`, unchanged) or a ticket is resolved with an assigned agent, a WhatsApp customer now **receives the request as a message** (capture `15`):
`How was your chat with Alpha? Reply with a number from 1 (poor) to 5 (great).` (`Alpha` = the agent's first name; if the agent has no name the prompt is `How was your chat with our team? …`.) The transcript still stores the shorter widget prompt `How was your chat with Alpha?`.

The customer answers with a bare `1`-`5` (also `4/5`, `3 stars`, `5⭐`, trailing `.`/`!`). The backend records it **exactly like a widget rating** and replies once `Thanks! We've recorded your 5/5.` (capture `16`). Capture `17` (`GET /reviews/session`, unchanged shape):
```json
{"score": 5, "comment": null, "meta": {"channel": "whatsapp"}, "session_id": "076904b2-…", "customer_label": "Ada", "rated_agent_id": "70bf8d5e-…", "source": "manual_request", "rating_request_id": "cf9a08e7-…", "rated_agent_name": "Alpha Owner", "ticket_id": "304e347e-…", "ticket_number": "TKT-CAPTURE-1", "rated_party": "human"}
```
- **New: `meta.channel == "whatsapp"` on a rating** (the only difference from a widget rating). A WhatsApp rating has no comment.
- A second answer, an answer after the 7-day expiry, a digit when no request is open, or anything that is not a bare number is treated as **ordinary chat text** (capture `18`: the second `1` went to the AI; capture `19`: still exactly one rating, score 5).
- The rating prompt is only "open" on WhatsApp once it was actually delivered; if the tenant's number was disconnected at that moment, a stray `5` is just text.

### 8.4 Agent joined line
When an agent takes over a WhatsApp conversation (`POST /chat/sessions/{id}/takeover`, capture `20`, `200`, unchanged response) the customer receives one WhatsApp message (capture `21`): `You're now chatting with Alpha.` (first name only; `a member of our team` when the agent has no name). The same agent taking over again does not send it twice; a different agent does. It is not sent while the owner has the chat paused from the phone. The in-widget transcript still shows `<full name> joined the conversation`. No other system notice is forwarded.

### 8.5 UX consequences the FE must surface
1. **Images are read by the AI on WhatsApp**: say so where tenants configure WhatsApp, and that images are moderated (a flagged image is dropped, the customer sees a generic refusal, the tenant sees nothing).
2. **No reply to media while a human owns the chat** is by design; the placeholder line tells the agent to check the customer's media on the phone for voice notes etc.
3. A **moderated or unreadable image may leave no trace** (moderated) or a placeholder (unreadable): do not promise tenants a complete media log.
4. **The customer's rating arrives without a comment.** Do not show an empty comment box as "missing".
5. **A tenant that turned file uploads off** (`widget_allow_file_upload`) also stops WhatsApp images: the customer is told `Sorry, I can't take images in this chat. Please type your question.` and the transcript shows the "could not be received" placeholder. Tell tenants the switch covers both channels. More than 10 image attempts from one chat within an hour are placeholder-only (no reply).
6. Rating prompts are delivered to WhatsApp automatically; there is nothing to configure and no way to disable it per channel in this slice.

## 9. Verification table

| Behaviour | Status |
|---|---|
| Consent GET; create 201; list/get shape; regenerate code; `not_pairable` | **Verified live** (fake sidecar) |
| `whatsapp_not_enabled`, `consent_required` (missing + stale), `invalid_phone`, `number_in_use`, `connection_limit`, `capacity_exceeded` (backend cap) | **Verified live** |
| Cross-tenant 404 on GET/PATCH/DELETE, unknown id 404 | **Verified live** (POST pairing-code 404 and unit isolation: tests only) |
| Status flow `awaiting_pair → connecting → connected → logged_out → awaiting_pair` through signed webhooks and arq | **Verified live** |
| Inbound customer text → AI (LLM **stubbed**) → reply enqueued → delivered to fake sidecar | **Verified live** |
| Non-text → fixed reply (2026-10-27 wording; reworded and made conditional in section 8) | **Verified live** (re-verified 2026-10-28, section 8) |
| Owner typed from phone → pause, `owner_phone` message, paused customer message stored, no AI reply | **Verified live** |
| Agent REST reply → forwarded to WhatsApp once | **Verified live** |
| `source: "whatsapp"` + `meta.channel` on staff session | **Verified live** |
| PATCH `ai_enabled`/`label`, DELETE 204 → GET 404 | **Verified live** |
| Pairing code absent from DB columns and from API/worker logs | **Verified live** (log grep) + tests |
| Sidecar-originated `503`, `502 sidecar_unavailable`, delete-while-sidecar-down | Tests only |
| `reconnecting` / `failed` statuses; disconnect reasons other than `logged_out_from_phone` | Tests only |
| Pause expiry (AI resumes after 2 h), pause extension | Tests only (time-shifted) |
| 402 plan-limit silence, AI-disabled path, ticket resolved/closed forwarding | Tests only |
| Customer image + caption → vision call received the image → AI reply delivered; attachment visible on the staff session | **Verified live** (captures `02`, `03`; moderation, Cloudinary and LLM stubbed) |
| Moderated image: nothing stored, one fixed sentence | **Verified live** (stub flags a 7x7 image) |
| Corrupt (sha256 mismatch) image: placeholder + "couldn't open" | **Verified live** |
| AI-off image stored with attachment, no send | **Verified live** |
| Voice note while escalated: stored, no send; with nobody owning: fixed reply | **Verified live** |
| Agent upload + image message → sidecar receives `image {url, caption}` | **Verified live** (fake sidecar enforces `res.cloudinary.com`) |
| Agent takeover → customer gets the join line (`21`) | **Verified live** |
| Rating prompt delivered → `5` → thank-you → rating attributed to agent + ticket → replay is ordinary text | **Verified live** |
| `customer_message` WS frame carrying the image attachment | Tests only (no socket client in the run) |
| Join line not repeated for the same agent, generic wording, owner-paused skip; upload switch off; 10-per-hour image cap; request from an earlier conversation ignored | Tests only |
| Owner-paused image, agent-assigned (not escalated) image, expiry of the 7-day window, sidecar media 404/outage, over-size (10 MB) images, image send 400/413/415 refusals, tenant isolation, plan-limit | Tests only |
| **Real Baileys sidecar (media endpoint, image send, real `kind:"image"` events), real OpenAI moderation, real Cloudinary, real vision answer, real pairing code format, real phone** | **NOT verified** |
