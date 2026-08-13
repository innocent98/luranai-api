# SOP — Auditor honest BLOCKED state (frontend) + non-blocking subresource DNS

**Status:** Code-complete 2026-08-11 · PRs pending (luran-dashboard FE + luran BE)
**Area:** `luran-dashboard/app/**/auditor/**` (FE) · `luran/app/auditor/crawler.py` (BE)
**Follows on from:** [`auditor_bot_protection_resilience.md`](auditor_bot_protection_resilience.md)

---

## What shipped

Two independent fixes to the website auditor, both surfaced while diagnosing "works in dev,
fails in production":

1. **Frontend now has a first-class `blocked` scan state.** The backend has emitted
   `ScanQuality.BLOCKED` since the bot-protection work, but the dashboard's `ScanQuality` type
   never included `"blocked"`, and every "failed report" call site collapsed it via
   `q === "unreachable" ? "unreachable" : "render_failed"`. So a bot/firewall block rendered with
   the **render_failed** title *"Something went wrong on our end."* glued to the backend's body
   *"This site's bot/firewall protection blocked our scanner."* — a self-contradicting message that
   blamed Luran for the customer's WAF. `blocked` is now its own state (amber, "Scan blocked / This
   site blocked our scanner"), routed through one shared `scanStateQuality()` helper, with a
   "Blocked" chip in the audit-history row.
2. **Subresource SSRF validation no longer blocks the crawl event loop.** `_ssrf_route_handler`
   called `resolve_and_validate_host()` (a blocking `socket.getaddrinfo`) inline, once per allowed
   subresource, on the crawl's event loop. A JS-heavy page (Next.js pulling dozens of same-host
   `/_next` chunks) serialised every render behind synchronous DNS. Resolution now runs off the loop
   via `asyncio.to_thread`, and a **per-crawl-session host-validation cache** collapses the many
   same-host checks into one lookup.

## Why (root cause of the prod failure these came out of)

The reported production symptoms — audits either fail with "bot/firewall protection blocked our
scanner", or succeed but return only ~2 pages — were root-caused to **Vercel bot protection
challenging the production/staging VPS's datacenter IP with HTTP 403**. All three sites reported
(luranai.com, beyrictech.com, havenrespite.co.uk) are on **Vercel** (luranai's apex is
Cloudflare-in-front-of-Vercel) and all do **apex→www** redirects — i.e. the same hosting/bot stack,
which is why "all are same". Dev runs the crawl from a non-datacenter IP that isn't challenged; prod
does not. The challenge is probabilistic (~2/3), so the root either never clears (→ BLOCKED) or
clears while most subpages don't (→ only the ~1/3 that cleared come back, e.g. 2 pages). The prod
`luran_worker` sat at 398 MiB / 1.95 GiB during failures, ruling out OOM/resources.

These two fixes do **not** defeat the block (that needs an egress change — residential proxy or a
managed rendering API — tracked as a follow-up). They stop the auditor **lying about** the block and
remove a real per-page render tax. The FE fix also converts the (previously misleading) failure into
an actionable one: "ask them to allowlist Luran, or import your content manually."

## How it works

| Unit | Change |
|------|--------|
| FE type | `ScanQuality` union gains `"blocked"` (`app/types/auditor.ts`). |
| FE mapping | New `scanStateQuality(q)` in `scanQuality.ts` maps backend quality → `AuditScanState` prop: `unreachable`→unreachable, `blocked`→blocked, else `render_failed` (so an analysis-side failure that committed `ok`/`thin` but ended `failed` still reads "our end"). Replaces the inline ternary at all three call sites (public share, public flow, dashboard). `isUnreadable` now includes `blocked`. |
| FE copy | `SCAN_STATE_META.blocked` added (kind `error`, amber accent — not danger-red — since it is the site's protection, not our error). Backend `scan_quality_reason` still wins for the body. |
| FE history | `AuditHistoryRow` shows a "Blocked" chip beside the Failed pill (mirrors the "Partial" chip for `thin`). |
| BE DNS | `_validate_subresource_host(host, cache)` awaits `asyncio.to_thread(resolve_and_validate_host, host)`; `_make_ssrf_route_handler(cache)` factory builds the interceptor. Module-level `_ssrf_route_handler` kept (uncached) for standalone renders + existing tests. `_browser_render_session` creates one `dict[str,bool]` cache per crawl and passes the cached handler to every page via `_render_page_in_context(context, url, handler)`. Cache lifetime == the crawl, the same span the root host is already IP-pinned for, so it does not widen the DNS-rebinding window. |

## What's involved (files)

- **FE:** `luran-dashboard/app/types/auditor.ts`, `app/components/luran/auditor/scanQuality.ts`,
  `AuditScanState.tsx`, `AuditHistoryRow.tsx`, `app/auditor/report/[auditId]/page.tsx`,
  `app/auditor/page.tsx`, `app/(dashboard)/dashboard/grow/auditor/[auditId]/page.tsx`;
  test `tests/e2e/auditor.spec.ts` (new "blocked" case).
- **BE:** `luran/app/auditor/crawler.py`; tests `luran/tests/auditor/test_crawler.py`
  (two new cache tests).

## Verification

- **BE:** `poetry run pytest tests/auditor/test_crawler.py` → **47 passed** (incl. the 2 new
  per-session cache tests: same host resolved once; blocked host cached + still aborted). Full
  `tests/auditor/` → 268 passed; the 30 errors are pre-existing `engine.raw_connection()` failures
  (no local Postgres) in DB-backed endpoint/redirect tests untouched by this change.
- **FE:** `npx tsc --noEmit` → exit 0 (the `Record<…>` scan-state map and `resolveScanCopy` are now
  exhaustive over the widened union); `npx eslint <changed files>` → exit 0. The new Playwright
  case (`blocked` renders `data-scan-quality="blocked"`, shows the firewall body, and never shows
  "on our end") is written but not executed here — no local Next server; run with
  `npx playwright test tests/e2e/auditor.spec.ts -g "blocked state"` against a running dashboard.
- **Live evidence:** apex/www + CDN posture of all three sites verified by `curl -I` (307/308 →
  www; `server: Vercel`/Cloudflare). Confirm the IP block on the box:
  `docker exec luran_worker curl -s -o /dev/null -w "%{http_code}\n" -A "<AUDITOR_UA>" https://www.beyrictech.com/`
  run ~10× — a 200/403 mix confirms the datacenter-IP challenge.

## Operate / roll back

- No migration, no config change, no infra change. Pure code. Roll back = revert the two PRs.
- Concurrency note (not changed, verified safe): `AUDITOR_MAX_CONCURRENT_CRAWLS` is `3` in
  `.env.production` (against a ~1.95 GiB worker) and `1` in `.env.staging` — both override the
  `config.py` default of `2`, so no env is at the OOM boundary the default alone would imply.

## Follow-ups

- **Primary (open):** egress that clears Vercel/Cloudflare bot protection — residential/ISP proxy
  or a managed rendering API (ScrapingBee/Browserless/Zyte/Scrapfly). Decision pending; a managed
  API also removes Chromium from the worker (frees the 394 MiB/crawl) but changes the SSRF model
  (the provider makes the outbound request, so the guard moves to pre-call host validation).
- Optional stealth (`--disable-blink-features=AutomationControlled`, mask `navigator.webdriver`) —
  most effective paired with the egress change; held to avoid coupling it to this shipment.
- `auditor_bot_protection_resilience.md` follow-up "Frontend: dedicated BLOCKED treatment" is now
  **done** (this SOP).
