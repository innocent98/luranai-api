# FE Integration Guide — Chat Image Upload

> **UPDATE 2026-10-12 (screen recording + 30-day retention, PR #122).** The attachment object is no longer `{id,url,content_type,width,height}`. Everywhere an attachment appears (`user_message_attachments`, `message.attachments`, REST history, WS frames, transcripts) it is now `{id, url, content_type, width, height, kind, duration_ms, poster_url, expired}`, and **`url` is nullable**. Where this guide below shows the 5-field form, read it as the 9-field form in section 9. Live captures: `docs/fe-integration-captures/screen-recording/` and `docs/fe-integration-guide-screen-recording.md`.

**Audience:** the widget (`luran-chat`), dashboard (`luran-dashboard`), and agent app (`luran_agent`) teams.
**Backend:** merged to `develop`, deployed to staging. **Every payload below marked `LIVE` was captured against staging** (`https://staging-api.luranai.com`, 2026-10-07) — not derived from schemas. Items marked `PENDING-AUTHED` or `SCHEMA` could not be exercised over the public flow and are labelled so you know how much to trust them.

Base path: `/api/v1`. Customer endpoints are **public** (no bearer; the widget's normal session context). Agent endpoints are **bearer-authed** (tenant-admin / support-agent).

---

## 1. When it triggers
A customer attaches image(s) in the widget; `gpt-6-luna` interprets them and replies in the same turn. On human takeover, agents see customer images and can send images back. Limits: **≤10 MB/image, ≤3 images/message**, formats **JPEG/PNG/WebP/non-animated GIF** (HEIC **not** supported in V1 — set the file input `accept="image/jpeg,image/png,image/webp"` so iOS converts HEIC→JPEG).

## 2. Two-step flow
1. **Upload** each file → get an `attachment_id`.
2. **Send** the message with `attachment_ids[]` (text optional when ≥1 attachment).

---

## 3. `POST /api/v1/chat/upload` — customer upload (public, multipart) `LIVE`

Request: `multipart/form-data` with `file` (the image) + `session_id` (form field).

**200 (LIVE):**
```json
{
  "attachment_id": "6aecdb36-e403-4828-be52-ba72223a0608",
  "url": "https://res.cloudinary.com/.../luran/chat_images/<tenant>/<session>/3879976c...jpg",
  "content_type": "image/png",
  "width": 480,
  "height": 300,
  "bytes": 3411
}
```
> ⚠️ **TRAP (confirmed live):** `url` ends in **`.jpg`** (Cloudinary normalises every upload to JPEG) while `content_type` echoes the **client's** type (`image/png`). **Render from `url`; never infer the stored format from `content_type`.** `width/height` are post-processing (capped at 2000px).

**Errors:**
- `422` missing fields **(LIVE)** — `POST` without body: `{"detail":[{"type":"missing","loc":["body","session_id"],"msg":"Field required"},{"type":"missing","loc":["body","file"],"msg":"Field required"}]}`
- `403` **(SCHEMA)** — tenant has `widget_allow_file_upload` off. FE should **hide** the attach button when off (see §7).
- `413` too large (>10 MB) · `415` unsupported/invalid image **(SCHEMA)**.
- `422` moderation reject **(SCHEMA)** — `{"detail":{"code":"image_rejected","reason":"<short friendly msg>"}}`. Show `reason`; do not leak more.
- `429` rate-limited (IP-keyed) **(SCHEMA)**.

## 4. `POST /api/v1/chat/message` — send with image(s) (public) `LIVE`

Request: `{"session_id","message"?,"attachment_ids":[...]}` — `message` **optional** when `attachment_ids` present (image-only allowed, LIVE-verified). Max 3 ids.

**200 (LIVE, text+image):**
```json
{
  "session_id": "8821fac5-...",
  "message": {
    "role": "assistant",
    "content": "The image shows a large red circle beside the text “ORDER #A-42 STATUS: CRACKED”, inside a rectangular border.",
    "id": "f59be41a-...",
    "attachments": [],
    "token_count": 863,
    "meta": { "escalation_action": "no_escalation", "token_breakdown": {"total_tokens": 863}, ... },
    "created_at": "2026-10-07T17:19:18Z"
  },
  "user_message_attachments": [
    { "id": "6aecdb36-...", "url": "https://res.cloudinary.com/....jpg", "content_type": "image/png", "width": 480, "height": 300 }
  ],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```
> ⚠️ **TWO-SHAPE CONTRACT (confirmed live):** `message` is the **assistant's reply** — its `attachments` is `[]`. The **customer's own** images come back on the top-level **`user_message_attachments`**. Echo the user's images from `user_message_attachments`, not from `message.attachments`.
>
> Field-nesting: upload returns **`attachment_id`**; embedded attachment objects use **`id`**.

**image-only (LIVE):** `{"session_id","attachment_ids":[id]}` (no `message`) → 200, luna still reads the image.

**Errors:** `400` for an unknown / foreign-session / already-linked / >3 `attachment_id`; `422` if both `message` and `attachment_ids` are empty.

## 4b. Sending on an ALREADY-ESCALATED session (no AI reply) `TESTS/CODE`

Once a session is escalated (a human agent is handling it), the bot **must not auto-reply**. A REST `POST /api/v1/chat/message` on such a session now persists the customer's message (text or image, incl. the `_fanout_customer_images` push to the agent for images) and returns **without** any assistant/AI reply.

**200 (escalated, image-only — derived from backend tests, not yet staging-captured):**
```json
{
  "session_id": "8821fac5-...",
  "message": {
    "role": "user",
    "content": "[image]",
    "id": "a1b2c3d4-...",
    "attachments": [],
    "created_at": "2026-10-07T17:40:00Z"
  },
  "user_message_attachments": [
    { "id": "6aecdb36-...", "url": "https://res.cloudinary.com/....jpg", "content_type": "image/png", "width": 480, "height": 300 }
  ],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```
> ⚠️ **THIRD SHAPE — do NOT render `message` as a bot bubble when the session is escalated.** Unlike §4 (where `message.role == "assistant"`), here `message` **echoes the customer's own message** (`role:"user"`, `content` is the text or `"[image]"`). There is deliberately **no AI turn**. The customer's images still arrive on top-level `user_message_attachments` (render from there, as always). Drive your "human mode / no bot" UI off the **session status**, not off this response — `should_escalate:false` here means only "no *new* escalation this turn", not "not escalated".
>
> **Exception:** a send whose message/`button_value` is the **`__talk_to_human__`** control token still goes through the normal escalation path (returns an assistant acknowledgement + may re-ring the agent) even on an escalated session — that is how a customer re-requests a human who missed the first ring. Plain text, images, and welcome/flow triggers are all suppressed; only that one control token is not.

## 5. Agent endpoints (bearer-authed) — takeover

- **Upload:** `POST /api/v1/chat/sessions/{session_id}/upload` (multipart `file`) — **`LIVE`: returns `401` without a bearer** (auth-gated as designed). Happy-path body mirrors §3 with `direction=outbound`. `PENDING-AUTHED` live capture.
- **Send:** `POST /api/v1/chat/sessions/{session_id}/agent-message` now accepts `attachment_ids[]` (≤3; text optional when attachments present). The WS fan-out to the customer + other agents includes `attachments`. `SCHEMA`.
> Note: the spec's §5.3 path `/agent/chat/upload` is **stale** — the real path is `/api/v1/chat/sessions/{id}/upload` (verified in code + live 401).

## 6. History / rendering inbound (both surfaces) `PENDING-AUTHED`
`GET /api/v1/chat/sessions/{id}` and the paginated history return each `MessageResponse` with **`attachments: [{id,url,content_type,width,height}]`** (customer messages `role:"user"`, agent `role:"human_agent"`). This path is **authed** (public flow got `401`), so capture its exact body against an authed session before finalising. The relationship is verified by backend tests; the shape matches `user_message_attachments`.
> **Live rendering — IMPLEMENTED (#106):** both WS directions now carry `attachments`: the `agent_message` frame (agent→customer, confirmed live) AND the `customer_message` frame to agents (`chat.py` `_fanout_customer_images`, fired when a customer HTTP-send has linked attachments). Shape: `attachments: [{id,url,content_type}]`. The exact live WS payload is `PENDING-AUTHED` (capture on an authed/WS session during the dashboard build). Note: HTTP *text-only* sends don't broadcast (customer text goes over WS already); only image sends use this HTTP broadcast.

### 6b. Agent History / Missed — `GET /api/v1/agent/escalations/{id}` `SCHEMA (#106)`
The escalation detail/transcript now returns each `TranscriptMessage` with **`attachments: [{id,url,content_type,width,height}]`** (default `[]`). Live-capture pending an authed agent session.

## 7. Gating
`GET /api/v1/widget/public/config/{tenant_id}` (header `X-API-Key: <public key>`) → `allow_file_upload: bool`. Default **true**. When false, **hide** the attach affordance (and treat a `403` from upload as a hide, defensively).

The **same** `allow_file_upload: bool` flag (default **true**) is now also returned on the two **customer-app** surfaces so the app can gate the attach button without calling the widget config endpoint `(CODE-confirmed; authed live capture still pending)`:
- `GET /api/v1/customer/conversations/{session_id}` (authed) → `CustomerConversationDetail.allow_file_upload` — top-level, alongside the `business_*` fields.
- `GET /api/v1/public/business/{slug}` (public) → `PublicBusinessProfileResponse.allow_file_upload` — top-level. **Private tenants** (`is_private: true`) omit widget config, so this field falls back to its **default `true`**; rely on `is_private` (not this flag) to gate a private profile.

> Field is named `allow_file_upload` on all three surfaces — one consistently-named flag to thread through the FE. Apply the same rule everywhere: `false` ⇒ hide the attach button; treat an upload `403` as a defensive hide.

## 8. Verification table
| Behaviour | Status |
|---|---|
| Attachment object with `kind`/`duration_ms`/`poster_url`/`expired`, nullable `url` (video) | ✅ LIVE (local, 2026-10-12; section 9) |
| Image expiry shape (`url: null`) | ⬜ CODE only |
| Customer upload 200 (url/public_id/dims/bytes) | ✅ LIVE |
| `url` is `.jpg` regardless of `content_type` | ✅ LIVE |
| Upload 422 missing fields | ✅ LIVE |
| Send text+image → luna vision reply | ✅ LIVE |
| Image-only send (no text) | ✅ LIVE |
| `user_message_attachments` vs `message.attachments` two-shape | ✅ LIVE |
| Escalated send → no AI reply, `message` echoes the USER message (§4b) | ✅ TESTS/CODE · live-capture PENDING-AUTHED |
| `__talk_to_human__` on escalated session still re-rings (§4b exception) | ✅ TESTS |
| Agent upload auth-gated (401) | ✅ LIVE |
| Upload 403 (gate off) / 413 / 415 / 422 image_rejected / 429 | ⬜ SCHEMA — capture live |
| Agent upload/send happy-path | ⬜ PENDING-AUTHED |
| History `MessageResponse.attachments[]` body | ⬜ PENDING-AUTHED |
| WS frames carry `attachments[]` (both directions) | ✅ CODE (#106) · live-capture PENDING-AUTHED |
| Escalation `transcript[].attachments` (History/Missed) | ✅ CODE (#106) · live-capture PENDING-AUTHED |
| **R7** `allow_file_upload` on `CustomerConversationDetail` + `PublicBusinessProfileResponse` (reflects tenant flag) | 🟨 CODE — tests assert true/false; authed live capture pending |
| Authed customer send forwards `attachment_ids` → links + `user_message_attachments` | 🟨 CODE — test asserts link; authed live capture pending |

## 9. Attachment object after screen recording (2026-10-12)

Captured live (local app, real Cloudinary dev cloud): `17-session-messages-admin-with-video.json`, `16-chat-message-video-ack.json`, `25-ws-agent-frames.json`, `26-session-messages-admin-after-expiry.json` in `docs/fe-integration-captures/screen-recording/`.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | (upload/register responses call it `attachment_id`; embedded objects use `id`) |
| `url` | string or **null** | **null when `expired` is true.** Null-check before rendering. |
| `content_type` | string | client MIME for images, always `video/mp4` for videos. Never render from it. |
| `width`, `height` | int or null | kept after expiry |
| `kind` | `"image"` or `"video"` | **new.** Drives rendering. Skip unknown kinds. Defaults to `"image"` for every pre-existing attachment. |
| `duration_ms` | int or null | video only; null for images |
| `poster_url` | string or null | video only (JPEG first frame); null for images and when expired |
| `expired` | bool | **new.** true once the 30-day retention job has destroyed the asset |

Video example (live, `17-...`):
```json
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
```
Expired (live, `26-...`): same object with `"url": null`, `"poster_url": null`, `"expired": true`; `kind`, `duration_ms`, `width`, `height`, `content_type` are retained.

- **Images now expire too:** 30 days after upload for every image (existing images: 30 days after the sr10 rollout). An expired image is `kind: "image"`, `url: null`, `expired: true`; show "Image . expired". The image expiry body itself was **not captured live** (resolved-from-code, same serializer).
- **WS history-replay and live frames** now carry the 9-field object (this resolves the earlier "`attachments: [{id,url,content_type}]`" note; live-captured for `customer_message` with a video).
- The image upload response (`/chat/upload`) is unchanged and does not include `kind`; only the video `register` response adds `kind`, `duration_ms`, `poster_url`.

_Staging captures used a test tenant/session; the UUIDs/URLs above are real staging artifacts (not customer data), kept verbatim as the live shapes._


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
