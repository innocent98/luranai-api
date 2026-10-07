# SOP — Chat image upload (backend, sub-project 1 of 4)

**Status:** Shipped on branch `feat/chat-image-upload-backend` (stacked on `feat/adopt-gpt6-luna-chatwide` / PR #100). Feature commits `733730c..cc85147` (base = the luna branch tip, reviewed separately in #100). 2026-10-07. **Not yet merged** — awaiting PR → `develop` once #100 lands (see Operate).
**Area:** `app/api/v1/endpoints/chat.py`, `app/services/{rag_service,conversation_service,image_moderation,account_purge_service}.py`, `app/utils/cloudinary_client.py`, `app/db/models/conversation.py`, `app/workers/{attachment_cleanup,document_processor}.py`, `app/schemas/conversation.py`, `app/core/config.py`, Alembic `ma50_add_message_attachments.py`.
**Spec:** `docs/superpowers/specs/2026-10-07-chat-image-upload-backend-design.md` · **Plan:** `docs/superpowers/plans/2026-10-07-chat-image-upload-backend.md`.

---

## What shipped

The backend for customers attaching images in the chat widget: gpt-6-luna interprets them and replies; human agents see customer images on takeover and can send images back; images surface in history; account deletion purges them; a daily cron reaps never-sent uploads. Bidirectional; contract-first (the widget/dashboard/agent-app consumers are separate sub-projects built against the live-captured guide).

## Why

Customers often need to show a problem (screenshot, receipt, damaged product) rather than describe it. gpt-6-luna (adopted chat-wide in #100) is vision-capable, so the model layer was ready; this adds the upload/storage/moderation/linking/purge plumbing around it.

## How (key decisions)

- **Two-step flow:** public `POST /api/v1/chat/upload` validates → **moderates the in-hand bytes (fail-closed) BEFORE Cloudinary upload** → stores → returns an `attachment_id`; the send then carries `attachment_ids[]` which link to the message. Moderating before upload means a flagged image is never stored, and avoids any OpenAI-must-fetch-a-URL dependency (a live probe showed OpenAI's fetcher 400s on some hosts).
- **Moderation:** OpenAI `omni-moderation-latest` (free), **fail-closed** — a flagged image, an API error, OR an empty/unverified response all reject (this was the one fail-OPEN bug the Opus review caught and we fixed).
- **Storage:** Cloudinary (`upload_chat_image` returns `public_id` for later deletion; images stored as JPEG, EXIF stripped, dimension-capped). New `CLOUDINARY_CHAT_FOLDER`.
- **Data model:** dedicated `message_attachments` table (migration `ma50messageattach`) carrying `tenant_id` — so the account-deletion completeness guard auto-enforces its cleanup. `url` is `EncryptedString`.
- **Vision:** a single `_user_content` seam in `rag_service` turns a text-only turn into text + `image_url` content-parts when `image_urls` is passed; verified live that luna accepts this via `chat.completions` (no Responses migration). Non-image path byte-identical.
- **Surfacing:** `Message.attachments` relationship → `MessageResponse.attachments` populates history + agent views; the send response adds `ChatResponse.user_message_attachments` (its `message` is the *assistant* reply). **FE must handle both shapes** (see FE guide).
- **Limits:** ≤10 MB (bounded read), **≤3 images/message** (enforced at schema + service), gate `widget_allow_file_upload` (customer only; agents bypass).
- **Purge + cron:** `purge_tenant` destroys each tenant attachment's Cloudinary asset (per-asset best-effort, never aborts the purge) then deletes rows; a daily cron (`cleanup_orphan_attachments`, 03:00) reaps `UPLOADED`+>24h orphans, keeping the row if `destroy` fails so it retries.

**Rejected alternatives:** Responses-API vision migration (unneeded — chat.completions works); `Message.meta` JSON for attachments (a table gives a clean purge query + per-image moderation state); moderating the Cloudinary URL after upload (data-URL-before-upload is safer).

## Verification

- 64 feature tests green locally under `OPENAI_LLM_MODEL=gpt-6-luna` + `ruff`/`black` clean. Migration reversibility + the purge **completeness guard** proven against Postgres (throwaway container; dev :5432 was another project's). Per-task reviews (Opus on the moderation + purge tasks) + an Opus whole-branch review: READY after the 3-image cap landed.
- **Not yet live-verified** (needs staging): real OpenAI moderation on a real image; luna fetching a real Cloudinary URL (fallback to base64 data-URL behind the seam if it can't); the end-to-end upload→vision→reply.

## Operate / roll back

- **Merge order:** this branch is stacked on #100. Merge #100 → `develop` first, then rebase this onto `develop` (drops luna's commits) and PR → `develop`. Deploy runs `alembic upgrade head` (applies `ma50messageattach`).
- **Roll back:** revert the feature commits; the migration's `downgrade()` drops the table + enum types cleanly.

## Follow-ups (all non-blocking; from the review triage)

- Link-time `direction` not enforced (an agent could re-link an unlinked INBOUND orphan — hygiene).
- Authed agent upload not rate-limited (calls paid moderation + Cloudinary).
- Synchronous Cloudinary `destroy` in purge (slow for a tenant with many images).
- Orphan-cleanup has no attempt cap (a permanently-failing `public_id` logs daily).
- `content_type` stored from the client header though the asset is always JPEG; animated GIF is flattened — document in the FE guide.
- **HEIC deferred** (shared `validate_image` is PIL-only; needs `pillow-heif`). V1 = JPEG/PNG/WebP/GIF; FE widget should request JPEG so iOS converts.
- **FE integration guide pending** — capture live request/response bodies after staging (global rule; not schema-derived).

## Addendum — authed customer-app gate + attachment forwarding (2026-10-07, PR #108)

Small follow-on so the customer app can gate + send images on its own surfaces:
- Exposed `allow_file_upload: bool` (default true, reflects `Tenant.widget_allow_file_upload`) on `CustomerConversationDetail` (`GET /customer/conversations/{id}`) and `PublicBusinessProfileResponse` (`GET /public/business/{slug}`) — mirrors the widget public-config flag so the FE threads one consistently-named flag everywhere. Private-tenant profiles redact widget config, so the flag falls back to its default there (gate on `is_private`).
- Fixed `send_customer_message` (`POST /customer/conversations/message`) silently dropping `attachment_ids` when it reconstructed the `MessageRequest` for the shared `send_message` handler — authed-customer image sends never linked. Now forwarded; links + surfaces on `user_message_attachments` identically to the public `/chat/message` path. (The authed upload reuses the public, session-scoped `POST /chat/upload` — no bearer gate blocks it.)
- Tests: `tests/api/v1/test_customer_conversations.py` (gate true/false + attachment-link) and `tests/api/v1/test_public_business_profile.py` (gate true/false). ruff/black clean; touched + regression suites green.
