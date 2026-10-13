# FE integration guide — Screen recording (video attachments) + 30-day retention

Backend: PR #122 (`feat/screen-recording-be`, merged to `develop` at `8db4af1`) plus the one-line `media_metadata` fix described in section 11. Spec: `docs/superpowers/specs/2026-10-12-screen-recording-design.md`. SOPs: `docs/sop/screen_recording_backend.md`, `docs/sop/attachment_retention.md`. Image contract (also changed by this feature): `docs/fe-integration-guide-chat-image-upload.md`.

**Provenance.** Every JSON body below is pasted from `docs/fe-integration-captures/screen-recording/` (NN-name.json, numbered as cited). They were captured on 2026-10-12 against the real app run **locally** (uvicorn on 127.0.0.1, Postgres 15, Redis 7, migrations at `sr10screenrec`), uploading **real** clips to a dev Cloudinary cloud (public demo clip `dog.mp4`, 13.4 s; looped demo clip, 80.5 s) and running the real `expire_attachments` job. It was not staging. Placeholders: `<cloud-name-placeholder>`, `<signature-placeholder>`, `<timestamp-placeholder>`, `<cloudinary-api-key-placeholder>`, `<widget-api-key-placeholder>`, `<admin-jwt-placeholder>`, `<agent-jwt-placeholder>`. Tenant and customer data is fake (`Acme Test Co`, `customer@example.test`). Local env differences: `AGENT_ALERT_ENABLED=false` (no ARQ worker, nobody is rung), `ENVIRONMENT=development` (so the sweep tag is `screen_recording_development`), a dummy OpenAI key (video turns never call the AI), no real browser. Anything not exercised live is marked inline and listed in section 12.

## 1. What the feature is (FE terms)

A customer can attach a **short screen recording** (max 60 s) to a chat message, either recorded in the browser (desktop, `getDisplayMedia`) or picked as a video file (any device, max 50 MB). To the customer it is "send a recording". To the system it is **"talk to a human"**: sending a video escalates the conversation, creates/reuses the ticket, rings agents, and the AI never sees the video. The customer gets a fixed assistant acknowledgement instead of an AI reply.

Also shipped in the same change: **every chat attachment (images and videos) is deleted 30 days after upload.** The message stays in the transcript; the attachment becomes an "expired" placeholder with no URL.

Surfaces that read these shapes: widget / hosted support page / authed customer chat (send), dashboard and Flutter agent app (play back, show expired tiles).

## 2. The client flow (3 calls, then a normal send)

1. `POST /chat/video/sign {session_id}` -> signed upload payload (`upload_url`, `fields`).
2. **Browser -> Cloudinary directly:** `multipart/form-data` POST to `upload_url` containing **every key in `fields` exactly as returned**, plus the file as the **last** part named `file`. Do not send an `Authorization` header and do not add or edit fields (the signature covers them). Nothing goes through our API or nginx.
3. `POST /chat/video/register {session_id, public_id}` where `public_id` is the **`public_id` from Cloudinary's upload response** (it already includes the folder, i.e. `<folder>/<hex>`; do not rebuild it from `fields`). -> `attachment_id`, `url`, `poster_url`, `duration_ms`.
4. `POST /chat/message` with `attachment_ids: [attachment_id]` (existing endpoint, same as images).

All three new calls are **public** (no auth; the session is identified by `session_id`, like `/chat/upload`). The gate is per tenant (section 3). Limits are in section 7.

## 3. Gating: should the button show?

`allow_screen_recording` is `true` only when the platform grant `can_use_screen_recording` is on **and** the tenant toggle `widget_allow_screen_recording` is on (fails closed). It is independent of `allow_file_upload`.

Public widget config, tenant enabled (capture `01-widget-public-config-enabled.json`): `GET /widget/public/config/{tenant_id}`, header `X-API-Key: <widget-api-key-placeholder>` -> **200**
```json
{
  "tenant_id": "86730f5c-e040-46f7-864b-2f9c8e7fd2bd",
  "company_name": "Acme Test Co",
  "company_description": null,
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": null,
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": true,
  "allow_screen_recording": true,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "first_responder": "ai"
}
```
After the admin turned the toggle off (`04-widget-public-config-disabled.json`): identical body with `"allow_screen_recording": false`.

Other surfaces expose the same `allow_screen_recording` flag: visitor bootstrap, `GET /customer/conversations/{session_id}`, and the public business profile. **These were not captured live** (resolved-from-code: `app/services/visitor_service.py:100,441`, `app/api/v1/endpoints/customer_conversations.py:181`); the field is a top-level boolean next to `allow_file_upload`. On the visitor/profile schemas the default is `false` (`app/schemas/visitor.py:38`, `app/schemas/customer_conversation.py:39`), so a surface that omits widget config reports no recording; gate private profiles with `is_private`.

### 3a. Tenant admin (dashboard Settings -> Widget)

`GET /widget/config` (`02-admin-widget-config-get.json`) -> **200**
```json
{
  "tenant_id": "86730f5c-e040-46f7-864b-2f9c8e7fd2bd",
  "company_name": "Acme Test Co",
  "widget_primary_color": "#2563eb",
  "widget_accent_color": "#10b981",
  "widget_logo_url": null,
  "widget_greeting_message": "Hi! How can we help you today?",
  "widget_position": "bottom-right",
  "widget_show_unread_badge": true,
  "widget_allow_file_upload": true,
  "widget_allow_screen_recording": true,
  "can_use_screen_recording": true
}
```
`PATCH /widget/config` body `{"widget_allow_screen_recording": false}` (`03-admin-widget-config-patch-off.json`) -> **200**, same shape with `"widget_allow_screen_recording": false` and `"can_use_screen_recording": true` (the read-only plan grant). `06-admin-widget-config-patch-on.json` turns it back on.

> **Trap:** two different names. Admin config uses `widget_allow_screen_recording` (editable) and `can_use_screen_recording` (read-only, platform grant). Public/customer surfaces use the single computed `allow_screen_recording`. Disable the dashboard toggle with "Not included in your plan." when `can_use_screen_recording` is `false` (not live-captured with `false`; only the `true` value was exercised).

## 4. Endpoints

Error envelope for all new video errors: `{"detail": {"code": "<code>", "message": "<text>"}}`. **Exception:** a 404 for an unknown session is the plain string form `{"detail": "Session not found"}`.

### 4.1 `POST /chat/video/sign`

Request `{"session_id": "<uuid>"}`. **200** (`08-sign-200.json`):
```json
{
  "upload_url": "https://api.cloudinary.com/v1_1/<cloud-name-placeholder>/video/upload",
  "fields": {
    "timestamp": "<timestamp-placeholder>",
    "folder": "luran/chat_images/a0ad1abd-2332-4052-98ad-334a66c8f6ee/27f7b867-eff1-434e-87e5-34c73ff34e75/video",
    "public_id": "9fcb1f3016c8499b8fc3915eb941d8ad",
    "eager": "c_limit,q_auto,vc_h264,w_1280/mp4",
    "eager_async": "true",
    "overwrite": "false",
    "tags": "chat,screen_recording,screen_recording_development,a0ad1abd-2332-4052-98ad-334a66c8f6ee",
    "api_key": "<cloudinary-api-key-placeholder>",
    "signature": "<signature-placeholder>"
  },
  "max_bytes": 52428800,
  "max_duration_s": 60
}
```
(`timestamp`, `api_key`, `signature` are real values at runtime. `tags` and `folder` differ per environment and tenant; forward them untouched.)

| Status | Body | When |
|---|---|---|
| 403 | `{"detail":{"code":"screen_recording_disabled","message":"Screen recording is not enabled"}}` (`05-sign-403-toggle-off.json`) | gate off. Hide the recording button for the rest of the session. |
| 404 | `{"detail":"Session not found"}` (`07-sign-404-unknown-session.json`) | unknown `session_id` |
| 429 | `{"detail":{"code":"video_session_limit","message":"Recording limit reached for this conversation"}}` (`21-sign-429-session-limit.json`) | 3 registered videos already in this session |
| 429 | IP rate limit, 5 sign calls/hour/IP. **Body not captured** (resolved-from-code: `app/utils/http_rate_limiter.py`, bucket `http_video`); handle any 429 the same way. | |
| 503 | `video_unavailable` | Cloudinary not configured (not live: resolved-from-code) |

### 4.2 Direct upload to Cloudinary (not our API)

`POST <upload_url>` multipart: all `fields` + `file`. Real response, trimmed (`09-cloudinary-upload-response.json`), **200**:
```json
{
  "public_id": "luran/chat_images/3c8489af-9907-4a75-9fde-9daa1d6c75eb/fd799e50-65af-4c3d-a7d1-3c03664919d2/video/98a2e28577724c9e885709e061882265",
  "version": 1791839030,
  "resource_type": "video",
  "type": "upload",
  "format": "mp4",
  "duration": 13.4134,
  "bytes": 9094354,
  "width": 854,
  "height": 480,
  "eager": [{"status": "processing", "...": "..."}],
  "tags": ["chat", "screen_recording", "screen_recording_development", "3c8489af-9907-4a75-9fde-9daa1d6c75eb"]
}
```
(See the capture for full `eager`/`secure_url`.) Take `public_id` from here. Note `eager[0].status` is `processing`: the transcoded MP4 is generated asynchronously (section 9 shows it was already servable). Cloudinary-side errors (network, 4xx) are not our API: show "Upload failed." with Retry and keep the clip.

### 4.3 `POST /chat/video/register`

Request `{"session_id", "public_id"}`. **200** (`10-register-200.json`):
```json
{
  "attachment_id": "488328b4-5c2e-43db-bf78-e22e5fabf0a4",
  "url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,q_auto,vc_h264,w_1280/v1791839243/luran/chat_images/86cc1ec8-1adc-4c7c-804e-53431ff3cf2a/c9da101c-6e9f-4e32-9f1f-022a69a4ce5c/video/fab0fc676fd144f2a41ae491236d30ce.mp4",
  "content_type": "video/mp4",
  "width": 854,
  "height": 480,
  "kind": "video",
  "duration_ms": 13413,
  "poster_url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,so_0,w_640/v1791839243/luran/chat_images/86cc1ec8-1adc-4c7c-804e-53431ff3cf2a/c9da101c-6e9f-4e32-9f1f-022a69a4ce5c/video/fab0fc676fd144f2a41ae491236d30ce.jpg",
  "bytes": 9094354
}
```
`content_type` is always `video/mp4` (the delivered derivative), whatever the source format. The register call is idempotent only through the 409 below. Stage `attachment_id` (+ `poster_url`, `duration_ms`) in your local list; the 200 body has **no `id` and no `expired`** (it is the upload shape).

| Status | Body | Meaning / FE action |
|---|---|---|
| 409 | `{"detail":{"code":"already_registered","message":"Already registered","attachment_id":"488328b4-..."}}` (`11-register-409-repeat.json`) | A retry. **Treat as success** using the returned `attachment_id`. (Note: `attachment_id` sits **inside `detail`**.) |
| 422 | `{"detail":{"code":"video_invalid","message":"We couldn't process that video"}}` (`12-register-422-foreign-folder.json`) | `public_id` not in this session's folder (nothing is looked up or destroyed) |
| 422 | same body (`13-register-422-not-found-asset.json`) | right folder shape but no such asset at Cloudinary (upload never finished) |
| 422 | `{"detail":{"code":"video_too_long","message":"Videos can be up to 60 seconds"}}` (`15-register-422-too-long.json`, an 80.5 s clip, upload `14-...`) | over 60 s (+1 s slack). The asset is destroyed server-side. |
| 422 | `video_too_large` / `"Videos can be up to 50 MB"` | over 50 MB. **Not captured live** (resolved-from-code: `chat_video.py`, `video_too_large`); same envelope. |
| 422 | `video_invalid` | also for a non-video resource or a format outside `webm`/`mp4`/`mov` (resolved-from-code) |
| 429 | `video_session_limit` | the 3-per-session cap re-checked at register (resolved-from-code; the sign form was captured) |
| 503 | `video_unavailable` | transient Cloudinary lookup failure, nothing destroyed; safe to retry (resolved-from-code) |

### 4.4 Send: `POST /chat/message`

Same endpoint as images; `attachment_ids` may include one video. **Validation first:** a bad id is `400 "Invalid or already-used attachment"` (existing), more than one video is the 422 below.

**A. Normal send, not yet escalated** (`16-chat-message-video-ack.json`), **200**:
```json
{
  "session_id": "c9da101c-6e9f-4e32-9f1f-022a69a4ce5c",
  "message": {
    "role": "assistant",
    "content": "Thanks — a member of our team will watch your recording and reply here.",
    "id": "cbf4f4bc-c1f8-41e2-996e-26522dbc91ab",
    "session_id": "c9da101c-6e9f-4e32-9f1f-022a69a4ce5c",
    "retrieved_chunk_ids": null,
    "token_count": null,
    "attachments": [],
    "meta": {"system_notice": "screen_recording_ack"},
    "created_at": "2026-10-12T21:07:44.217403Z"
  },
  "user_message_attachments": [
    {
      "id": "488328b4-5c2e-43db-bf78-e22e5fabf0a4",
      "url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,q_auto,vc_h264,w_1280/v1791839243/.../video/fab0fc676fd144f2a41ae491236d30ce.mp4",
      "content_type": "video/mp4",
      "width": 854,
      "height": 480,
      "kind": "video",
      "duration_ms": 13413,
      "poster_url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,so_0,w_640/v1791839243/.../video/fab0fc676fd144f2a41ae491236d30ce.jpg",
      "expired": false
    }
  ],
  "should_escalate": true,
  "escalation_reason": "Customer sent a screen recording",
  "ticket_id": "8819e6ba-6d3d-43e9-b878-da61dcafea3c"
}
```
`message` is the **ACK**: an assistant message with `meta.system_notice == "screen_recording_ack"`. It is fixed text, not an AI generation. Render it as a normal assistant bubble. The customer's own video is on `user_message_attachments` (same two-shape contract as images). If the customer sent no text, `content` of their stored user message is the placeholder `[screen recording]`.

**B. Already-escalated session** (`20-chat-message-video-already-escalated.json`), **200**: no ACK, no new ticket. `message` **echoes the customer's own message** (`role: "user"`), `should_escalate: false`, `ticket_id: null`:
```json
{
  "session_id": "c9da101c-6e9f-4e32-9f1f-022a69a4ce5c",
  "message": {"role": "user", "content": "Another recording", "id": "48e91de2-cb6b-4501-bc91-95d34678c0f4", "attachments": [], "meta": {}, "...": "..."},
  "user_message_attachments": [{"id": "127037de-cee2-4010-a73e-0c4a79398857", "kind": "video", "duration_ms": 13413, "expired": false, "...": "..."}],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```
> **Trap:** here `message.attachments` is `[]` even though the message has a video. Render the customer's video from `user_message_attachments`, never from `message.attachments`. Drive "waiting for a human" UI from session status, not from `should_escalate`. (A re-ring happens only if the ticket is MISSED; not exercised live.)

**C. Tenant requires email before escalation** (`24-chat-message-video-contact-prompt.json`; the tenant was set with `23-admin-ai-config-require-email.json`), **200**: the video message is saved and linked, and the response is the contact-capture prompt instead of the ACK:
```json
{
  "session_id": "d026e575-4045-48c5-94c9-bf5744f3a5d5",
  "message": {
    "role": "assistant",
    "content": "Before I connect you with a support agent, please share the best email address for follow-up.",
    "id": "4abb9c2a-ba46-42d7-bfc4-5819229be0ea",
    "attachments": [],
    "meta": {"contact_capture_required": true, "contact_capture_fields": ["email"]},
    "...": "..."
  },
  "user_message_attachments": [{"id": "23039eb6-537b-4a7c-b050-2eed07ddeb7f", "kind": "video", "duration_ms": 13413, "expired": false, "...": "..."}],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```
Do not discard the staged video or show a failure: it is already sent. Show the prompt; escalation happens when the customer replies with the email (the reply step itself was **not** captured for video; it is the existing contact-capture mechanism, see `docs/fe-integration-guide-who-answers-first.md` 4.4).

**D. Two videos in one message** (`22-chat-message-two-videos-422.json`), **422**:
```json
{"detail": {"code": "too_many_videos", "message": "One recording per message"}}
```
Nothing is persisted or linked; both videos stay staged and sendable.

## 5. What agents and other clients receive

### 5.1 Agent WebSocket `customer_message` frame (live)

> **UPDATE 2026-10-13 (staff credential hygiene, not re-captured live):** agent-facing `customer_message` frames (live, replay, image fanout) no longer carry `user_identifier`; they carry `customer_label` (string). The capture below predates this. Not re-captured live.

Agent socket: `ws://<host>/api/v1/ws/agent/{session_id}?token=<agent-jwt-placeholder>` (the dashboard may use the `access_token` cookie instead, Origin-checked; the Flutter app uses `?token=`). Captured with a real agent login connected **before** the customer sent (`25-ws-agent-frames.json`):
```json
{
  "type": "customer_message",
  "message_id": "398e7f60-cf45-4398-a93d-834198410c55",
  "content": "Here is a recording of the problem",
  "user_identifier": "customer@example.test",
  "timestamp": "2026-10-12T21:10:02.926725+00:00",
  "attachments": [
    {
      "id": "6dfcb19f-a79a-4155-ad01-104009d5868d",
      "url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,q_auto,vc_h264,w_1280/v1791839383/.../video/540a7bce2a0746ac9ee5384a1b19bfe7.mp4",
      "content_type": "video/mp4",
      "width": 854,
      "height": 480,
      "kind": "video",
      "duration_ms": 13413,
      "poster_url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,so_0,w_640/v1791839383/.../video/540a7bce2a0746ac9ee5384a1b19bfe7.jpg",
      "expired": false
    }
  ]
}
```
Only the `customer_message` frame arrived for each send (2 frames across the two sends); the ACK is **not** broadcast as a frame (the agent already sees the ticket). The same `attachments` shape (from `attachment_frames`) is used in the on-connect replay and every other frame carrying a `message_id`. The ring/push to agents for the escalation was **not exercised** (no ARQ worker, no devices); its reason text is `Customer sent a screen recording`.

### 5.2 REST message list (agent/admin)

`GET /chat/sessions/{session_id}/messages` (admin auth; `17-session-messages-admin-with-video.json`) -> **200**, array of messages; the customer message carries:
```json
"attachments": [
  {
    "id": "6dfcb19f-a79a-4155-ad01-104009d5868d",
    "url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,q_auto,vc_h264,w_1280/v1791839383/.../video/540a7bce2a0746ac9ee5384a1b19bfe7.mp4",
    "content_type": "video/mp4",
    "width": 854,
    "height": 480,
    "kind": "video",
    "duration_ms": 13413,
    "poster_url": "https://res.cloudinary.com/<cloud-name-placeholder>/video/upload/c_limit,so_0,w_640/v1791839383/.../video/540a7bce2a0746ac9ee5384a1b19bfe7.jpg",
    "expired": false
  }
]
```
followed by the ACK message (`role: "assistant"`, `meta.system_notice: "screen_recording_ack"`, `attachments: []`).

Transcript / History / Missed (`TranscriptMessage.attachments`) use the same `AttachmentOut` shape: **resolved-from-code** (`app/schemas/conversation.py` `AttachmentOut`, `conversation_service.attachment_frames`), not captured.

## 6. Expired attachments (30-day retention)

Thirty days after upload a daily job destroys the Cloudinary asset. The row stays. Captured after setting one row's `expires_at` 8 days into the past (older than the 7-day active-ticket ceiling) and running the real `expire_attachments` (`26-session-messages-admin-after-expiry.json`), same message as 5.2:
```json
"attachments": [
  {
    "id": "6dfcb19f-a79a-4155-ad01-104009d5868d",
    "url": null,
    "content_type": "video/mp4",
    "width": 854,
    "height": 480,
    "kind": "video",
    "duration_ms": 13413,
    "poster_url": null,
    "expired": true
  }
]
```
The other (non-expired) video in the same capture still has `expired: false` and live URLs. Images expire identically (`kind: "image"`, `url: null`; the image case was **not** captured live, resolved-from-code via the same `AttachmentOut`).

> **Traps:** (1) `url` and `poster_url` are `null` when `expired` is `true`, and `url` is **nullable on every attachment type now**. Null-check before `<img src>`/`<video src>`. (2) `width`/`height`/`duration_ms`/`content_type` survive expiry; use them for the placeholder. (3) A WS frame does not tell you an existing message expired; refetch history to see it.

Placeholder copy (spec): `Screen recording . 0:42 . expired` (format `duration_ms` as m:ss) and `Image . expired`.

## 7. Limits

| Limit | Value | Enforced by |
|---|---|---|
| Duration | 60 s (BE tolerance +1 s) | register (`video_too_long`) |
| Recorder auto-stop | 60 s **or** 15 MB, whichever first; keep the clip | FE |
| Uploaded file | 50 MB (`max_bytes: 52428800`) | FE pre-check + register (`video_too_large`) |
| Videos per message | 1 | send (`too_many_videos`) |
| Attachments per message | 3 total (images + video) | existing 400/422 |
| Videos per session | 3 registered | sign and register (`video_session_limit`) |
| Sign calls | 5 / hour / IP | 429 |
| Register calls | 20 / hour / IP | 429 |
| Formats accepted | webm, mp4, mov (source) | register (`video_invalid`) |

## 8. Customer error copy (spec 5.6)

| Case | Behaviour |
|---|---|
| Picker cancelled / permission denied | silent reset |
| sign 403 `screen_recording_disabled` | hide the recording button for the session |
| sign 429 | "You've sent the maximum number of recordings for now." |
| Cloudinary upload failure / network | "Upload failed." + Retry (clip kept) |
| register 422 `video_too_long` / `video_too_large` | "Videos can be up to 60 seconds and 50 MB. Trim it in your Photos app and try again." |
| register 422 `video_invalid` | "We couldn't process that video. Try recording again." |
| register 409 `already_registered` | success, use `detail.attachment_id` |
| send 422 `too_many_videos` | "One recording per message." |
| send failure | keep the draft |

## 9. Delivery: is the MP4 playable right away?

Probe of the registered `url` (`18-delivery-mp4-probe.json`) and `poster_url` (`19-delivery-poster-probe.json`), status and headers only, two attempts 3 s apart, requested right after the first send (seconds after upload, while the eager job reported `processing`):

| URL | Attempt 1 | Attempt 2 |
|---|---|---|
| `url` (eager derivative) | **200**, `content-type: video/mp4;codecs=avc1`, `content-length: 927576` | 200, same |
| `poster_url` | **200**, `content-type: image/jpeg`, `content-length: 16737` | 200, same |

The first request returned 200 (not 423/processing), and the derivative (927 KB) is much smaller than the 9.1 MB original, so the H.264 `w_1280` transcode was served. Still, for safety render `<video controls preload="metadata" poster=...>` and handle a `<video>` `error` event with a retry after a few seconds: a 423 on a very large clip is possible (**not observed**; only a 13 s clip was tried).

## 10. UX consequences the FE must surface

- **Sending a recording escalates the chat to a human.** Say so before sending (the consent sheet) and show the ACK; do not expect an AI answer. If the tenant requires an email, the customer sees the contact prompt instead (4.4 C).
- **Consent copy** (once per session): "You'll choose a tab, window or screen to share. Anything visible will be recorded, so close passwords or private info first. No audio. Max 60 seconds. **Kept for 30 days** and seen only by {company_name}'s support team."
- **Expired items** render a placeholder, never a broken player (section 6). Tell tenants and agents that images are now also deleted at 30 days (dashboard note beside each toggle: "Recordings/Images are automatically deleted 30 days after upload.").
- **Do not render by `content_type`.** Branch on `kind` (`"image"` | `"video"`); skip unknown kinds (forward compatibility). `content_type` is `video/mp4` for every video, even a `.mov` or `.webm` source.
- **Old clients.** Dashboard and Flutter players must reach prod before the record button does; an old client would show the MP4 as a broken image.
- **Browser support** (from the spike; only Chromium was verified live, see section 12):

| Browser | `getDisplayMedia` recording | Output | Status |
|---|---|---|---|
| Chrome / Edge | yes | webm vp9/vp8, also mp4 | verified in spike (Chromium 152) |
| Firefox | yes | webm | unverified |
| Safari 17+ macOS | yes | mp4 only (no webm) | unverified |
| iOS / Android browsers | no | n/a | hide Record, offer Upload + "how to record your screen" tip |

  Feature-detect `navigator.mediaDevices?.getDisplayMedia`, pick the first `isTypeSupported` MIME of vp9 webm, vp8 webm, mp4. A host page that sends `Permissions-Policy: display-capture=()` blocks recording inside the widget iframe: hide Record for the session, keep Upload.
- **Rate-limited customers** hit 429 on sign after 5 signs/hour from one IP; this affects offices behind one NAT.
- **Dev note:** the login endpoint rejects `.test` email addresses with 422 (reserved TLD); use `@example.com` for staff test users. Customer identifiers like `customer@example.test` are accepted by chat.

## 11. Found while capturing (backend bug, fixed)

The first live `register` returned `422 video_too_long` for a 13 s clip: Cloudinary's Admin API `resource()` omits `duration` unless called with `media_metadata=True`, so the BE read duration 0 and rejected every real video. The unit tests mocked the Admin API and could not catch it. Fix: `get_video_resource` passes `media_metadata=True` (`app/utils/cloudinary_client.py`), plus a regression test (`tests/utils/test_cloudinary_video.py::test_get_video_resource_requests_media_metadata`). All captures above were taken with the fix applied. **Any environment running PR #122 without this fix rejects all recordings.**

## 12. Verification table

| Behaviour | Status | Evidence |
|---|---|---|
| Public widget config `allow_screen_recording` true / false | verified-live (local) | 01, 04 |
| Admin GET/PATCH `widget_allow_screen_recording`, `can_use_screen_recording` | verified-live (local) | 02, 03, 06 |
| `can_use_screen_recording: false` rendering | resolved-from-code | not toggled live |
| Visitor bootstrap / customer conversation / business profile flag | resolved-from-code | `visitor_service.py`, `customer_conversations.py` |
| sign 200 / 403 / 404 | verified-live (local) | 08, 05, 07 |
| sign 429 `video_session_limit` | verified-live (local) | 21 |
| sign 429 IP limiter body, 503 | resolved-from-code | `http_rate_limiter.py`, `chat_video.py` |
| Real Cloudinary signed upload (remote URL) | verified-live (local) | 09, 14 |
| register 200 / 409 / 422 invalid (x2) / 422 too long | verified-live (local) | 10, 11, 12, 13, 15 |
| register 422 `video_too_large`, 429, 503 | resolved-from-code | `chat_video.py` |
| Send with video: ACK + `user_message_attachments[0].kind == "video"` + ticket | verified-live (local) | 16 |
| Already-escalated: echo, no ACK | verified-live (local) | 20 |
| Contact-capture prompt with video | verified-live (local) | 24 (reply step not captured) |
| `too_many_videos` 422 | verified-live (local) | 22 |
| MISSED ticket re-ring on a video | resolved-from-code | unit tests |
| Agent WS `customer_message` frame with video | verified-live (local) | 25 |
| Agent ring / push for a video | NOT LIVE | `AGENT_ALERT_ENABLED=false`, no devices |
| REST message list with video, then expired | verified-live (local) | 17, 26 |
| Expiry job (`expire_attachments` destroyed asset, row `EXPIRED`) | verified-live (local) | 26 (job returned 1 expired) |
| Active-ticket deferral (7-day guard) | resolved-from-code | unit tests (this capture used the past-ceiling case) |
| `sweep_unregistered_videos` | ran, 0 destroyed | needs assets older than 24 h; unit tests only |
| Delivered MP4 + poster served | verified-live (local) | 18, 19 (200 on first request) |
| Transcript / History / Missed attachment shape | resolved-from-code | `AttachmentOut` |
| Image expiry shape | resolved-from-code | `AttachmentOut` |
| Real `getDisplayMedia` picker inside the embedded widget iframe | PENDING-HUMAN | needs a person and a host page |
| iPhone screen-recording `.mov` upload (HEVC transcode) | PENDING-HUMAN | needs a device |
| Firefox / Safari desktop recording | PENDING-HUMAN | unverified |
| Dashboard / Flutter playback | PENDING-HUMAN | P3 |


## Customer credential on write paths (2026-10-14)

Posting into an existing conversation now requires the customer credential, not just `session_id`. Captured against a running app (`docs/captures/customer-credential-hardening/`, files `01`-`11`).

| Path | Rule today |
|---|---|
| `POST /chat/message` with `session_id` | **Required.** Send `user_identifier` (what luran-chat already sends), or `metadata.visitor_token` of the session's visitor. Anything else is `403`. |
| `POST /chat/upload` (form) | Send `user_identifier` as an extra form field. **Optional for now**: a wrong value is `403`; a missing one is still accepted. |
| `POST /chat/video/sign`, `POST /chat/video/register` (JSON) | Same: optional `user_identifier` in the body; wrong is `403`, missing still accepted. |

**Action for luran-chat:** add `user_identifier` (the same value `useChat` sends on `/chat/message`) to `uploadChatImage`, `signVideo` and `registerVideo`. Authed customers (cookie/Bearer customer token that owns the session) are also accepted without it. Backend will make it mandatory (`CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL=true`) only after that ships; until then missing is accepted.

Rejection, identical on all three paths (verified live):
```
HTTP 403  {"detail": "Invalid customer credential"}
HTTP 403  {"detail": "Invalid customer credential"}
HTTP 403  {"detail": "Invalid customer credential"}
```
The body never echoes the credential. luran-chat's upload error mapper turns any `403` into the "upload is turned off" copy; a real credential mismatch should be impossible for a correct client, so no new copy is needed, but a `403` with `detail == "Invalid customer credential"` on `/chat/message` should be treated as "session no longer yours, start a new conversation".

`metadata.customer_user_id` is ignored from public callers (`/chat/message`, `POST /chat/sessions`); the customer binding only comes from the authenticated `POST /customer/conversations/message` (verified live, `05`, `06`, `14`, `15`).

| Claim | Verified |
|---|---|
| 403 body on `/chat/message`, `/chat/upload`, `/chat/video/sign` mismatch | live (`01`-`03`, `07`, `10`) |
| Legit `user_identifier` still 200 on `/chat/message`; media paths with only `session_id` still pass the gate | live (`04`, `08`, `09`, `11`) |
| `metadata.visitor_token` accepted as the credential; owning-customer token accepted on media paths when enforced; `CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL=true` behaviour | pytest only, not exercised live |
