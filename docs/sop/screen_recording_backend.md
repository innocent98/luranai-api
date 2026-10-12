# SOP — Screen recording (video attachments), backend (2026-10-12)

**Status:** merged to `develop` via PR #122 (`feat/screen-recording-be`, merge `8db4af1`). A follow-up one-line fix (section "Live-capture finding") is required for any real use. Prod promotion has prerequisites (see "Rollout prerequisites").
**Spec:** `docs/superpowers/specs/2026-10-12-screen-recording-design.md` (SR-D1..D10 and section 12 amendments) · **Plan:** `docs/superpowers/plans/2026-10-12-screen-recording-backend.md` · **FE contract:** `docs/fe-integration-guide-screen-recording.md` (captures in `docs/fe-integration-captures/screen-recording/`) · **Retention:** `docs/sop/attachment_retention.md`.

## What shipped
Customers can attach a screen recording (max 60 s) to a chat message. The browser uploads straight to Cloudinary using a signed payload, the API verifies and registers the asset, and sending it escalates the chat to a human. The AI never sees the video. Recording attachments carry `kind`, `duration_ms`, `poster_url`, `expired` on every REST and WebSocket surface.

## Why
Customers struggle to describe multi-step UI bugs in text, and the AI (gpt-6-luna) handles text and images but not video. A recording lets a human agent see the problem. Video also needs a hard human hand-off because there is no automated video moderation.

## How
| Concern | Decision |
|---|---|
| Upload trust (SR-D8) | `POST /chat/video/sign` mints a signed direct-upload (folder `<CLOUDINARY_CHAT_FOLDER>/<tenant>/<session>/video`, random hex `public_id`, `overwrite=false`, eager `c_limit,q_auto,vc_h264,w_1280/mp4` async, tags `chat,screen_recording,screen_recording_<ENVIRONMENT>,<tenant>`). `POST /chat/video/register` checks ownership with an exact `fullmatch` on `<folder>/<32 hex>` **before any Cloudinary call**, then verifies through the Admin API (resource type video, format webm/mp4/mov, duration <= 61 s, bytes <= 50 MB). A rejected asset is destroyed (never one outside the caller's folder). Rejected: Cloudinary webhook; proxying bytes through the API (nginx 12 MB cap). |
| Delivery (SR-D9) | One progressive H.264 MP4 (the eager derivative, `.mp4`) plus a JPEG poster (`so_0,w_640`). No HLS. |
| Escalation (SR-D4) | A message that links one video skips flows, automations and the AI. It uses the existing contact-capture gate, `_escalate_to_human`, ring and MISSED reopen. ACK is fixed text stored as an assistant message with `meta.system_notice = "screen_recording_ack"`. Already escalated: persisted and fanned out, no ACK, re-ring only for a MISSED ticket. |
| Limits | 60 s (`SCREEN_RECORDING_MAX_SECONDS`), 50 MB (`SCREEN_RECORDING_MAX_BYTES`), 1 video per message (`too_many_videos`), 3 per session (`video_session_limit`, checked at sign and register), sign 5/h/IP (`http_video`), register 20/h/IP (`http_video_register`). |
| Gate (SR-D3) | `screen_recording_allowed` = platform grant `TenantCapabilities.can_use_screen_recording` (default true, **not** a plan-config key, so plan sync never resets it) AND `Tenant.widget_allow_screen_recording` (default true). Fails closed. Exposed as `allow_screen_recording` on public widget config, visitor bootstrap, customer conversation detail and business profile; admin widget config carries the toggle and the read-only grant. |
| Serialization | `AttachmentOut` and `attachment_frames` add `kind`, `duration_ms`, `poster_url`, `expired`; `url` is nullable. `AttachmentStatus` gains `EXPIRED`, `AttachmentModeration` gains `NOT_APPLICABLE` (video rows). |
| Purge | `account_purge_service` now passes `resource_type=<kind>` (it was hard-coded to image, a latent video leak). `destroy(public_id, *, resource_type)` is keyword-required everywhere. |
| Unique index | Partial unique index on `cloudinary_public_id` where `kind = 'VIDEO'` (idempotent register; image test data reuses ids). |

## What's involved
- Migration `alembic/versions/sr10_screen_recording.py` (revision `sr10screenrec`, down `n3firstresp`).
- Endpoints: `app/api/v1/endpoints/chat_video.py` (new; `sign`, `register`), video branch in `app/api/v1/endpoints/chat.py` (`turn_has_video`, around lines 1224-1360), `widget.py`.
- Services/utils: `app/services/screen_recording/gating.py`, `app/utils/cloudinary_client.py` (`sign_video_upload`, `get_video_resource`, `video_delivery_urls`, `list_tagged_videos`, `destroy`), `app/utils/http_rate_limiter.py`, `app/services/first_responder.py` (`SCREEN_RECORDING_ACK`), `conversation_service.py`, `account_purge_service.py`.
- Schemas: `app/schemas/{conversation,widget,tenant,visitor,customer_conversation}.py`. Models: `app/db/models/{conversation,tenant}.py`.
- Config: `ATTACHMENT_RETENTION_DAYS=30`, `ATTACHMENT_RETENTION_ENABLED`, `SCREEN_RECORDING_MAX_SECONDS=60`, `SCREEN_RECORDING_MAX_BYTES=52428800`, plus `ENVIRONMENT` and `CLOUDINARY_CHAT_FOLDER` (now part of the sweep's tag and id shape).
- Workers: see `docs/sop/attachment_retention.md`.

## Migration `sr10screenrec`
Adds `kind` (enum `attachment_kind`), `duration_ms`, `poster_url`, `source_format`, `expires_at` (NOT NULL, server default now+30 d), makes `url` nullable, adds enum values `EXPIRED` and `NOT_APPLICABLE`, the partial unique index and the two gate columns. Existing rows are backfilled `kind = IMAGE`, `expires_at = migration time + 30 d`. Verified on Postgres 15 (upgrade, downgrade, upgrade with a seeded pre-sr10 row) and again in this live capture (fresh `alembic upgrade head`).

## Verification
- PR #122: full suite on Postgres 15 + Redis 7, 1520 passed, 33 skipped; ruff 0.8.6 and black 24.10.0 clean; Opus reviews of `register` and the retention job.
- Live local capture on 2026-10-12 (26 captures, real Cloudinary dev cloud): sign 200/403/404/429; real upload; register 200/409/422 (foreign folder, missing asset, 80 s clip); send with ACK, already-escalated echo, contact-capture prompt, `too_many_videos`; agent WS `customer_message` frame; REST list; real `expire_attachments` run and the expired shape; MP4 and poster served with HTTP 200 on the first request. Details and the NOT LIVE list: the FE guide's verification table.

## Live-capture finding (must be deployed)
Cloudinary's Admin API `resource()` does not return `duration` unless called with `media_metadata=True`. Without it the BE read duration 0 and **rejected every real recording with `422 video_too_long`**. Mocked unit tests could not see this. Fix: `get_video_resource` passes `media_metadata=True` + regression test `test_get_video_resource_requests_media_metadata`. Confirm it is on `develop` before any environment is used for recordings.

## Operate / roll back
- **Kill switch, no deploy:** set the tenant's `widget_allow_screen_recording=false` (admin PATCH `/widget/config`), or revoke the platform grant `can_use_screen_recording` per tenant via the platform `TenantCapabilitiesUpdate` endpoint. The button then disappears (`allow_screen_recording=false`) and `sign`/`register` return 403 `screen_recording_disabled`.
- **Pause deletion:** `ATTACHMENT_RETENTION_ENABLED=false`.
- **Migration downgrade** drops the new columns and **deletes** `VIDEO` rows and rows with `url IS NULL` (EXPIRED) without destroying their Cloudinary assets. Before downgrading, destroy or sweep the video assets yourself. The `EXPIRED`/`NOT_APPLICABLE` enum values remain (Postgres cannot drop enum values). Downgrade also loses the image grace dates.
- Local capture harness notes: staff test users must use `@example.com` (login rejects `.test` with 422); run workers/jobs in-process with `asyncio.run(expire_attachments({}))` against the same env.

## Rollout prerequisites (from PR #122, read before promoting)
1. **Prod must set `ATTACHMENT_RETENTION_ENABLED=false`** at promotion. It stays off until the dashboard and Flutter players ship (P3). If retention is enabled more than about 3 days after `sr10` ran, re-stamp the image grace first (SQL in `docs/sop/attachment_retention.md`).
2. **Every environment must set a distinct `ENVIRONMENT`.** The compose files default it to `production`, and the value now feeds the sweep's tag. Preferably give each environment its own Cloudinary cloud or `CLOUDINARY_CHAT_FOLDER`.
3. **Never run workers against a restored copy of another environment's DB while it holds that environment's Cloudinary credentials.** Retention, cleanup, purge and the sweep would delete the source environment's assets.
4. **FE order:** the dashboard and Flutter players must reach prod before the widget or luran-chat ship the record button. The gate is on by default, and old agent clients would show `video/mp4` as a broken image.

## Follow-ups
- P2 (luran-widget `display-capture`, luran-chat recorder) and P3 (dashboard + Flutter players, settings toggle, expired tiles) plans, written from the FE guide.
- PENDING-HUMAN: real `getDisplayMedia` inside the embedded widget; iPhone `.mov` upload; Firefox/Safari recording.
- Measure Cloudinary transformation credits (one eager MP4 per clip) and the free-plan video size limit; rotate the dev Cloudinary secret (owed by Adebayo).
- `sweep_unregistered_videos` was only run as a no-op live (needs assets older than 24 h).
