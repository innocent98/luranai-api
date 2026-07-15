# Stage 3 — AI Sales Agent · Slice 5d "Slack Notifications" — Backend Design

**Date:** 2026-06-30
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · Alembic · ARQ · httpx · pytest)
**Paired FE spec:** `luran-dashboard/docs/superpowers/specs/2026-06-30-stage3-slack-notif-dashboard-design.md`
**Builds on:** Slice 5a (`NotificationLog`, `NotificationService.dispatch`, `_try_log`, `_EmailSendFailed` retry pattern, ARQ worker registration), 5c (`_render_in_app_text`, `title`/`body` columns). **Branch `feat/stage3-slack-notif-backend` stacks on `feat/stage3-inapp-notif-backend` (Slice 5c PR #38).**
**Decomposition:** Slice 5 = 5a Email ✅ → 5b PDF ✅ → 5c In-app ✅ → **5d Slack (this)** → 5e CRM.
**PRD:** §3.6 (`notify_channels.slack`).
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Post the three rep-alert events (sales-ready lead, meeting booked, quote generated) to a tenant's Slack via a per-tenant **Incoming Webhook URL**, gated by `notify_channels.slack`. Backend half of the paired slice.

**Decisions (brainstorm):**
- **Incoming Webhook URL** (no OAuth, no token storage) — the tenant pastes a `https://hooks.slack.com/…` URL.
- Slack is a network call → delivered via an **ARQ job** (retry/non-blocking), like email — *not* inline like in-app.

**In scope (BE):** `slack_webhook_url` config column; dispatch writes a `slack` `NotificationLog` row + returns a `send_slack_notification` job when enabled+configured; the ARQ job POSTs a Block Kit message via httpx with retry.

**Out of scope:** Slack OAuth app / channel picker / threading; per-event routing; CRM (5e). The dashboard webhook-URL field is the paired FE spec.

---

## 2. Config

Add `slack_webhook_url: str | None` to `TenantSalesAgentConfig` (`String(512)`, nullable; additive migration; `default_for` → `None`). `notify_channels.slack` already exists.

---

## 3. Dispatch — write slack row + return job

Extend `NotificationService.dispatch`: when `notify_channels.slack` is truthy **AND** `config.slack_webhook_url` is set, for each of the three rep events:
- Render `(title, body)` via the existing `_render_in_app_text(event, entity_type, entity_id)`.
- `_try_log(channel=NotifChannel.SLACK, recipient="slack", status=NotifStatus.PENDING, title=title, body=body, …)` — `recipient="slack"` sentinel → one slack notification per `(tenant, event, entity)` via the dedupe index. Idempotent (reuse the savepoint pattern).
- On a fresh insert, **append `(log_id, "send_slack_notification")` to the returned `jobs`** (Slack goes through ARQ like email).
- If `notify_channels.slack` is off OR `slack_webhook_url` is missing → no slack row, no job.

Email (jobs) + in-app (inline row) behavior unchanged. `dispatch` now may return email AND slack jobs.

---

## 4. Slack ARQ job

`send_slack_notification(ctx, notification_log_id)` (`app/workers/sales_notifications.py`, registered in `WorkerSettings.functions`):
- Fresh `SessionLocal()`; load the `NotificationLog`; if missing or already `SENT` → return (idempotent).
- Load the tenant's `TenantSalesAgentConfig.slack_webhook_url` **at send time** (so a URL change doesn't break the dedupe key). If the URL is now empty → mark `failed` ("no webhook"), return.
- Build a small **Block Kit** payload: a `header` block (`title`) + a `section` block (`body`) + a `context`/`actions` "View in dashboard" link. POST it to the webhook via `httpx.Client().post(url, json=payload, timeout=10)`.
- **Outcome mapping:** `2xx` → `sent`. A **5xx or network error/timeout** → mark `failed` + `raise` (an `_EmailSendFailed`-style sentinel) so ARQ retries. A **4xx** (bad/expired/revoked webhook — Slack returns 400/404 with `invalid_payload`/`no_service`) → mark `failed`, do NOT raise (retrying a dead webhook is pointless). `finally: db.close()`.

Reuse the 5a `_EmailSendFailed` sentinel + outer-handler guard so the retry path is consistent and doesn't double-commit.

---

## 5. Safety & invariants

- Slack row/job only when `notify_channels.slack` AND `slack_webhook_url` set; idempotent (dedupe index); tenant-scoped.
- Non-blocking: dispatch only writes the row + returns the job; the httpx POST runs in the worker. A Slack failure never affects the chat/booking response or other channels.
- Webhook URL is tenant secret-ish config (stored plaintext like other config; not logged — log only status/error code, never the URL or message body verbatim beyond what's needed).
- 5a/5b/5c (email, PDF, in-app) behavior unchanged.

---

## 6. Testing (pytest)

- **Dispatch slack**: `notify_channels.slack` on + `slack_webhook_url` set → a `slack` row (PENDING, title/body) AND a `send_slack_notification` job returned; slack off → none; URL missing → none; idempotent (2nd dispatch → no new row/job); email+slack both on → both jobs.
- **ARQ job** (mock `httpx`): 2xx → log `sent`, POSTed payload has header(title)+section(body) blocks to the configured URL; 5xx/timeout → `failed` + raises (retry); 4xx → `failed`, no raise; already-SENT → no-op; missing URL at send time → `failed` "no webhook". Mock `httpx.Client.post`.
- Green `poetry run pytest`; 5a/5b/5c suites unaffected.

---

## 7. Files

**New:** Alembic migration (`slack_webhook_url` column), `tests/sales/test_slack_dispatch.py`, `tests/workers/test_slack_notification.py`.
**Modify:** `app/db/models/sales_agent_config.py` (`slack_webhook_url` + `default_for`), `app/services/notification_service.py` (slack dispatch), `app/workers/sales_notifications.py` (the job), `app/workers/document_processor.py` (register the job).

---

## 8. Acceptance (PRD §3.6)

1. With Slack enabled and a webhook configured, each rep event posts a Block Kit message (title + body + dashboard link) to the tenant's Slack.
2. Slack delivery is async (ARQ), retries transient (5xx/timeout) failures, and gives up on a dead webhook (4xx) — never blocking the response or other channels.
3. No Slack row/post when the channel is off or the URL is unset; idempotent; tenant-scoped. Email/in-app + 5a–5c behavior unchanged. All new + existing tests green.
