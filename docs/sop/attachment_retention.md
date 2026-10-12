# SOP — Chat attachment retention (30 days) (2026-10-12)

**Status:** merged to `develop` with PR #122 (`8db4af1`). **Prod: keep `ATTACHMENT_RETENTION_ENABLED=false` until the enable procedure below is done.**
**Spec:** `docs/superpowers/specs/2026-10-12-screen-recording-design.md` section 4.5 (SR-D5, SR-D6) · **Related:** `docs/sop/screen_recording_backend.md`, `docs/fe-integration-guide-screen-recording.md` (expired shape, section 6).

## What shipped
All chat attachments (images and videos) are destroyed at Cloudinary 30 days after upload. The `message_attachments` row is kept with `status = EXPIRED`, `url = NULL`, `poster_url = NULL`, so transcripts show an "expired" placeholder. Unsent and unregistered uploads are also reaped.

## Why
Screen content can show passwords, balances or third-party data, and images were kept forever. Retention bounds that exposure.

## How
| Job (ARQ cron, `app/workers/document_processor.py`) | Schedule | What it does |
|---|---|---|
| `expire_attachments` (`app/workers/attachment_retention.py`) | daily 03:15 UTC | Selects `status = LINKED AND expires_at < now()` oldest first, batches of 200 (max 10 batches). Per row: `destroy(public_id, resource_type=kind)` in `asyncio.to_thread`; ok or "not found" counts as success -> `EXPIRED`, `url`/`poster_url` NULL, commit per row. Any other error: log public_id + error class, leave for the next run, never retry the row in the same run. A breaker ends the run after 20 consecutive destroy failures. No-op when `ATTACHMENT_RETENTION_ENABLED` is false. |
| `cleanup_orphan_attachments` (`attachment_cleanup.py`) | existing, 03:00 UTC | now also covers `kind = VIDEO`, passes `resource_type=kind`. |
| `sweep_unregistered_videos` | daily 03:30 UTC | Admin API list of `resource_type=video` with tag `screen_recording_<ENVIRONMENT>` older than 24 h (max 5 pages x 100). Destroys an asset only if its id matches this environment's folder shape, it has no `message_attachments` row, **and** its tenant+session exist in this DB. |
| Account purge | on deletion | destroys with `resource_type=row.kind`. |

- **Active-ticket guard:** a row whose session has a ticket `open`, `in_progress` or `missed` is skipped, **unless** `expires_at < now - 7 days` (the 7-day hard ceiling, `ACTIVE_GRACE`).
- **Existing images (SR-D6):** the migration stamps them `expires_at = migration time + 30 d` (30-day grace from rollout), not `created_at + 30 d`, so nothing is mass-deleted on day one.
- `expires_at` is set from `ATTACHMENT_RETENTION_DAYS` (default 30) for new uploads.

## What's involved
`app/workers/attachment_retention.py`, `attachment_cleanup.py`, `document_processor.py` (cron registration, lines ~1316-1330), `app/utils/cloudinary_client.py` (`destroy`, `list_tagged_videos`, `video_sweep_tag`), `app/services/account_purge_service.py`, `app/core/config.py`, migration `alembic/versions/sr10_screen_recording.py`. Tests: `tests/workers/test_attachment_retention.py`, `tests/workers/test_attachment_cleanup.py`.

## Verification
- Unit tests: guard skip, 7-day ceiling, not-found = done, per-row isolation, breaker, kill switch, sweep scoping.
- Live local (2026-10-12): set one linked video's `expires_at` to `now() - 8 days`, ran `expire_attachments` in-process with a real dev Cloudinary cloud -> returned 1; the REST message list then showed `expired: true`, `url: null`, `poster_url: null` (capture `26-session-messages-admin-after-expiry.json`). The 8-day offset was used because the session's ticket was open; the active guard otherwise defers it. `sweep_unregistered_videos` also ran (0, nothing older than 24 h).

## Prod enable procedure
1. Promote with `ATTACHMENT_RETENTION_ENABLED=false`. Confirm the dashboard and Flutter players are live (a tenant cannot see an expired placeholder correctly otherwise).
2. If you enable retention **more than ~3 days after `sr10` ran**, the stamped image grace has been eating into the 30 days. Re-stamp first (Adebayo runs it; `<sr10 deploy time>` is the timestamp migration `sr10screenrec` ran in prod):
   ```sql
   UPDATE message_attachments
   SET expires_at = now() + interval '30 days'
   WHERE kind = 'IMAGE'
     AND status = 'LINKED'
     AND expires_at < now() + interval '30 days'
     AND created_at < '<sr10 deploy time>';
   ```
3. Set `ATTACHMENT_RETENTION_ENABLED=true` and redeploy the worker.
4. Send the tenant release note: "Chat images and screen recordings are now automatically deleted 30 days after upload. Older tickets will show an 'expired' placeholder."
5. First run: watch the worker log line `Attachment retention: N expired, N skipped, N deferred to next run`.

## Operate / roll back
- **Kill switch:** `ATTACHMENT_RETENTION_ENABLED=false` (the job logs and returns 0). Already-destroyed assets cannot be restored.
- **Never run the workers against a restored copy of another environment's DB while it holds that environment's Cloudinary credentials**: retention, cleanup, purge and the sweep would delete the source environment's assets. Give each environment a distinct `ENVIRONMENT` (it feeds the sweep tag) and preferably its own Cloudinary cloud or `CLOUDINARY_CHAT_FOLDER`. The `expire_attachments` job itself is scoped only by DB rows, so a restored production copy with production credentials is the dangerous case.
- **Downgrade caveat:** migration downgrade deletes `VIDEO` and EXPIRED (`url IS NULL`) rows without destroying their Cloudinary assets. Sweep or destroy the video assets first if you need them gone.

## Follow-ups
- Per-tenant retention periods and pre-deletion notifications are non-goals for v1.
- Measure the first real prod run (rows, Cloudinary API time) and the credit use of eager MP4s.
- Add a metric/alert on consecutive destroy failures (today only a log line).
