# Stage 3 — AI Sales Agent · Slice 5e "CRM (generic webhook)" — Backend Design

**Date:** 2026-06-30
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · Alembic · ARQ · httpx · Fernet encryption · pytest)
**Paired FE spec:** `luran-dashboard/docs/superpowers/specs/2026-06-30-stage3-crm-webhook-dashboard-design.md`
**Builds on:** 5a (`NotificationLog`, `dispatch`, `_try_log`, `_EmailSendFailed` retry, ARQ registration), 5c (`_render_in_app_text`), 5d (the Slack ARQ-job pattern = the template here). `app/core/encryption.py` (`EncryptedString`, `encrypt_field`/`decrypt_field`). **Branch `feat/stage3-crm-webhook-backend` stacks on `feat/stage3-slack-notif-backend` (Slice 5d PR #39).**
**Decomposition:** Slice 5 = 5a Email ✅ → 5b PDF ✅ → 5c In-app ✅ → 5d Slack ✅ → **5e CRM (this — LAST)**.
**PRD:** decision-log #1 line 1581 ("Webhook-first — covers all CRMs, then add native HubSpot"); §2.3 line 287 ("CRM push works for HubSpot, Zoho, Salesforce (via webhook as fallback)"); §3.6 (CRM Integration config); lead flow lines 101–139.
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

When a lead crosses **sales-ready**, POST a lead payload to the tenant's configured **CRM webhook URL** (provider-agnostic — HubSpot/Zoho/Salesforce/Zapier/Make/custom). Generic webhook satisfies the PRD's webhook-first decision; native per-vendor clients are later slices. Backend half of the paired slice.

**Decisions (brainstorm):**
- **Generic outbound webhook** (PRD decision #1), not a native HubSpot API client and not the action-call automation engine — a dedicated CRM webhook on the notification spine (consistent with email/slack).
- **Trigger: `SALES_READY_LEAD` only** (the existing `crossed_sales_ready` moment) — one qualified push per lead. CRM does NOT fire on meeting/quote events.
- **`crm_webhook_url` stored ENCRYPTED at rest** via the existing `EncryptedString` type (CRM endpoints commonly embed a token).
- **`crm_id` write-back**: on a 2xx whose JSON carries an id, best-effort store it on `Lead.crm_id` (sync never depends on it).
- Delivered via an **ARQ job** with status-based retry (mirror 5d Slack exactly).

**In scope (BE):** encrypted `crm_webhook_url` config column + `"crm"` in `notify_channels` default; dispatch writes a `crm` row + returns a `sync_lead_to_crm` job on sales-ready when enabled+configured; the ARQ job POSTs the lead payload via httpx with retry + best-effort `crm_id` write-back.

**Out of scope:** native HubSpot/Zoho/Salesforce API clients; bidirectional sync; the action-call engine; CRM on meeting/quote events. The dashboard CRM toggle + URL field is the paired FE spec.

---

## 2. Config

- Add `crm_webhook_url` to `TenantSalesAgentConfig` as `Mapped[str | None] = mapped_column(EncryptedString, nullable=True)` (Fernet-encrypted Text; transparently decrypts on read so the `/sales/config` API returns plaintext to the dashboard). Additive migration (the column is `Text`; existing rows are NULL). `default_for` → `crm_webhook_url=None`.
- Add `"crm": False` to the `notify_channels` JSON default (existing rows without the key are treated as falsy via `.get("crm")`). `crm_provider` (existing) is left untouched — unused by the generic webhook.

---

## 3. Dispatch — write crm row + return job (sales-ready only)

Extend `NotificationService.dispatch`: when `event == NotifEvent.SALES_READY_LEAD` AND `channels.get("crm")` AND `config.crm_webhook_url` is set:
- Render `(title, body)` via `_render_in_app_text` (for the log row's display text; the job builds its own payload).
- `_try_log(channel=NotifChannel.CRM, recipient="crm", status=NotifStatus.PENDING, title, body, …)` — `recipient="crm"` sentinel → one crm push per `(tenant, SALES_READY_LEAD, lead)` via the dedupe index. Idempotent (savepoint).
- On a fresh insert (`log_id is not None`), append `(log_id, "sync_lead_to_crm")` to the returned `jobs`.
- Gate is event-scoped: meeting/quote dispatches never produce a crm row/job. Off OR no URL → nothing.

Email/Slack/in-app behavior + their job-returns unchanged.

---

## 4. CRM ARQ job

`sync_lead_to_crm(ctx, notification_log_id)` (`app/workers/sales_notifications.py`, registered in `WorkerSettings.functions`):
- Fresh `SessionLocal()`; load the `NotificationLog`; missing OR already `SENT` → return (idempotent).
- Load the `Lead` by `log.entity_id`. Load the tenant's `crm_webhook_url` **at send time** (decrypts via `EncryptedString`). No URL → mark `FAILED` "no crm webhook", return (no raise).
- Build the payload dict: `{lead_id, tenant_id, name, email, company, team_size, industry, intent, lead_score, status, source, utm, created_at}` (name/email read off the Lead decrypt transparently). `httpx.Client(timeout=10).post(url, json=payload)`.
- **Outcome mapping (mirror 5d):** 2xx → `SENT`; 5xx/network/timeout → `FAILED` + raise `_EmailSendFailed` (ARQ retries); 4xx → `FAILED`, no raise. `_EmailSendFailed` re-raised first by the outer handler (no double-commit). `finally: db.close()`.
- **`crm_id` write-back:** on 2xx, attempt `resp.json()`; if it's a dict with an id-ish key (`id` / `crm_id` / `contactId` / `vid`), set `lead.crm_id = str(that value)` before commit. Any parse failure / absent id → skip silently (the 2xx still marks `SENT`).
- **NEVER log the URL or the payload** (which contains PII) — log status/HTTP-code only.

---

## 5. Safety & invariants

- Crm row/job only on sales-ready when `notify_channels.crm` AND `crm_webhook_url` set; idempotent; tenant-scoped.
- Webhook URL encrypted at rest; never logged. Payload contains decrypted lead PII (name/email) — sent only to the tenant's own configured endpoint; never logged.
- Non-blocking: dispatch writes the row + returns the job; the POST runs in the worker. A CRM failure never affects the chat response or other channels.
- 5a–5d behavior unchanged.

---

## 6. Testing (pytest)

- **Dispatch crm**: sales-ready + `notify_channels.crm` on + url set → a `crm` row (PENDING, recipient "crm", title/body) AND a `sync_lead_to_crm` job; crm off → none; url missing → none; **non-sales-ready event (quote/meeting) → no crm row/job even with crm on**; idempotent (2nd → no new row/job).
- **ARQ job** (mock `httpx`): 2xx → `SENT`, POSTed payload includes the lead fields (email/company/lead_score) to the configured url; 2xx with `{"id": "..."}` → `lead.crm_id` set; 2xx with no id → `SENT`, crm_id unchanged; 5xx/timeout → `FAILED` + raises; 4xx → `FAILED`, no raise; missing-url → `FAILED`; already-SENT → no-op. Assert the URL/payload are NOT logged.
- **Encryption**: `crm_webhook_url` round-trips (set plaintext → DB stores ciphertext → read returns plaintext); a raw DB read shows it is not plaintext.
- Green `poetry run pytest`; 5a–5d suites unaffected.

---

## 7. Files

**New:** Alembic migration (`crm_webhook_url` column + `notify_channels` default note), `tests/sales/test_crm_dispatch.py`, `tests/workers/test_crm_sync.py`, `tests/sales/test_crm_webhook_encrypted.py`.
**Modify:** `app/db/models/sales_agent_config.py` (`crm_webhook_url` EncryptedString + `default_for` + `notify_channels` default `"crm": False`), `app/services/notification_service.py` (crm dispatch, sales-ready-only), `app/workers/sales_notifications.py` (`sync_lead_to_crm`), `app/workers/document_processor.py` (register).

---

## 8. Acceptance (PRD decision #1 / §2.3 / §3.6)

1. With CRM enabled and a webhook URL configured, a lead crossing sales-ready POSTs a lead payload (name/email/company/team_size/intent/lead_score/source/timestamp) to the tenant's CRM webhook — works for any provider/endpoint (HubSpot/Zoho/Salesforce/Zapier/custom), satisfying "CRM push … via webhook."
2. Delivery is async (ARQ), retries transient (5xx/timeout) failures, gives up on 4xx, and best-effort records the returned id in `Lead.crm_id` — never blocking the chat response or other channels.
3. The webhook URL is encrypted at rest and never logged; the PII payload is never logged. No crm push when the channel is off, the URL is unset, or the event isn't sales-ready; idempotent; tenant-scoped. 5a–5d behavior unchanged. All new + existing tests green.
