# Live E2E — Stage 4 (Auditor) + Stage 3 (AI Sales Agent)

**Date:** 2026-06-30
**Goal:** Exercise both stages end-to-end with real data on the local full stack, catch + fix bugs before proceeding. Run in parallel: Claude drives an automated/API pass; you run the dashboard + chat UI pass; reconcile findings.
**Stack:** `develop` (luranai-api) local docker-compose (api+worker+db+redis+qdrant+adminer) + `staging` (luran-dashboard) `npm run dev`.

---

## 0. Preconditions (shared)

- [ ] Stack up: `docker compose ps` all healthy; `GET http://localhost:8000/health` (or `/docs`) responds.
- [ ] Dashboard up: `cd luran-dashboard && npm run dev` → http://localhost:3000.
- [ ] A tenant exists with an API key (note the `X-API-Key` / widget key) and a TENANT_ADMIN user to log into the dashboard.
- [ ] **CRM webhook receiver:** create a throwaway endpoint at https://webhook.site (copy its unique URL) — this is where 5e POSTs the sales-ready lead. Keep the page open to watch the POST land.
- [ ] Sales agent config (dashboard → Grow → Sales, or API): `agent_enabled=true`, `lead_capture_enabled=true`, `meeting_booking_enabled=true`, `quotation_enabled=true`. **Notify via:** enable **Email + In-app + Slack + CRM**. Set `notification_email`, a real Slack Incoming Webhook URL, and paste the webhook.site URL into **CRM webhook URL**. Save.
- [ ] (Email) confirm SMTP creds in `.env` reach a real inbox you can check, or watch the worker logs for the send.

---

## 1. Stage 4 — Website Auditor

- [ ] **Public audit:** submit a real URL to the public auditor tool (dashboard public page or `POST /api/v1/audit` per the auditor contract). Use a normal public site.
- [ ] Worker runs `run_website_audit` (watch `docker compose logs -f worker`): crawl + discovery complete, **no SSRF block on a legitimate public URL**, report row created.
- [ ] **SSRF negative check:** submit `http://localhost` / `http://169.254.169.254` as the audit URL → must be rejected/blocked (not crawled). (Confirms `resolve_and_validate_host` on the auditor path.)
- [ ] Audit report visible in the dashboard (Grow nav → Auditor) with findings.
- [ ] **Auditor→lead link:** if the audit captures contact info, a `Lead` is created with `audit_report_id` set (check via adminer / `GET /api/v1/leads`).

---

## 2. Stage 3 — Sales Agent: lead capture → scoring

- [ ] **Chat conversation** (widget or `POST /api/v1/chat` against the tenant): drive a sales-intent conversation ("I want pricing for an enterprise plan, ~40 seats"). Confirm intent detection fires a trigger intent (PRICING_INQUIRY / DEMO_REQUEST / etc.).
- [ ] Lead captured: a `Lead` row with `name`/`email` (encrypted at rest — verify ciphertext in adminer), `company`, `team_size`, `intent`, `source`, `utm`.
- [ ] Qualification questions asked + answers stored.
- [ ] **Scoring:** `lead_score` + `score_breakdown` populated. Drive the conversation until the score **crosses sales-ready (≥86)** — this is the dispatch trigger.

---

## 3. Stage 3 — Sales-ready → notifications + CRM (the 5a–5e payoff)

When the lead crosses sales-ready, dispatch fires for every enabled channel. Verify each:

- [ ] **In-app (5c):** a `NotificationLog` row, channel `in_app`, status `SENT` (written inline), with title/body. Appears in the dashboard NotificationBell (unread count + a11y label). Mark-read works.
- [ ] **Email (5a):** `NotificationLog` channel `email` → worker `send_rep_alert` → status `SENT`; rep-alert email arrives at `notification_email`. *(Known follow-up: the email CTA links to `#` — the `dashboard_url` template bug; note if you see it.)*
- [ ] **Slack (5d):** channel `slack` → `send_slack_notification` → Slack message posts with header + body + **"View in dashboard"** link → status `SENT`.
- [ ] **CRM (5e):** channel `crm`, recipient `crm` → `sync_lead_to_crm` → **a POST lands at your webhook.site URL** with the lead payload (`lead_id, tenant_id, name, email, company, team_size, industry, intent, lead_score, status, source, utm, created_at`) → status `SENT`. If your receiver returns JSON with an `id`, confirm `Lead.crm_id` gets written back.
- [ ] **CRM SSRF (5e):** temporarily set the CRM webhook URL to `http://169.254.169.254/x` and re-trigger → the log goes `FAILED` "blocked webhook host", **no POST**, no crash. Reset it after.
- [ ] **Idempotency:** re-trigger the same sales-ready event → no duplicate `NotificationLog` rows (dedupe index holds).

---

## 4. Stage 3 — meeting booking + quotation

- [ ] **Meeting (Slice 3):** book a demo via chat/dashboard → `Meeting` row, slot validation + timezone correct → `MEETING_BOOKED` dispatch → visitor confirmation email + rep alert. `Lead.meeting_id` set.
- [ ] **Quote (Slice 4):** generate a quotation → `Quote` row with line items + currency + total → `QUOTE_GENERATED` dispatch → rep alert email **with the PDF attached (5b)**. `Lead.quote_id` set.

---

## 5. Worker / health checks (shared)

- [ ] `docker compose logs worker` — no unhandled tracebacks; ARQ jobs complete; retries only on injected failures.
- [ ] No webhook URL or PII (lead email/name) appears in any worker log line (5e logging invariant).
- [ ] All `NotificationLog` rows end `SENT` (or `FAILED` only where you injected a failure).

---

## Findings log (reconcile here)

| # | Stage/Slice | What | Severity | Owner | Status |
|---|-------------|------|----------|-------|--------|
| 1 | Env / 5e | Dev DB (5432) missing `crm_webhook_url` — never under alembic, `create_all` doesn't ALTER existing tables. Only that one column had drifted (notification_logs 5c fields + leads FKs were present). | Medium (env, not code) | Claude | **FIXED** — `ALTER TABLE … ADD COLUMN crm_webhook_url TEXT`. Proper fix pending: bring dev DB under alembic (entrypoint `alembic upgrade head`, or stamp head). |
| 2 | Infra / dev compose | `docker-compose.yml` api healthcheck used `curl -f`, but the runtime image has no `curl` (only the builder stage installs it) → api always reported `unhealthy` despite serving 200. Prod compose already used a python probe; dev was stale. | Medium (dev only; prod unaffected) | Claude | **FIXED** (commit) — dev healthcheck now uses the same `python -c urllib.request.urlopen('…/api/v1/health')` probe as prod; api now reports `healthy`. |

## Claude's automated backend pass — results (5e, live stack)

- ✅ Stack healthy (api/db/redis/qdrant healthy, worker up); `/health` 200.
- ✅ Worker registered `sync_lead_to_crm`.
- ✅ Schema matches models after finding #1 fix.
- ✅ Encryption-at-rest: `crm_webhook_url` ciphertext in raw DB; plaintext/token absent.
- ✅ SSRF block in real worker: `169.254.169.254` → `FAILED "blocked webhook host"`, no POST.
- ✅ No-leak: secret token never appeared in api/worker logs.
- ⏳ Real public POST + `crm_id` write-back → **user lane** (webhook.site; SSRF guard correctly refuses a local receiver).
