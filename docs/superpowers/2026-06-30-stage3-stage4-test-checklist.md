# Test Checklist — Stage 3 (AI Sales Agent) + Stage 4 (Website Auditor)

Covers everything implemented in both stages and every check verified during the live E2E.
Legend: **[A]** automated (Playwright / pytest), **[L]** live-data manual, **[R]** regression for a bug fixed during E2E.

---

## 0. Setup / preconditions

- [√] Stack up: `docker compose ps` → api/worker/db/redis/qdrant healthy; `GET /api/v1/health` → 200. **[R]** api shows **healthy** (not "unhealthy" — healthcheck curl→python fix).
- [√] Dashboard up: `cd luran-dashboard && npm run dev` → http://localhost:3000.
- [ ] Seed fixtures: `docker compose exec -e PYTHONPATH=/app api python scripts/seed_staging_e2e.py` → `staging-e2e-pro@luranai.com` / `StagingE2E123$` + platform admins.
- [ ] CRM/Slack receivers: a https://webhook.site URL (CRM) + a real Slack Incoming Webhook (or public echo like `https://postman-echo.com/post` for both).
- [ ] Sales config (dashboard → Grow → Sales): enable agent + lead capture + meeting booking + quotation; enable **Email + In-app + Slack + CRM**; set notification email, Slack URL, CRM webhook URL, pricing tiers, availability, annual discount. **Save.**
- [ ] **[R]** After save + reload, confirm the Slack URL, CRM URL, availability, and annual discount all **persisted** (the schema-drift bug — they used to silently vanish).

---

## 1. Stage 4 — Website Auditor

### Public auditor
- [√] **[A][L]** Submit a real public URL → audit runs, overall score shown.
- [√] **[A]** Limit-reached view renders on a 429 from the start call.
- [ ] **[L]** Worker `run_website_audit` completes (no traceback); report row created.
- [ ] **[L]** SSRF negative: submit `http://localhost` / `http://169.254.169.254` → rejected, not crawled.

### Dashboard auditor (Grow → Auditor)
- [√] **[A]** Completed audit appears in history with adaptive category tags; placeholder controls disabled.
- [√] **[A]** Edit modal opens + loads entry content; "Save changes" sends PATCH.
- [√] **[A]** "Confirm & go live" triggers import + confirmation.
- [√] **[A]** Gaps page lists gap card with disabled "Generate outline".
- [ ] **[L]** If contact captured, a `Lead` is created with `audit_report_id` set (auditor→lead link).

---

## 2. Stage 3 — Leads Core (Slice 1)

### Leads inbox (Grow → Leads)
- [ ] **[A]** Inbox renders stats cards + rows; Grow nav has the Leads link.
- [ ] **[A]** Stats cards show correct values (new-this-week, sales-ready count, pipeline value).
- [ ] **[A]** Filter-bar status tabs visible; Export CSV / Sync to CRM buttons disabled (as designed).

### Lead detail
- [ ] **[A]** Detail shows name heading + panels.
- [ ] **[A]** Status change sends PATCH with **lowercase** status (contract).

### Sales config form
- [ ] **[A]** Capabilities toggles render (Agent / Lead capture / Meeting booking / Quotation).
- [ ] **[A][R]** Meeting booking + Quotation toggles are **live** (no "AVAILABLE SOON" — those slices shipped).
- [ ] **[A]** "Save changes" sends PATCH; "Discard" resets the form.
- [ ] **[A]** Pricing tiers display ₦ NGN amounts.
- [ ] **[A][R]** Toggle switches have an accessible name (queryable by `role=switch` name — a11y fix).

---

## 3. Stage 3 — Chat-Widget Sales (Slice 2)

- [ ] **[L]** `POST /api/v1/chat/message` with `tenant_id` (null `session_id`) starts a session + AI replies.
- [ ] **[L]** Sales-intent message → `Lead` created with intent (one of the 7: SUPPORT/PRICING_INQUIRY/ENTERPRISE_PROSPECT/DEMO_REQUEST/PURCHASE_INTENT/OBJECTION/CHURN_RISK), company, team_size, email captured.
- [ ] **[L]** `Lead.name`/`Lead.email` are **encrypted at rest** (raw DB column shows ciphertext).
- [ ] **[L]** Scoring engine writes `lead_score` + `score_breakdown` (signals: email 20, company 10, team>50 15, enterprise 20, demo 15, competitor 10, ready-to-buy 25; sales-ready ≥ 86).
- [ ] **[L][R]** A clearly-sales message ("ready to buy, quote for N seats") reaches the **sales agent**, not a support action (router fix). A genuine support message ("check my order ORD-123") still routes to its action.

---

## 4. Stage 3 — Notifications + CRM (Slice 5: 5a–5e)

### Sales-ready dispatch (the payoff)
- [ ] **[L]** Drive a conversation until `lead_score` crosses **86** → dispatch fires.
- [ ] **[L]** `NotificationLog` rows for the lead, all `sent`:
  - [ ] **in_app** — written inline; appears in the dashboard NotificationBell (unread count + a11y label); mark-read works (5c).
  - [ ] **email** — `send_rep_alert` → real SMTP "Email sent successfully" (5a).
  - [ ] **slack** — `send_slack_notification` → POST with header + body + **"View in dashboard"** link → 2xx (5d).
  - [ ] **crm** — `sync_lead_to_crm` → **real lead-payload POST** to the webhook → 2xx; `Lead.crm_id` written back if the response carries an id (5e).
- [ ] **[L][R]** Idempotency: re-trigger the same sales-ready event → no duplicate `NotificationLog` rows.

### 5e CRM specifics
- [ ] **[A]** Encryption: `crm_webhook_url` round-trips (plaintext in/out, ciphertext in raw DB).
- [ ] **[A][L]** SSRF: CRM webhook set to `http://169.254.169.254/x` → job marks `FAILED "blocked webhook host"`, **no POST**, no crash.
- [ ] **[A]** Retry mapping: 2xx→SENT; 5xx/network/timeout→FAILED+retry; 4xx→FAILED no-retry; missing-url→FAILED; already-SENT→noop.
- [ ] **[A][L]** No URL or PII payload ever appears in worker/api logs.
- [ ] **[A]** Job registered in `WorkerSettings.functions`.

### Event-scoping (5e gate)
- [ ] **[L]** CRM fires **only** on sales-ready — **not** on meeting or quote events.

---

## 5. Stage 3 — Meeting Booking (Slice 3)

- [ ] **[L]** `GET /api/v1/chat/meetings/availability?conversation_id=…` returns real slots from the tenant's availability window (weekday/hours/slot/buffer/timezone respected; past slots excluded).
- [ ] **[L]** `POST /api/v1/chat/meetings` books a slot → `Meeting` row `scheduled`, `Lead.meeting_id` linked; 409 on taken/past slot; one live future meeting per lead (idempotent).
- [ ] **[L]** `MEETING_BOOKED` dispatch → **visitor confirmation email** + **rep alert email** + in-app + slack, all `sent`. (No CRM — correct.)
- [ ] **[A]** Slot computation: weekday filter (isoweekday), timezone, alignment, buffer (pytest `generate_slots`).

---

## 6. Stage 3 — Quotation (Slice 4 + 5b PDF)

- [ ] **[L]** In a sales conversation with team size known + quotation enabled, the agent emits `offer_quote` → a `Quote` is created.
- [ ] **[L]** `compute_quote` picks the right tier for the team size and applies `annual_discount_pct` (e.g. Enterprise 500k − 15% = 425k); correct currency.
- [ ] **[L]** `Quote` row created, `Lead.quote_id` linked.
- [ ] **[L]** `QUOTE_GENERATED` dispatch → **rep alert email with the quote PDF attached (5b)** + in-app + slack, all `sent`. (No CRM — correct.)
- [ ] **[A]** Pricing validation + currency formatting (pytest).

---

## 7. Cross-cutting / infra

- [ ] **[A]** Auth: tenant login + invalid-login message; platform-admin login.
- [ ] **[A]** Navigation: dashboard docs page loads.
- [ ] **[A]** Flows: create/delete a flow; routing + upgrade-guidance entry points.
- [ ] **[A]** Automation: action edit mode; temporary action delete; plan-limit + capability gates (`test_flow_action_call_regressions`).
- [ ] **[A]** Subscription: downgrade blocked-messaging; automation setup copy.
- [ ] **[A]** Explore Phase A: category nudge banner states; category PUT payload.
- [ ] **[L]** Worker: no unhandled tracebacks; ARQ jobs complete; retries only on injected failures.
- [ ] **[R]** dev DB schema matches models (no missing additive columns — `crm_webhook_url` etc.). Long-term: bring dev DB under alembic.

---

## 8. Open follow-ups (not blockers)

- [ ] Spine-wide SSRF **DNS-rebinding** hardening: migrate crm/slack/`flow_engine`/`url_scraper` outbound to `resolve_and_validate_host` + IP-pinning.
- [ ] 5a rep-alert email template `dashboard_url` defaults to `#` — inject `{FRONTEND_URL}/sales/leads` in `_entity_context`.
- [ ] Bring the dev DB (5432) under alembic so additive migrations don't silently drift.

---

## How everything was verified (reference)

- **Playwright (headed), all green:** auditor 11/11, sales-leads 15/15, auth/navigation/flows/automation/subscription/explore.
- **pytest:** full backend suite 749 passed; sales-config endpoint round-trip (incl. the 4-field regression); 5e dispatch/job/encryption (20+ tests).
- **Live data on the running stack:** real OpenAI chat → lead → score 100 → SALES_READY (all 4 channels incl. real CRM POST→2xx) → meeting (MEETING_BOOKED) → quote (QUOTE_GENERATED + PDF). Encryption-at-rest, SSRF block, and no-PII-logging confirmed in the real worker.
