# SOP — Chat image upload (backend, sub-project 1 of 4)

**Status:** ✅ **SHIPPED + LIVE (staging + prod), 2026-10-07 → fully promoted to prod 2026-10-08.** Merged via PRs **#101** (image backend) on top of **#100** (luna) + **#102** (CI/build reproducibility) + **#103** (deploy lazy-load hotfix), promoted to prod via **#104** (develop→main). FE integration guide **#105** + two FE-driven follow-ups **#106** (live inbound to agents; History/Missed images) + authed-gate/attachment-forwarding **#108** + escalated-AI-suppression **#109** were on `develop`; **all promoted to prod 2026-10-08 via #110 (develop→main, main=c3a078f)** — production deploy succeeded (no new migration; `ma50` already in prod via #104), prod health 200 + `/upload` 422 live-verified. Staging happy-path live-captured (upload→moderation→Cloudinary→luna vision reply).
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

---

## Addendum — Suppress AI reply on an already-escalated session (2026-10-07, PR #109)

**What shipped:** PR **#109** (`fix/suppress-ai-on-escalated-session` → `develop`). When a session is already `ESCALATED` (a human agent owns it), a REST send to `POST /api/v1/chat/message` now persists the customer's message and returns **without** generating an AI reply — instead of the bot intruding on the human↔customer thread.

**Why:** Customers send **images** over REST even after escalation (text goes over the agent WebSocket; images can't, so the widget uses `/chat/message` + `/customer/conversations/message`, which then fan out to the agent via `_fanout_customer_images`). That image send *also* ran the full RAG/model pipeline and produced a spurious assistant reply in the middle of a live human conversation. Root cause: `_send_message_impl` had no `ESCALATED` branch — only `ABANDONED→ACTIVE` and `RESOLVED→ACTIVE` — so generation always proceeded.

**How (key decisions):**
- **Injection point:** `app/api/v1/endpoints/chat.py` immediately after `effective_message_lower` is computed (`~chat.py:1082`), i.e. *after* the `attachment_ids` linking wrapper is installed (`~chat.py:1066`) and *before* the flow-router / RAG / generation steps. Placing it here (not at the ABANDONED/RESOLVED block ~909, which runs *before* the wrapper is installed) means the image-only path still links attachments through the same `_add_message_linking_attachments` wrapper, so the wrapper's `commit` + the `send_message` wrapper's `_fanout_customer_images` fire unchanged.
- **Narrow, not blanket (important):** suppress every input on an escalated session **except** the `__talk_to_human__` control token. That token must fall through to the existing escalation branch so an again-requested or previously-MISSED escalation re-rings the agent — this is intentional behaviour guarded by `test_chat_flow_router::test_re_escalating_*`. A blanket early-return broke those two tests; narrowing to exclude the token keeps them green while still suppressing plain text, images, and stray welcome/flow triggers (all of which would otherwise surface a bot message during human chat). Checking `effective_message_lower` (not the raw message) also catches the token when it arrives as a `button_value`.
- **`ChatResponse` shape decision:** the schema requires a non-optional `message: MessageResponse` and there is deliberately no assistant reply this turn, so `message` **echoes the persisted USER message** (`role:"user"`). `should_escalate=False`, `escalation_reason=None`, `ticket_id=None`; `user_message_attachments` carries the linked images as on the normal path. The FE must not render `message` as an assistant bubble when the session is escalated (documented in the FE guide §4b). Rejected: returning a synthetic assistant/system acknowledgement (invents a bot turn the UI would show) — echoing the user message is the least-surprising valid shape.
- **Latent `None.lower()` guard:** the `RESOLVED` path at `~chat.py:917` was flagged for `message_data.message.lower()` on `None`. It is in practice already guarded by the `IMAGE_ONLY_PLACEHOLDER` substitution at `~chat.py:805` (an image-only/empty send never reaches 917 with `None`), so it is **not currently reachable** — but I applied the one-line defensive guard `(message_data.message or "").lower()` anyway since the task flagged it and it hardens against future reordering.

**Files:** `app/api/v1/endpoints/chat.py` (the escalated branch + the RESOLVED `None` guard); `tests/api/v1/test_chat_escalated_suppression.py` (new). No schema, migration, or WebSocket/`_fanout_customer_images` change.

**Verification:** `poetry run ruff check app/` + `poetry run black --check app/` clean; new suite (4 tests: escalated text-suppressed, escalated image-only link+suppress, escalated image fan-out-but-no-reply, ACTIVE still replies) green; the CI automation/flow-regression job (`test_chat_flow_router.py` + `test_flow_action_call_regressions.py`, 14 tests) green under the CI SQLite env; `test_chat_image_send.py` + the chat/customer-conversation regression net (37 tests total) green. Postgres-only suites (`tests/voice/test_escalate.py`, `test_notification_triggers`) error at fixture setup locally (no `localhost:5432`), unrelated to this change.
