# SOP — Auditor redirect canonicalization (apex→www)

**Status:** Shipped · PR #64 → `develop` (merge `03928c1`) · 2026-07-25
**Area:** `app/auditor` + public/dashboard auditor endpoints + one migration

---

## What shipped

The website auditor now **follows host-canonicalization redirects** (apex→www, http→https)
instead of silently choking on them, and requires **explicit confirmation** before scanning a
genuinely different host. It records the URL actually scanned and only spends a public scan
credit when an audit is really enqueued.

## Why (root cause)

Auditing the apex `luranai.com` returned only **3 pages** and reported real content as "gaps".
`luranai.com` **307-redirects to `www.luranai.com`**, and the auditor didn't canonicalize that.
Two compounding failures:

1. **Sitemap seeding died.** `discovery._default_fetch_text` uses `follow_redirects=False`
   (deliberate SSRF guard) and returns text only on HTTP 200 → the 307 yielded `None` → all 19
   sitemap URLs were discarded; only the root was seeded.
2. **The crawl's own links were filtered out.** The render followed the redirect to `www`, but
   same-host filtering keys off the first seed's host (the apex), so every `www.` link was
   dropped as off-host — leaving only home/pricing/privacy.

Not stale code: `main` had every prior auditor fix. Both auditors share ONE engine
(`AuditService` → `app.auditor.pipeline`); only the page budget differs (public=10,
dashboard=plan-based, Growth=500) — so this was never a budget problem.

## How it works (tiered handling)

At submit time (`AuditService.start_audit`), the entered root's redirect chain is resolved
**once, safely, bounded**, then classified:

| Tier | Redirect | Behavior |
|------|----------|----------|
| 1 — same site | `www`/scheme toggle only (e.g. `x.com`→`www.x.com`) | Followed **silently**; scan the canonical host; record `entered_url` |
| 2 — different host | different subdomain/domain (e.g. `x.com`→`app.x.com`, `oldco.com`→`newco.io`) | Return **409 `REDIRECT_CONFIRMATION_REQUIRED`**, create **no rows**; client re-submits `confirmed_target`; server **re-resolves + re-compares** (never trusts client) |
| 3 — unsafe | internal-IP hop, loop, or > `max_hops=3` | **400** blocked |

Key components (all in `app/auditor/`):
- `discovery.resolve_redirect_chain(url, *, max_hops=3)` — the ONLY new redirect follower.
  Re-guards **every hop** via `resolve_and_validate_host`, pins the connection to the validated
  IP, keeps `follow_redirects=False` per hop, and **fails closed** (transport error → `ValueError`).
- `discovery.classify_redirect(entered, final)` → `"same_site"` (only a leading `www.` and/or
  scheme differs, same port) or `"different_host"`.
- `service.start_audit(..., confirmed_target=None)` — resolves, normalizes the final URL once,
  classifies, gates. For `different_host` it raises `RedirectConfirmationRequired` **before** any
  row is created; a `confirmed_target` is accepted only when it matches a **fresh** server-side
  re-resolution (`normalize_url(confirmed_target) == final_url`).

### Rate limiting (public tool)
- The 3/day **scan credit** (`public_audit_limiter`) is now spent **only after a successful
  enqueue** — a 409 confirmation prompt or a 400/503 no longer burns one.
- A generous **30/day per-IP anti-abuse limiter** (`public_audit_abuse_limiter`) is the endpoint
  dependency, so the redirect pre-flight can't be an unmetered probe.
- Both limiter calls use the codebase's `RATE_LIMIT_FAIL_OPEN` convention.

## Key decisions

- **Tier 1 is silent, not confirmed.** apex↔www is the same site's canonical address and the most
  common redirect on the web; prompting there would breed confirm-fatigue and devalue the Tier 2
  prompt that actually carries a consent/security signal.
- **Cross-domain redirects are confirmed + transparent, NOT refused.** The public tool already
  lets anyone scan any public domain by typing it directly, so a redirect grants no new
  capability; refusing would break legitimate rebrands/vanity domains/agency audits for near-zero
  security gain. Internal-target hops remain blocked (Tier 3).
- **Never trust the client's `confirmed_target`** — the server re-resolves and re-compares; a
  stale/mismatched value re-prompts and never scans.

## What's involved (files touched)

| File | Change |
|------|--------|
| `app/db/models/audit.py` | `AuditReport.entered_url` (nullable `String(2048)`) |
| `alembic/versions/aa10_add_entered_url_to_audit_reports.py` | additive migration (down_revision `b7d21c9f3a04`) |
| `app/auditor/discovery.py` | `ResolvedRoot`, `_guarded_get`, `resolve_redirect_chain`, `classify_redirect`, `_same_registrable_host` |
| `app/auditor/service.py` | `RedirectConfirmationRequired`; canonicalization + confirm gating in `start_audit` |
| `app/schemas/auditor.py` | optional `confirmed_target` on both audit requests; `entered_url` on `PublicReportResponse` |
| `app/api/v1/endpoints/public_auditor.py` | 409 mapping; scan-credit-on-enqueue; anti-abuse limiter; expose `entered_url` |
| `app/api/v1/endpoints/auditor.py` | 409 mapping; pass `confirmed_target` |

API is additive on `/api/v1` (new optional field, new 409 code, nullable column).

## Verification

- `tests/auditor/` — **259 passed**, ruff clean. New test files: `test_redirect_resolver.py`,
  `test_redirect_classifier.py`, `test_start_audit_redirect.py`, `test_public_auditor_redirect.py`.
  (2 files excluded from the local run — `test_chat_grounding.py`, `test_service_import.py` — hang
  on a pre-existing tiktoken first-use network download, unrelated; CI runs them.)
- TDD per task; each task reviewed for spec + quality; whole-branch opus review before merge.
- **Merge-blocker caught in final review:** the confirm comparison was asymmetric (normalized the
  client value but compared to a raw `final_url`), so a different-host redirect to a path-bearing
  target (trailing slash/query) would re-prompt forever. Fixed by normalizing `final_url` once
  after resolution (`fix df3c16b`) with a path-target regression test.

## Operate / roll back

- Migration is additive + nullable + reversible (`downgrade` drops the column). No backfill.
- **Immediate user workaround** for a redirecting apex: audit the canonical host directly
  (e.g. `www.luranai.com`) — it seeds all sitemap URLs.
- Deploys to `develop`'s environment via `cd.yml` on merge.

## Follow-ups

- **Frontend — SHIPPED (2026-07-26):** confirm modal on both auditors consuming the 409, and the
  "Scanned www.luranai.com (you entered luranai.com) · N pages reached" transparency display. See
  `luran-dashboard/docs/sop/auditor_redirect_confirm_ui.md`.
- **`entered_url` exposure — SHIPPED:** the dashboard `list_audits` rows now carry `entered_url`,
  and the public report GET now returns `url` (needed by the FE transparency line). Landed on this
  same branch alongside the FE work. **Deploy ordering:** these fields must reach production
  first-or-together with the FE, else the public report renders "Scanned undefined."

Design spec: `docs/superpowers/specs/2026-07-25-auditor-redirect-canonicalization-design.md`
Plan: `docs/superpowers/plans/2026-07-25-auditor-redirect-canonicalization-backend.md`
