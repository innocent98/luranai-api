# SOP: quote PDF naira sign, lead score hold, rep-alert deep link (2026-10-30)

## What shipped
Three defects found by the sales-agent smoke run (`docs/sop/sales_agent_smoke_2026-10-30.md`, defects 1-3), fixed in one PR. WhatsApp quote delivery (defect 4) is NOT part of this; the Offerings catalog plan covers it.

## Why and how
| # | Problem (root cause) | Fix |
|---|---|---|
| 1 | The quote PDF printed a black square for the naira sign. `quote_pdf.html` used base Helvetica (WinAnsi), which has no U+20A6; NGN is the default currency. | Embed DejaVu Sans (regular + bold) in the PDF. Files live in `app/assets/fonts/` (subsets of DejaVu 2.37, about 65 KB each, `LICENSE_DEJAVU` and a `README.md` with source hashes and the subset command beside them). They ship with `COPY ./app /app/app`, are read at render time by the non-root `appuser` (read-only is enough), and nothing is fetched at runtime. `quote_pdf_service._font_context()` passes the paths to the template's `@font-face`; if the files are missing it logs one warning and renders the old Helvetica PDF instead of crashing. Subset covers Basic Latin, Latin-1, Latin Extended-A/B, combining marks, Latin Extended Additional (Yoruba diacritics), punctuation, the whole Currency Symbols block and the minus sign. Rejected: Noto Sans (larger, no practical gain here); fetching a font at runtime (new failure mode, network at render). |
| 2 | A later chat turn lowered a lead that had already reached sales-ready (100 -> 70, tier `hot`). `enrich_from_chat` re-scored from only that turn's signals. | `LeadService.apply_score(..., hold_peak=True)` for chat turns: the stored score and its breakdown are kept unless the new score is at least as high. Explicit rescoring (`recompute`, `POST /leads/{id}/score`) still replaces the figure, so a rep can lower it and later turns then hold the new value. Status is untouched by chat turns (only `set_status` moves it, forward only), so a rep's status change or `converted` is never overridden. `crossed_sales_ready` stays False on a held score, so there is no second alert (the notification unique index still guards forced dispatches). |
| 3 | Rep alert email "View in Dashboard" pointed at `#`; the Slack link pointed at `/sales/leads`, which is not a dashboard route. | `app/sales/links.py::lead_dashboard_url(lead_id)` builds `{FRONTEND_URL}/dashboard/grow/leads/{lead_id}` (route `app/(dashboard)/dashboard/grow/leads/[leadId]/page.tsx` in luran-dashboard). Same setting the other emails use, **no new env var**. Email context gets it for lead, meeting and quote alerts; Slack uses the same helper (a small addition to the ask: same bug class, same helper). No base URL: the button is hidden (never `#`). |

## What's involved
`app/services/quote_pdf_service.py`, `app/templates/emails/quote_pdf.html`, `app/assets/fonts/*`, `app/services/lead_service.py`, `app/sales/links.py`, `app/workers/sales_notifications.py`, `app/templates/emails/sales_rep_alert.html`. No migration, no config change, no FE change.

## Verification
- Tests first (red), then green: `tests/sales/test_quote_pdf.py` (naira text present, no box, font embedded via `/FontFile2`, USD/GBP/EUR/NGN symbols, Yoruba name, missing-font fallback logs once), `tests/sales/test_lead_score_hold.py` (raise, hold, higher recompute replaces, rep rescore honoured, status never overridden, no second alert, re-crossing after a rep drop), `tests/sales/test_lead_dashboard_link.py` (link from base URL + lead id, empty base falls back, template, lead/meeting/quote contexts, Slack).
- Local checks: `ruff check app/`, `black --check app/` clean; 789 tests pass over sales, workers, leads/meeting/notification/sales-config API, entitlements and auditor suites.
- Live rerun of the smoke harness on the fixed code: 74/74 checks (`docs/fe-integration-captures/sales-agent-fixes-2026-10-30/run-output.txt`): PDF text has `₦120,000` with DejaVu embedded (`quote-attachment-page1.png` viewed: naira and bold render), the lead stays 100 / `sales_ready` after the demo-request turn, every rep email and the Slack message link to `/dashboard/grow/leads/<lead id>` (`raw/rep-email-links.json`).
- Not proven: prod image render on Linux (fonts are plain files, reportlab reads them the same way), a real inbox, the real dashboard route opening in a browser.

## Operate / roll back
Nothing to set: no new env name (`FRONTEND_URL` already drives the link; confirm it is the dashboard origin in staging/prod). Revert the PR to roll back; an existing lead's stored score is never rewritten by the deploy.

## Follow-ups
Scores of leads already lowered before this deploy are not restored. WhatsApp quote/picker delivery remains (catalog plan).
