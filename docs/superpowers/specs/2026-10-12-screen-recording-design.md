# Screen recording + attachment retention (design)

**Status:** APPROVED in brainstorm 2026-10-12 (Adebayo), written spec pending review · **Package:** follows N1/N3 (handoff order: screen recording → N4 → presence → N2)
**Repos:** `luran` (BE, feature → `develop`), `luran-widget` (embed loader, → `main`), `luran-chat` (feature → `stage`), `luran-dashboard` (feature → `staging`), `luran_agent` (feature → `main`)
**Related:** spike findings `docs/superpowers/specs/2026-10-10-screen-recording-spike-findings.md` (branch `spike/screen-recording`, local); image-upload design `2026-10-07-chat-image-upload-backend-design.md`; N3 design `2026-10-10-n3-who-answers-first-design.md`; handoffs `docs/handoff/2026-10-08-chat-attachments-and-screen-recording.md` §6 and `2026-10-10-observations-triage.md` (workspace).
**PRD:** `docs/luran_prd.md` has no screen-recording, attachment or retention feature. This is new scope from field feedback, built as an extension of the PRD's human-escalation / support-ticket flow (Escalation Rate and Resolution metrics).

## 1. Problem

Customers describe UI problems in text, which is slow and lossy for agents. Image upload (shipped 2026-10-08) helps, but a bug that only shows across several steps needs a short recording. The AI (`gpt-6-luna`) handles text and images, not video, and video understanding would be costly and unreliable.

Separately, chat images are kept forever today. Screen content can show passwords, balances or third-party data, so both images and recordings need a bounded retention period.

## 2. Goals / success criteria

- A customer on **desktop** can record a tab, window or screen (max 60 s), preview it, and send it in chat.
- A customer on **any device**, including phones, can upload a video file (max 60 s / 50 MB). Mobile users see how to use their phone's built-in screen recorder.
- Any video sent **escalates to a human** through the existing talk-to-human path. **The video never reaches the AI.**
- An agent can play the clip within seconds of the send, on the dashboard and in the Flutter app.
- All chat attachments (images and videos) are **deleted 30 days after upload**. The transcript keeps an "expired" placeholder, and nothing leaks in Cloudinary (unsent, unregistered and purged assets are all cleaned up).

**Non-goals (v1):** agents sending video; audio capture; native in-app screen recording; AI understanding of video; paid Cloudinary video moderation; HLS/adaptive streaming; client-side transcoding; per-tenant retention periods; pre-deletion notifications.

## 3. Decisions (brainstorm 2026-10-12)

| ID | Decision |
|---|---|
| SR-D1 | Surfaces: **all customer surfaces**: embedded widget, hosted support page (`/support/[slug]`), the logged-in floating chat and conversation views, and the logged-in `/messages/[sessionId]` page. `DashConversationView` is moved onto the shared attachment hooks first. |
| SR-D2 | **Desktop:** Record screen + Upload video. **Mobile / no `getDisplayMedia`:** Upload video + a "How to record your screen" sheet (iOS/Android steps by user agent). (Rejected: desktop-only.) |
| SR-D3 | **Gating = plan capability + tenant preference**, mirroring voice: `TenantCapabilities.can_use_screen_recording` (granted on every current plan, `override_flags` honoured) AND `Tenant.widget_allow_screen_recording` (default true). Independent of `widget_allow_file_upload`. |
| SR-D4 | **Sending a video = "talk to a human"**: same contact-capture gate, `escalate_session`, ticket, `_ring_agents`, MISSED fallback. No AI turn; fixed acknowledgement. Already escalated → no re-ring unless the ticket is MISSED. (Rejected: skip contact capture; AI reply first.) |
| SR-D5 | **Retention 30 days from upload for videos AND images.** Asset destroyed, row kept as `EXPIRED`. The dashboard settings show the policy next to each toggle. No per-deletion notifications. |
| SR-D6 | **Existing images:** `expires_at = rollout + 30 d` (30-day grace from launch). (Rejected: new-only; `created_at + 30 d` mass delete.) |
| SR-D7 | **Customer flow:** consent sheet once per session → browser picker → countdown bar with Stop → inline preview (Send / Re-record / Discard) → upload progress → bubble with poster + duration. (Rejected: no preview; consent every time.) |
| SR-D8 | **Upload trust:** sign → direct browser upload to Cloudinary → `register`, where the BE verifies the asset through the Cloudinary Admin API. (Rejected: Cloudinary webhook; proxy through API.) |
| SR-D9 | **Delivery = one progressive MP4** (`f_mp4,vc_h264,w_1280,c_limit,q_auto`, eager async, on-the-fly fallback) + JPEG poster. **No HLS.** Overrides the spike's HLS recommendation. |
| SR-D10 | Limits: **60 s** for all videos (decided 2026-10-10); recorder stops at 60 s **or** 15 MB and keeps the clip; uploaded files ≤ **50 MB**; **1 video per message** (≤ 3 attachments total). |

## 4. Backend (`luran`)

### 4.1 Data model (one Alembic revision)

`message_attachments` (`app/db/models/conversation.py:274`):

| Column | Type | Notes |
|---|---|---|
| `kind` | enum `attachmentkind` (`image`, `video`), NOT NULL, server default `image` | backfilled `image` |
| `duration_ms` | int, nullable | video only |
| `poster_url` | encrypted text, nullable | video only; same encryption as `url` |
| `source_format` | varchar(16), nullable | Cloudinary `format` of the original (`webm`/`mp4`/`mov`) |
| `expires_at` | timestamptz, NOT NULL after backfill | `created_at + ATTACHMENT_RETENTION_DAYS`; backfill = migration time + 30 d |
| `url` | **becomes nullable** | NULL once expired |

- `AttachmentStatus` gains `EXPIRED` and `AttachmentModeration` gains `NOT_APPLICABLE` (Postgres enum `ADD VALUE`; this must run outside a transaction block, as in earlier enum migrations).
- New unique index on `cloudinary_public_id`.
- `width`/`height` are set for video too (from Cloudinary).
- `content_type` stays (it holds the client MIME); rendering is driven by `kind` + `url`, never `content_type`.

Tenant (`app/db/models/tenant.py`):
- `TenantCapabilities.can_use_screen_recording: bool`, default true; seeded true for every plan in the plan-sync map. `subscription_service` respects `override_flags` exactly as for `can_use_voice` (`tenant.py:441`).
- `Tenant.widget_allow_screen_recording: bool`, default true (beside `widget_allow_file_upload`, `tenant.py:157`).
- New `app/services/screen_recording/gating.py::screen_recording_allowed(db, tenant) -> bool` = capability AND preference; **fails closed** (missing row or exception → false), like `services/voice/gating.py:118`.
- `allow_screen_recording` is exposed wherever `allow_file_upload` is today: widget config (`widget.py:73/437`), `visitor_service.py:98/437`, `CustomerConversationDetail` (`customer_conversations.py:179`), `PublicBusinessProfileResponse`. Tenant admin read/update schemas carry `widget_allow_screen_recording` and read-only `can_use_screen_recording`.

Settings (`app/core/config.py`): `ATTACHMENT_RETENTION_DAYS=30`, `ATTACHMENT_RETENTION_ENABLED=true`, `SCREEN_RECORDING_MAX_SECONDS=60`, `SCREEN_RECORDING_MAX_BYTES=52428800`.

### 4.2 Cloudinary client (`app/utils/cloudinary_client.py`)

- `destroy(public_id, *, resource_type)`: **`resource_type` is required** (no default), so no caller can silently fall back to `image`. Treat `result == "not found"` as success at the call sites that need idempotency.
- `sign_video_upload(tenant_id, session_id) -> dict`: ported from the spike. Signed fields: `timestamp`, `folder=<CLOUDINARY_CHAT_FOLDER>/<tenant>/<session>/video`, `public_id=<uuid4 hex>`, `eager=f_mp4,vc_h264,w_1280,c_limit,q_auto` (built with `cloudinary.utils.build_eager`), `eager_async=true`, `tags=chat,screen_recording,<tenant>`. Returns `upload_url`, `fields` (incl. `api_key`, `signature`). The secret never leaves the server.
- `get_video_resource(public_id) -> dict | None`: Admin API `resource(public_id, resource_type="video")`; returns None on not-found.
- `video_delivery_urls(public_id, version) -> (mp4_url, poster_url)`: MP4 = the eager transformation (generated on the fly if the eager job is still running); poster = `so_0,w_640,c_limit,f_jpg`.

### 4.3 Endpoints (`app/api/v1/endpoints/chat.py`, public, same `session_id` model as `/chat/upload`)

**`POST /chat/video/sign`** body `{ "session_id": "<uuid>" }`
- 404 if the session is unknown (same check as `/chat/upload`); 403 `screen_recording_disabled` if the gate is off.
- **Error shape for all new video errors:** `{"detail": {"code": "<code>", "message": "<human text>"}}` (the object form `/chat/upload` already uses for `image_rejected`). The live guide captures the exact bodies.
- Rate limit: new `http_video` bucket, **5 / hour per IP**, plus **3 registered videos per session** (counted from `message_attachments`, `kind=video`) → 429.
- 200 → `{ "upload_url", "fields": {...}, "max_bytes": 52428800, "max_duration_s": 60 }`.

**`POST /chat/video/register`** body `{ "session_id": "<uuid>", "public_id": "<folder/uuid>" }`
1. Gate + session checks as in sign.
2. If a row with this `public_id` exists → **409** `{ "code": "already_registered", "attachment_id": "<id>" }` (idempotent retry).
3. `get_video_resource`; None → 422 `video_invalid`.
4. Verify: `resource_type == "video"`; `public_id` starts with this tenant/session folder; `format in {webm, mp4, mov}`; `duration ≤ 61.0`; `bytes ≤ 52428800`. A failure → `destroy(..., resource_type="video")` and then 422 with `video_too_long` | `video_too_large` | `video_invalid`.
5. Insert `MessageAttachment(kind=video, direction=INBOUND, status=UPLOADED, moderation_status=NOT_APPLICABLE, url=mp4_url, poster_url, duration_ms, width, height, bytes, source_format, content_type="video/mp4", expires_at=now+30d)`.
6. 200 → the **same shape as the `/chat/upload` response** (`attachment_id`, `url`, `content_type`, `width`, `height`, `bytes`) plus `kind: "video"`, `duration_ms`, `poster_url`.

No separate rate limit on register (a signed upload is a prerequisite); the per-session cap applies.

### 4.4 Send path

- Linking is unchanged (`attachment_ids[]`, `get_linkable_attachments`, `link_attachments`). New validation: **at most 1 `kind=video` per message** and ≤ 3 attachments total → 422 `too_many_videos`.
- **AI exclusion:** `_turn_image_urls` (`chat.py`, currently ~:2396 on develop) filters on `kind == image`. Unit test asserts a video URL never reaches `rag_service._user_content`.
- **Escalation:** the inline talk-to-human block (`chat.py` ~:1357–1420 on develop: contact capture → `escalate_session` → `_get_or_create_escalation_ticket` → `_ring_agents` → emails) is pulled out into a helper `_escalate_on_customer_request(..., reason, summary)`. It is called by both `__talk_to_human__` and the new rule "this message links ≥ 1 video":
  - Not escalated: run the helper with reason `"Customer sent a screen recording"`, summary `"Customer sent a screen recording."`, priority HIGH. If contact capture is required, the user message and its video are **persisted first** (linked); then the contact prompt is returned. Escalation happens when contact is supplied (existing pending-contact mechanism).
  - Already escalated: the message is persisted and fanned out (existing early return at ~:1275). Re-ring only if the ticket is MISSED (same rule as the token).
  - N3 `human` mode: the video path runs before the human-first reply, so the customer sees the video acknowledgement rather than the generic human-first reply.
- **Acknowledgement** (new constant in `first_responder.py`): `SCREEN_RECORDING_ACK = "Thanks — a member of our team will watch your recording and reply here."`, stored as an ASSISTANT message with `meta={"system_notice": "screen_recording_ack"}`. It is not an AI generation; the FE renders it like other assistant messages.
- WS frames, REST message lists, the transcript, history and missed views serialize attachments with `kind`, `duration_ms`, `poster_url`, and `expired` (`status == EXPIRED`); `url`/`poster_url` are null when expired.
- Agent alert/push body for a video-triggered escalation: `"New screen recording from {customer}"`.

### 4.5 Retention and cleanup (workers)

**Expiry job** `app/workers/attachment_retention.py::expire_attachments`: daily ARQ cron beside `cleanup_orphan_attachments` (3 AM UTC, `document_processor.py:1303`). No-op when `ATTACHMENT_RETENTION_ENABLED` is false.
- Selects `status = LINKED AND expires_at < now()` in batches of 200, oldest first.
- **Active guard:** skip a row whose session's ticket is `open`/`in_progress`/`missed` **unless** `now() > expires_at + 7 days` (hard ceiling).
- Per row: `destroy(public_id, resource_type=kind)`; on `ok` or `not found` → `status=EXPIRED`, `url=NULL`, `poster_url=NULL`, commit per row. On any other error: log `public_id` + error class only, leave the row for the next run. One bad row never aborts the batch.

**Orphans:**
- `cleanup_orphan_attachments` (`attachment_cleanup.py:23`) also covers `kind=video` and passes `resource_type=kind`.
- New `sweep_unregistered_videos` (same cron): Admin API list of `resource_type=video` with tag `screen_recording`, `created_at` older than 24 h, max 5 pages × 100 per run; destroy any `public_id` with no `message_attachments` row.

**Account purge** (`account_purge_service.py:362-375`): `destroy(row.cloudinary_public_id, resource_type=row.kind)`. Fixes the latent video leak found in the spike.

### 4.6 Moderation

There is no automated moderation for video. The human agent is the reviewer, which is the reason every video escalates (SR-D4). Images keep the existing fail-closed OpenAI moderation. `AttachmentModeration` (today only `PASSED`) gains `NOT_APPLICABLE`, used for video rows.

## 5. Customer surfaces

### 5.1 `luran-widget` (ships first)
- iframe `allow` changes `'microphone; camera'` → `'microphone; camera; display-capture'` in `src/widget-loader.js:520` and `widget-loader.dev.js:487`. Embedded snippets pick this up from the CDN; no customer action is needed. Host pages that send their own `Permissions-Policy: display-capture=()` header override it, which §5.3 handles.

### 5.2 `luran-chat` shared layer
- **Refactor first (its own commit):** `luran/dash/DashConversationView.tsx` drops its local copies and uses the shared `useStagedAttachments` / `StagedAttachments` / upload helpers. Existing tests must still pass.
- `app/lib/chat-upload.ts`: add `signVideo`, `uploadToCloudinary` (XHR multipart with progress, abortable), `registerVideo`. Error mapping covers every BE code in §4.3.
- New hook `app/hooks/useVideoAttachment.ts`: state machine `idle → consent → recording → preview → uploading → staged | error`. A registered video goes into the staged list as `{kind:"video", attachment_id, poster_url, duration_ms}`.
- `StagedAttachments` renders a video item (poster or a local first-frame thumbnail, duration, remove).
- `MessageInput` gets the 🎥 button, shown only if `allow_screen_recording`. Desktop (`navigator.mediaDevices?.getDisplayMedia` present) shows a menu with Record screen / Upload video. Otherwise it shows Upload video + "How to record your screen".
- Message rendering (customer bubbles): a `kind=video` item shows the poster + duration and plays inline (`<video controls preload="metadata">`); an expired item shows "Screen recording · 0:42 · expired" (images: "Image · expired").
- Render `SCREEN_RECORDING_ACK` like any assistant message.

### 5.3 Recorder
- `getDisplayMedia({ video: { width:{max:1280}, height:{max:720}, frameRate:{max:15} }, audio: false })`.
- MIME: first of `video/webm;codecs=vp9`, `video/webm;codecs=vp8`, `video/mp4` passing `MediaRecorder.isTypeSupported`; `videoBitsPerSecond` 800 000 (vp9) / 1 000 000 (others); `timeslice` 1000 ms so the byte total can be tracked.
- A countdown bar shows `0:NN left` + Stop. Auto-stop at 60 s **or** when the accumulated bytes reach 15 MB; the clip is **kept** and goes to preview. Track `ended` (browser "Stop sharing") stops it the same way. All tracks are stopped on every exit path.
- Preview plays from a blob URL with seeking disabled (MediaRecorder webm has no index). Buttons: Send / Re-record / Discard; an optional caption goes in the normal text input.
- `NotAllowedError` from a user cancel resets silently. A permissions-policy denial hides **Record screen** for the session and keeps Upload video.

### 5.4 Upload video
- `<input type="file" accept="video/mp4,video/quicktime,video/webm">`. Pre-check with a hidden `<video>` `loadedmetadata`: `duration ≤ 60` and `size ≤ 50 MB`, otherwise "Videos can be up to 60 seconds and 50 MB. Trim it in your Photos app and try again." If metadata cannot be read, upload anyway and rely on the BE check.

### 5.5 Consent sheet and mobile tip
- Consent sheet copy: "You'll choose a tab, window or screen to share. Anything visible will be recorded, so close passwords or private info first. No audio. Max 60 seconds. Kept for 30 days and seen only by {company_name}'s support team." Actions: Start / Cancel. It is shown once per session for both Record and Upload (flag kept in `sessionStorage`, try/catch, defaults to showing).
- The mobile tip sheet shows iOS steps (Control Center → Screen Recording; add it in Settings → Control Center if missing) or Android steps (swipe down twice → Screen record) based on the user agent; unknown devices see both. It ends with an "Upload video" button.

### 5.6 Error copy (customer)

| Case | Behaviour |
|---|---|
| Picker cancelled / permission denied | silent reset |
| sign 403 | hide 🎥 for the session |
| sign 429 | "You've sent the maximum number of recordings for now." |
| Cloudinary upload failure / network | "Upload failed." + Retry (clip kept) |
| register 422 `video_too_long` / `video_too_large` | the duration/size copy from §5.4 |
| register 422 `video_invalid` | "We couldn't process that video. Try recording again." |
| register 409 `already_registered` | treat as success using the returned `attachment_id` |
| send 422 `too_many_videos` | "One recording per message." |
| send failure | keep the draft (existing behaviour) |

## 6. Agent surfaces

### 6.1 `luran-dashboard`
- `AttachmentGrid` / `AttachmentThumb` get a `kind=video` branch: poster, ▶ overlay, duration chip. Clicking opens a new `VideoPlayerModal` (`<video controls preload="metadata" poster>` on the MP4 `url`, plus Open original and Download), reusing the `ImageLightbox` a11y patterns (focus trap, Esc, labelled close). No new dependency.
- Expired tiles: "Screen recording · 0:42 · expired" / "Image · expired".
- Unknown `kind` → skipped (forward-compatible).
- Inbox/ticket list preview for a video-only last message: "🎥 Screen recording".
- Settings → Widget: a new **Screen recording** toggle beside **File uploads**, each with its retention note ("Recordings/Images are automatically deleted 30 days after upload."). The toggle is disabled with "Not included in your plan." when the capability is false.

### 6.2 `luran_agent` (Flutter)
- `lib/data/models/attachment.dart` parses `kind`, `durationMs`, `posterUrl`, `expired`; an unknown kind is dropped (keeps parsing total).
- `_AttachmentsBlock` (`message_bubble.dart:133`) shows a poster tile with ▶ + duration; tap → a new `VideoPlayerScreen` (full-screen, `video_player`, play/pause + scrub). New dependency: `video_player` only. HTTPS Cloudinary needs no ATS/manifest change (confirm in build).
- Expired tile matches the dashboard's.

## 7. Testing

**BE (pytest; throwaway Postgres 15 + Redis 7 on 55432/56379; ruff 0.8.6 / black 24.10.0 via poetry):**
- Signer field set and tamper → signature mismatch.
- Gate fails closed.
- Register: happy path, each 422 (asset destroyed), 409 idempotent, wrong-folder (another session/tenant).
- Send: one-video cap; video excluded from `_turn_image_urls`; video triggers contact capture → escalation/ticket/ring + ACK; already escalated → no ring; ticket MISSED → ring; N3 human mode → ACK not the human-first reply.
- Serialization of `kind`/`expired` across REST, WS, transcript.
- Expiry job: guard skip, 7-day ceiling, not-found = done, per-row error isolation, kill switch.
- Orphan + unregistered sweeps.
- Purge passes `resource_type`.
- Migration up/down on a DB with existing image rows (backfill values).
- Cloudinary Admin API is mocked in tests.

**luran-chat (Vitest + `tsc --noEmit`):** `useVideoAttachment` with stubbed `getDisplayMedia`/`MediaRecorder` (60 s and 15 MB auto-stop keep the clip; track-ended; permissions-policy fallback; cancel); upload pre-check; mobile tip OS selection; gate hides the button; DashConversationView refactor regression; error-copy mapping.

**Dashboard:** Vitest (video thumb, modal, expired tile, settings toggle states), `tsc`, eslint (baseline 21 warnings), `next build`.

**Flutter:** model parsing tests, widget tests for the tile/expired/player screen, `flutter analyze`, iOS + Android build.

**Live on staging (source of the FE integration guide):**
1. Real `getDisplayMedia` recording inside the embedded widget on a test host page (**needs Adebayo's click**). This closes the spike's unverified picker item.
2. iPhone screen-recording `.mov` uploaded on the hosted support page.
3. Agent playback on the dashboard and on a device (app).
4. Expiry job with retention forced short on staging.
5. Firefox / Safari desktop recording if possible, otherwise marked unverified.
6. Capture sign, register 200 / each 422 / 409, the send response with ACK, the WS frame, and the expired attachment shape.

## 8. Rollout

1. **BE** → `develop` (staging). Prod promotion with `ATTACHMENT_RETENTION_ENABLED=false` until step 5.
2. **luran-widget** `display-capture` → CDN.
3. **luran-chat** → `stage` → `main` (button shows only where the gate is true).
4. **Dashboard** (`staging` → `main`) and **Flutter app** (`main`) players. These must be on prod/main **before** step 3 reaches prod; old clients skip unknown kinds but would not play video.
5. Turn retention on in prod. Existing images expire 30 days after the **migration** timestamp, so the migration should run close to this step or the grace window shrinks. Tenant release note covering the image-retention change.

**Rollback:** the gate (`can_use_screen_recording` via `override_flags`, or the tenant toggle) hides the feature with no deploy. `ATTACHMENT_RETENTION_ENABLED=false` pauses deletion. The migration downgrade drops the new columns; the EXPIRED enum value stays (Postgres cannot drop enum values), which is harmless.

## 9. Docs shipped with the work

- SOPs per repo: `luran/docs/sop/screen_recording_backend.md`, `luran/docs/sop/attachment_retention.md`, `luran-chat/docs/sop/screen_recording_widget.md`, `luran-dashboard/docs/sop/screen_recording_dashboard.md`, `luran_agent/docs/sop/screen_recording_agent_app.md`, `luran-widget` change noted in the luran-chat SOP.
- FE guide `luran/docs/fe-integration-guide-screen-recording.md`, live-captured (§7), with a verification table.
- Update `luran/docs/fe-integration-guide-chat-image-upload.md` for `kind`, `expired`, nullable `url`.
- Master checklist (workspace `docs/checklist/luran-master-checklist.md`): a "Screen recording + attachment retention" section mapped before the build.

## 10. Implementation packages

Contract-first:
- **P1 BE** (incl. retention, live-captured guide).
- **P2 customer FE** (luran-widget + luran-chat).
- **P3 agent FE** (dashboard + Flutter, in parallel with P2 once P1 is on staging).

Sonnet implements; one Opus review per package, plus an Opus review of `register` (upload trust) and the retention/purge workers (data deletion).

## 11. Risks / open items

- Host-page `Permissions-Policy` can block recording; Upload video remains (§5.3).
- Cloudinary free-plan transformation credits and video size limit (~100 MB, unverified): one eager MP4 per clip; measure during staging before rollout.
- iPhone HEVC `.mov` files: Cloudinary transcodes to H.264 MP4; verified live in §7 item 2.
- The image-retention change is visible to tenants (older tickets show placeholders); covered by the settings note + release note.
- Rotate the **dev** Cloudinary API secret before the build (the spike echoed part of it; owed by Adebayo).
