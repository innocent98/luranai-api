# SOP: AI Sales Agent smoke re-verification (local), 2026-10-30

## What shipped
Evidence only, no product code changed. The Stage 3 journey (chat -> qualification -> lead + score -> sales-ready -> notifications -> meeting -> quote, plus plan gating) was re-run from scratch against the real code at `origin/develop` `1391403`, with a one-command-per-process harness. 69/69 live checks passed; 396 existing tests passed. It also found 4 real defects/gaps (below). Captures: `docs/fe-integration-captures/sales-agent-smoke-2026-10-30/` (`run-output.txt`, `results.json`, numbered request/response files, `raw/`, `quote-attachment.pdf`).

## Why
The sales agent was declared complete and live-tested in June-July 2026 and is in prod, but nothing had re-checked it since; the owner is about to onboard pilots and build an Offerings catalog that feeds the quote path.

## How
Local stack, own throwaway containers on free ports (Postgres 15, Redis 7, Qdrant, Mailpit as the email stub; none shared with 55432 or the VPS). Real API (uvicorn) and real arq worker, DB migrated to `wa40contactexclusion (head)`, plans seeded with `scripts/seed_data.py`, tenants created through `/auth/register-company`. Stand-ins (all in `harness/`): `standins.py` is an OpenAI-compatible server (the app's real OpenAI client is pointed at it with `OPENAI_BASE_URL`, so the structured-output request, parsing and token accounting are real; only the model's decision is scripted) plus a CRM/Slack receiver. `run_worker.py` is `python worker.py` with ONE relaxation: `127.0.0.1` passes the outbound SSRF guard so the worker can reach the local receiver (link-local and private ranges stay blocked; the unmodified guard is also exercised). `drive.py` runs the journey. The earlier June run had no committed harness (real LLM + webhook.site by hand), so there was nothing to re-run as-is.

## Verified live (local, real code)
| Area | Result |
|---|---|
| Config save (webhook URLs, availability, tiers, notify channels) | Round-trips on re-read (the June drift bug has not returned) |
| Intent / directive wiring | Request to the model carries the tenant's first qualification question, `## Available Plans`, 20% annual discount line, "scheduling IS available"; lead created on the first trigger-intent turn |
| Qualification answers, lead, score | team size and industry captured (score 5, cold); name/email/company captured, score 100, `sales_ready` on the buying turn; PII (`email`) and CRM URL are ciphertext in the DB |
| Notifications on sales-ready | email (Mailpit), in-app, Slack (Block Kit + dashboard link), CRM (exact lead payload at the receiver; `Lead.crm_id` written back); one row per channel; a second crossing of 86 does NOT re-dispatch |
| Quote | Total computed in code from `pricing_tiers` (the model never supplies a price): 40 users on Professional = 150,000 - 20% = 120,000 NGN; 60 users = (150,000 + 10 x 3,000) - 20% = 144,000; row persisted and linked; email + in-app + Slack fired; PDF attached to the rep email and is a valid PDF |
| Meeting | availability slots (Africa/Lagos window, 30-min grid), booking, past slot 409, another lead on a taken slot 409 `SLOT_UNAVAILABLE`, booked slot (+ buffer) removed from the list, visitor confirmation email + rep email |
| Plan gating | Lite: enabling quotes/booking or Slack/CRM -> **403** `{"code":"feature_not_in_plan","feature":"sales_close","min_tier":"professional",...}` (capture 39); with the stored switches left on (a downgrade), chat gives no quote card and no picker, directive says scheduling is NOT available, availability = `[]`, booking 403 `BOOKING_DISABLED`, no quote row; lead capture, scoring and email/in-app alerts still work, Slack/CRM do not fire |
| SSRF guard | unmodified code blocks loopback and 169.254.169.254; a live CRM job aimed at link-local ends `failed` "blocked webhook host" with no POST |
| Logs | no tracebacks; no lead email or webhook URL in INFO-level worker/API lines |
| Tests | 396 passed in 33.8 s: `tests/sales`, the chat lead/quote/sales-wiring, leads, meeting, notification and sales-config API tests, `tests/entitlements`, `tests/workers/test_crm_sync.py`, `test_sales_notifications.py` (`pytest-sales-summary.txt`) |

## Findings
UPDATE 2026-10-30: defects 1-3 are fixed in [PR #194](https://github.com/innocent98/luranai-api/pull/194) (`fix/sales-quote-pdf-score-link`; SOP `docs/sop/sales_quote_pdf_score_link_fixes.md`; live rerun 74/74 with the PDF page viewed, the lead held at 100 `sales_ready`, rep email and Slack linking to `/dashboard/grow/leads/<id>`) once it merges. Defect 4 is open (catalog plan). The table below is the state at discovery.

| # | Severity | What | Evidence |
|---|---|---|---|
| 1 | Medium | **Quote PDF prints a black square instead of the naira sign** (`■150,000`). Default currency is NGN; the template uses base Helvetica (WinAnsi, no U+20A6). Deterministic, not OS-specific. The existing test only checks it is a PDF. | `quote-attachment.pdf`, `quote-attachment-page1.png`, `pdffonts` shows Helvetica Type 1 WinAnsi, not embedded |
| 2 | Low-Med | **Lead score is not sticky.** It is recomputed from the CURRENT turn's signals, so the same lead went 100 (`sales_ready`) -> 70 (`hot`) after a plain demo-request turn, then back to 100. Alerts already fired and the dedupe holds, but the leads inbox tier/filter can lose a lead that already alerted. | captures 16, 29, 33 |
| 3 | Low | Rep-alert emails have `View in Dashboard` pointing at `#` (open since June). | `raw/emails-summary.md` (redacted; the full bodies were removed from the repo) |
| 4 | Gap | **WhatsApp**: only `response.message.content` is forwarded (`app/services/whatsapp_inbound.py:402-417`, `:650-669`). The quote card and meeting picker live in `meta` and never reach the customer, while the reply text refers to them ("here is your quotation", "pick a time below"). Leads from WhatsApp are also stamped `source="chat_widget"` (`app/api/v1/endpoints/chat.py:2954`). Code-read, not run live. | capture 15 (quote in `meta`, text only in `content`) |
Also noted: a brand-new tenant is `first_responder=human`, so the sales agent is silent (visitor escalated "Human-only mode", no lead) until the admin switches to AI; pilots must be told (captures 02, 59). A lead keeps one meeting: picking a different slot afterwards returns the original (200), no reschedule (capture 26). `slack_webhook_url` is plaintext in the DB (only the CRM URL is encrypted). DEBUG logging (dev only; prod runs INFO) prints the visitor's message including email.

## NOT proven (a local run of the real code)
Staging/prod config and data (SMTP creds, `FRONTEND_URL`, plan rows on the VPS); the real model (intent quality, one-question pacing, no `[insert URL]` placeholders, strict JSON-schema adherence on `gpt-6-luna`); a real inbox, real Slack, a real CRM (receiver was a stand-in; the guard was relaxed for loopback in the worker only); the browser UI (luran-chat renders the quote card/picker; dashboard leads inbox); WhatsApp sidecar end to end; a real knowledge base (the run had zero sources); the production image on Python 3.11 Linux; concurrency.

## Operate / roll back
Nothing deployed. Stack torn down (processes stopped, containers removed, throwaway env and keys deleted). To re-run: start Postgres/Redis/Qdrant/Mailpit on free ports, migrate, seed plans, start `standins.py`, uvicorn and `run_worker.py` (with `SMOKE_ALLOW_LOOPBACK=1`), then `PYTHONPATH=. poetry run python docs/fe-integration-captures/sales-agent-smoke-2026-10-30/harness/drive.py` with `OPENAI_BASE_URL=http://127.0.0.1:58090/v1`, `SMTP_HOST/PORT` pointed at Mailpit, `ENVIRONMENT=development`.

## Follow-ups
Defects 1-3: done in #194. WhatsApp quote/picker plan (send the quote as text; no picker over WhatsApp) before pilots use WhatsApp for sales; verify with the real model and a real inbox on staging when credentials allow.
