# Chat Image Upload — Backend Design (sub-project 1 of 4)

**Status:** Design — awaiting user review before planning.
**Date:** 2026-10-07
**Repo:** `luran` (FastAPI backend).
**Depends on:** `feat/adopt-gpt6-luna-chatwide` (PR #100) — the vision model (`gpt-6-luna`) and the
capability-aware `build_chat_completion_kwargs()`. This feature should build on top of that branch
(or after it merges to `develop`).

---

## 1. What this is

Let chat customers attach **images** in the widget; `gpt-6-luna` interprets them and replies in the
same conversation. When a human agent takes over, the agent **sees** customer images and can **send
images back**. Multi-tenant, with a public (unauthenticated) upload endpoint, so storage, retention,
moderation, gating, and the account-deletion purge all matter.

This is sub-project **1 of 4** (contract-first). The three frontend consumers — widget
(`luran-chat`), dashboard (`luran-dashboard`), agent app (`luran_agent`) — are separate specs built
against this backend's **live-captured** contract.

**In scope (V1, bidirectional):** customer→AI image understanding + response; agent sees customer
images on takeover; agent→customer image send.
**Out of scope (YAGNI):** video/audio/PDF/other file types; AI image *generation*; standalone OCR;
albums beyond the per-message cap; image editing.

## 2. Decisions (settled in brainstorming 2026-10-07)

| # | Decision |
|---|---|
| Scope | Bidirectional: customer↔AI, agent sees + sends on takeover |
| Storage | **Cloudinary** (reuse `app/utils/cloudinary_client.py`); store secure URL + `public_id`; pass **URL** to luna vision |
| Moderation | **OpenAI `omni-moderation-latest`** (free; text+image) on every upload; reject flagged (sexual/violence/self-harm) |
| Moderation failure | **Fail-closed** — if the moderation call errors, reject the upload (never store/show unmoderated) |
| Data model | **Dedicated `message_attachments` table** (Alembic migration) |
| Gating | Per-tenant `widget_allow_file_upload` — **default ON for all** (matches existing flag default) |
| Limits | ≤10 MB/image; ≤3 images/message; **JPEG, PNG, WebP, non-animated GIF** (HEIC **deferred** — see note); strip EXIF/GPS |

> **HEIC deferred (ruling 2026-10-07):** V1 uses the shared `validate_image` (PIL), which supports JPEG/PNG/GIF/WebP only. HEIC needs a `pillow-heif` dependency + a change to that shared validator (used by logo/channel/email uploads) and is deferred to a follow-up. FE guidance: the widget's file input should request `image/jpeg,image/png,image/webp` so iOS converts HEIC to JPEG on selection.
| Upload flow | **Two-step**: upload returns an attachment ref; send-message carries `attachment_ids[]` |

## 3. ⚠️ Validation-first (plan task 1, before building)

The spike proved luna vision via the **Responses API**. Our chat pipeline uses
**`chat.completions`**, and vision via `chat.completions` + luna is **unverified** (tool-calling
needed `reasoning_effort="none"` there — vision may have a similar constraint). **Task 1 of the plan
is a throwaway live probe**: `chat.completions.create(model=gpt-6-luna, messages=[{role:user,
content:[{type:text},{type:image_url,image_url:{url}}]}], reasoning_effort=low|none)`.
- If it works → proceed as designed (URL content-parts).
- If rejected → adapt: try `reasoning_effort="none"`, else route image turns through the Responses
  API (`responses.create` with `input_image`). The design's prompt-integration section (§6) is
  written to allow either path behind one seam.

This gates the design; nothing downstream is built until it's answered.

## 4. Data model — `message_attachments`

New table (Alembic migration), one row per image:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `message_id` | UUID FK → messages, **nullable** | NULL while uploaded-but-not-yet-sent; set on send |
| `session_id` | UUID FK → sessions, indexed | set at upload so orphans + purge can be found without a message |
| `tenant_id` | UUID FK → tenants, indexed | denormalized for a clean purge query |
| `direction` | enum `inbound`/`outbound` | inbound = customer→us; outbound = agent→customer |
| `cloudinary_public_id` | str | for deletion on purge |
| `url` | **EncryptedString** | the secure Cloudinary URL (encrypted at rest; may embed identifiers) |
| `content_type` | str | e.g. `image/jpeg` |
| `width`, `height` | int nullable | |
| `bytes` | int | post-processing size |
| `moderation_status` | enum `passed` (only stored rows are `passed`; flagged never persist) | audit |
| `status` | enum `uploaded`/`linked` | `uploaded` until a message references it |
| `created_at` | datetime | orphan cleanup uses this |

**Why a table (not `Message.meta` JSON):** per-image moderation/status, multiple images, and a clean
purge query (`WHERE tenant_id = ?`) — see §8.

## 5. Endpoints (contract — bodies to be live-captured post-build for the FE guide)

### 5.1 Customer upload — `POST /api/v1/chat/upload` (public, session-scoped)
- Auth: same public/session context as other widget chat endpoints (no bearer). Requires valid
  `session_id` + tenant resolution; rejects if the tenant's `widget_allow_file_upload` is false (403).
- Body: `multipart/form-data` — `file`, `session_id`.
- Pipeline: size/type validate → strip EXIF → **OpenAI moderation** (fail-closed) → Cloudinary
  upload → insert `message_attachments` row (`status=uploaded`, `direction=inbound`, `message_id=NULL`).
- 200: `{attachment_id, url, content_type, width, height, bytes}`.
- Errors: 400 bad request, 403 feature-off, 413 too large, 415 unsupported type, 422
  `{detail:{code:"image_rejected", reason}}` (moderation flagged OR moderation unavailable — same
  friendly shape), 429 rate-limited, 502 storage error.

### 5.2 Send message with attachments — extend the existing send path
- `MessageRequest` gains `attachment_ids: list[UUID] | None`; **`message` becomes optional when
  attachments are present** (today `min_length=1` required).
- Validates each id belongs to the session + is `uploaded` + unlinked; links them (`message_id` set,
  `status=linked`); builds the luna vision turn (§6).
- `MessageResponse` gains `attachments: list[AttachmentOut]` so every surface renders them.

### 5.3 Agent upload + send (takeover, bearer-authed)
- **New authed endpoint `POST /api/v1/agent/chat/upload`** mirroring 5.1's pipeline (validate →
  EXIF strip → moderation → Cloudinary → row) but **bearer-authed** (agent identity), setting
  `direction=outbound`. Same limits + fail-closed moderation. Returns the same body as 5.1.
- **Extend `POST /chat/sessions/{id}/agent-message`** (chat.py:2751) with `attachment_ids[]`
  (links outbound rows to the agent message); agent text becomes optional when attachments present.

## 6. Prompt integration (rag_service) — behind one seam

A single helper builds the user turn: text-only → today's string; with image attachments → a
content-part list `[{type:"text",...}, {type:"image_url", image_url:{url}} …]`. The helper is the
**one seam** that §3's validation may flip from `chat.completions` to `responses.create` — nothing
else in `rag_service` changes. Reuses `build_chat_completion_kwargs()` from PR #100.

## 7. Gating, limits, abuse controls

- **Gate:** `widget_allow_file_upload` (default ON); widget bootstrap already exposes it (widget.py:73).
- **Limits:** ≤10 MB (bounded read), ≤3/message, formats JPEG/PNG/WebP/non-animated GIF (HEIC deferred — see §2 note), EXIF stripped.
- **Rate limit:** reuse the existing limiter keyed by session + IP on the public upload endpoint.
- **Moderation:** OpenAI omni-moderation on every upload; flagged → 422; moderation error → 422 (fail-closed).

## 8. Account-deletion purge (hard requirement)

Extend the purge worker + its completeness guard ([[project_account_deletion]]): for the tenant,
select `message_attachments WHERE tenant_id = ?`, **delete Cloudinary assets by `public_id`** (batch,
tolerate already-deleted), then delete rows. Add `message_attachments` to the completeness guard so a
tenant purge fails loudly if any attachment or asset remains.

## 9. Orphan cleanup

A periodic ARQ job deletes `message_attachments` with `status=uploaded` (never sent) older than N
hours (default 24h) + their Cloudinary assets. Covers abandoned uploads.

## 10. Error handling summary

- Upload: friendly, non-leaking messages; moderation rejection and moderation-outage share one
  `image_rejected` shape so the widget shows one consistent message.
- Send: invalid/foreign/already-linked `attachment_ids` → 400; image-only message allowed.
- Cloudinary/model failures: surfaced as retryable without exposing internals.

## 11. Testing

- Unit: validation (type/size), EXIF strip, moderation pass/flag/**error→reject**, attachment
  link/ownership, image-only message acceptance, `MessageResponse.attachments` shape.
- Prompt: vision content-part build (text-only unchanged; image turn produces image_url parts).
- Purge: tenant purge deletes attachment rows + calls Cloudinary delete by `public_id`; completeness
  guard fails if any remain.
- Orphan job: unlinked + old → deleted; linked/recent → kept.
- The §3 live validation is a throwaway probe, not a committed test.

## 12. Live contract capture (global rule)

After the backend runs on staging, capture **real** request/response bodies for 5.1–5.3 (+ all error
shapes) into `docs/fe-integration-guide-chat-image-upload.md` before the FE sub-projects build. No
schema-derived payloads.

## 13. Build order

1. Backend (this spec) → plan → build (task 1 = §3 validation).
2. Widget (luran-chat) — against the live guide.
3. Dashboard + 4. Agent app — against the live guide.
