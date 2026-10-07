# FE Integration Guide — Chat Image Upload

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

_Staging captures used a test tenant/session; the UUIDs/URLs above are real staging artifacts (not customer data), kept verbatim as the live shapes._
