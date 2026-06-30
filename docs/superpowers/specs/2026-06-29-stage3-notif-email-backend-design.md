# Stage 3 — AI Sales Agent · Slice 5a "Notification Core + Email" — Backend Design

**Date:** 2026-06-29
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · Pydantic · Alembic · pytest · ARQ · SMTP `EmailService`)
**Builds on:** Slice 1 (`Lead`/`LeadService`/`apply_score`), Slice 3 (`Meeting`, `POST /chat/meetings`), Slice 4 (`Quote`, chat quote block). **Branch `feat/stage3-notif-email-backend` stacks on `feat/stage3-quote-backend` (Slice 4 PR #34).**
**Decomposition:** Slice 5 (Notifications + CRM) = 5a Notification core+Email (this) → 5b Quote PDF → 5c In-app → 5d Slack → 5e CRM. This sub-slice builds the dispatch spine the others extend.
**PRD:** `docs/luran_prd.md` §3.4 (rep alert + visitor confirmation), §3.5 (quote delivery), §3.6 (`notify_channels`), acceptance §2.3.
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Notify the sales team (and the visitor) when the AI produces something actionable. Three domain events — **lead becomes sales-ready**, **meeting booked**, **quote generated** — enqueue ARQ jobs that send **email** notifications gated by the tenant's `notify_channels`. Backend-only; this is the **dispatch spine** that 5b–5e extend with PDF/in-app/Slack/CRM.

**In scope (5a):** a `NotificationService.dispatch` spine; a `NotificationLog` model (idempotency + audit + seed for 5c in-app); triggers wired into the meeting-booking, quote, and lead-scoring paths; ARQ email jobs (rep alert + visitor meeting-confirmation); email templates.

**Out of scope (later sub-slices):** quote PDF generation/email (5b); in-app feed + dashboard UI (5c); Slack (5d); CRM sync (5e). No dashboard/widget changes in 5a.

**Constraint:** notification work is **non-blocking** — `dispatch` only enqueues; SMTP runs in the ARQ worker. Failure never breaks the chat/booking response (try/except, same discipline as Slices 2–4).

---

## 2. Events & trigger conditions

| event | fires when | recipients (5a email) |
|---|---|---|
| `sales_ready_lead` | a lead's score **first** reaches ≥ 86 (sales_ready tier) | rep alert |
| `meeting_booked` | a `Meeting` is created (`POST /chat/meetings`) | rep alert + **visitor** confirmation |
| `quote_generated` | a `Quote` is persisted (chat quote block) | rep alert |

`sales_ready_lead` is **deduped** — only the first crossing notifies (enforced by the `NotificationLog` unique constraint, §4). Re-scoring the same lead does not re-alert.

---

## 3. Trigger seams (where `dispatch` is called)

- **meeting_booked** → in `MeetingService.book` (or the `POST /chat/meetings` handler) after the `Meeting` is committed.
- **quote_generated** → in the chat quote block (`app/api/v1/endpoints/chat.py`) after `QuoteService.create_quote`.
- **sales_ready_lead** → in `LeadService.apply_score`: when the freshly computed score ≥ 86 AND the prior score was < 86 (or null). Covers chat + auditor lead paths (both score through `LeadService`).

Each call is wrapped in try/except — a dispatch error logs and is swallowed; the primary response is unaffected.

---

## 4. `NotificationLog` model

New table `notification_logs` (`app/db/models/notification_log.py`):

| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK tenants, indexed | tenant scope |
| `event` | str enum `sales_ready_lead`\|`meeting_booked`\|`quote_generated` | lowercase |
| `channel` | str enum `email`\|`in_app`\|`slack`\|`crm` | lowercase (full set defined now; 5a only emits `email`) |
| `entity_type` | str (`lead`\|`meeting`\|`quote`) | |
| `entity_id` | uuid, indexed | |
| `recipient` | str | resolved address / "rep"/"visitor" kind for in-app later |
| `status` | str enum `pending`\|`sent`\|`failed` | |
| `error` | text, nullable | last failure detail |
| `created_at` | timestamptz | |

**Unique index** `(tenant_id, event, channel, entity_id, recipient)` → idempotency: a duplicate `dispatch` finds the existing row and skips enqueuing. Additive migration.

---

## 5. Dispatch spine

`app/services/notification_service.py` → `NotificationService(db)`:
- `dispatch(tenant_id, event, *, entity_type, entity_id, recipients) -> None`:
  - Load `TenantSalesAgentConfig`; read `notify_channels`.
  - For each channel where `notify_channels[channel]` is true AND a handler exists for it in this sub-slice (5a: `email` only):
    - For each recipient kind applicable to the event (rep; visitor only for `meeting_booked`): attempt to INSERT a `NotificationLog` (status `pending`). On unique-constraint conflict → already dispatched, skip. On fresh insert → enqueue the channel's ARQ job with the log id.
  - Async enqueue uses the existing pattern: `redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL)); await redis.enqueue_job("send_rep_alert", str(log_id))`. (The chat handler + meeting endpoint are `async`, so enqueue is awaited there; `apply_score` is sync — see §8 note on how the sync path enqueues.)

**Recipient resolution (rep):** `config.notification_email` → else the tenant's admin user email → else skip (log `failed` with "no recipient" or don't create the row).

---

## 6. ARQ email jobs (`app/workers/`, registered in `WorkerSettings.functions`)

- `send_rep_alert(ctx, notification_log_id)` — fresh worker DB session: load the `NotificationLog` + its entity (lead/meeting/quote); render the rep-alert template (event-parameterized); `EmailService.send_templated_email(...)` to the recipient; set log `sent`/`failed`. ARQ `max_tries` retries transient SMTP errors; final failure → `failed` + error.
- `send_visitor_meeting_confirmation(ctx, notification_log_id)` — emails the lead's email the booked-slot confirmation (visitor-local time string from the meeting).

---

## 7. Email templates

Add HTML templates rendered via `EmailService.render_template` (reuse the existing email layout/styling):
- **rep-alert** — one template, event-parameterized: heading + a short summary block (sales-ready lead → name/company/score; meeting → slot + lead; quote → total + lead) + a "View in dashboard" link.
- **visitor meeting-confirmation** — "You're booked — {weekday} {date}, {time}" + brief details.

---

## 8. Safety & invariants

- Tenant-scoped throughout (`NotificationLog.tenant_id`).
- Idempotent dispatch via the unique constraint — no double-sends across re-scores/re-dispatches.
- Non-blocking: dispatch enqueues only; SMTP in the worker; all trigger calls in try/except.
- Channel gating by `notify_channels`; email needs a resolvable recipient.
- **Sync-path enqueue note:** `LeadService.apply_score` is sync and cannot `await create_pool`. Resolution: `apply_score` records that a sales-ready crossing occurred (returns a flag / sets it on the lead), and the **async caller** (chat handler, auditor task) performs the `dispatch`/enqueue after the sync work — OR `dispatch` uses ARQ's sync enqueue (`arq` supports creating a pool in a short-lived event loop). The plan picks one; default: the async caller dispatches using the crossing flag from `apply_score`, keeping all enqueues on async seams.
- No PII beyond what the lead already stores; recipient addresses are tenant-configured or the lead's own email.

---

## 9. Testing (pytest)

- **dispatch**: enqueues one job per enabled channel + writes a `pending` log; second identical dispatch → unique-constraint hit, no new job/row (idempotent); `notify_channels.email=false` → no email job; no recipient → no rep row/job.
- **triggers**: meeting-booked / quote-generated / sales-ready-crossing each dispatch once; sales_ready only on the **first** crossing (<86 → ≥86), not on subsequent re-scores or when already ≥86.
- **jobs**: `send_rep_alert` recipient fallback (config email → tenant admin → skip); renders the correct event variant; `send_visitor_meeting_confirmation` → lead email; log status `pending`→`sent`/`failed`; mock `EmailService._send_email`/`send_templated_email`.
- Green `poetry run pytest`; full suite no regression (Slices 1–4 untouched behaviorally).

---

## 10. Files

**New:** `app/db/models/notification_log.py`, Alembic migration, `app/services/notification_service.py`, `app/workers/sales_notifications.py` (the ARQ jobs), email templates (rep-alert + visitor-confirmation), `tests/sales/test_notification_dispatch.py`, `tests/workers/test_sales_notifications.py`.
**Modify:** `app/services/lead_service.py` (sales-ready crossing flag), `app/api/v1/endpoints/chat.py` (quote dispatch) + `app/services/meeting_service.py` or the meetings endpoint (meeting dispatch), `app/workers/document_processor.py` (register the new jobs in `WorkerSettings.functions`), `app/db/models/__init__.py`.

---

## 11. Acceptance (PRD §2.3 / §3.4 / §3.5)

1. Booking a meeting emails the rep an alert AND the visitor a confirmation (when `notify_channels.email` on).
2. A generated quote emails the rep an alert.
3. A lead crossing sales-ready emails the rep — once (deduped), not on every re-score.
4. All sends are non-blocking (ARQ) and never break the chat/booking response; failures are logged, not raised.
5. `notify_channels.email=false` or no recipient → no email. Idempotent (no double-sends). All new tests green; Slices 1–4 unaffected.
