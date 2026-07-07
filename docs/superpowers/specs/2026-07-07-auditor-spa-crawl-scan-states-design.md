# Reliable AI-Readiness Auditor — SPA-aware Crawl + Honest Scan States

**Date:** 2026-07-07
**Status:** Approved (design)
**Repos:** `luran` (backend), `luran-dashboard` (frontend)
**PRD grounding:** Stage 4 — AI Website Auditor. Feature 4.1 (Website Crawler),
Feature 4.5 (Public Website Auditor), Feature 4.6 (Dashboard auditor). See
`docs/luran_prd.md:311–336`, `:447–489`.

---

## Problem

Two defects, discovered when scanning `https://weysure.luranai.com` (a
client-rendered Next.js site) returned a "0% ready / 0 pages scanned" report.

1. **Missing browser binary (already fixed).** The worker image had the
   `playwright` Python package but not the Chromium browser binary, so
   `crawler._playwright_render` threw `BrowserType.launch: Executable doesn't
   exist`. `render_page` caught it and returned an empty page; the pipeline
   "completed" over 0 rendered pages → 0% score. Fixed by adding
   `playwright install --with-deps chromium` to the Dockerfile (pinned to a
   shared `/opt/ms-playwright`). **Out of scope for this plan** beyond noting it
   as the precondition; it is committed separately.

2. **Discovery is blind to JavaScript-rendered navigation.** Two compounding
   causes:
   - `discovery.discover` finds internal links via a **plain HTTP fetch** and a
     regex over the *raw* server HTML. For a client-rendered SPA the raw HTML
     has no content links (they are injected by JS after hydration), so only the
     homepage is ever discovered.
   - Even the browser renderer used `wait_until="domcontentloaded"`, which
     snapshots the **empty shell before hydration**. So rendering alone did not
     help either.

   This violates PRD Feature 4.1 acceptance criterion (`docs/luran_prd.md:335`):
   "Handles JavaScript-rendered pages via headless browser (Playwright/Puppeteer)"
   — rendering handled JS for *content*, but link *discovery* never did, so
   internal links on JS sites were never followed to the required depth
   (`:316`, default 3).

3. **Dishonest failure surface.** A scan that could not reach or fully read a
   site is indistinguishable from a genuinely empty site: both render a
   confident "0% ready". Users cannot tell "your site scored low" from "we
   couldn't read your site".

### Validated evidence (live, in the running worker)

Same page (`weysure.luranai.com/`), two Playwright wait strategies:

| `wait_until`             | HTML size | total `href`s | same-site content links |
|--------------------------|-----------|---------------|--------------------------|
| `domcontentloaded` (old) | 49 KB     | 8             | **0**                    |
| `networkidle`            | 65 KB     | 33            | **14** (`/features`, `/faq`, `/help`, `/contact`, `/about`, `/how-it-works`, `/blog`, `/careers`, `/press`, `/privacy`, `/terms`, …) |

Waiting for hydration is the mechanism that makes SPA discovery work.

---

## Goals

- The auditor discovers and crawls JS-rendered sites to the configured depth,
  fulfilling PRD 4.1 (`:335`, `:316`).
- The report distinguishes **ok / thin / unreachable / render_failed** and the
  UI communicates each honestly, with a retry path.
- All existing safety rails are preserved verbatim: SSRF IP-pinning
  (`resolve_and_validate_host` + `--host-resolver-rules`), `robots.txt`,
  per-page timeout + size cap, 1 rps politeness, `max_pages` per plan.

## Non-goals

- No change to classification, gap analysis, scoring math, profile synthesis,
  claim/import flows, or notification wiring.
- No new backend endpoint for retry (the FE re-uses the existing start endpoint).
- No headless-browser tuning beyond the hydration wait (no proxying, no
  screenshotting, no per-domain concurrency changes).

## Global constraints

- Ground every requirement in `docs/luran_prd.md`; cite feature numbers.
- Commits in Luran repos MUST NOT include a Claude co-author trailer.
- Frontend follows Luran v2 visual constraints: Geist + Instrument Serif, ink
  `#0E0D2A` + warm-indigo scale, cream `#FAFAF7`, burnt-amber serif accents;
  gradients only as subtle radial halos (no gradient fills).
- Backend is **sync** SQLAlchemy; the auditor pipeline is async and runs in the
  ARQ worker. `crawl_site` and the renderer are async.
- `crawl_site` and every phase must never raise to the ARQ entrypoint; per-page
  failures are captured on `RawPage`, and `run_audit` already wraps everything
  in a FAILED-marking try/except.

---

## Architecture

The pipeline stays a linear multi-phase flow in `app/auditor/pipeline.py`. The
change replaces the "discover (HTTP) then crawl (render)" split with
"seed (cheap HTTP) then render-crawl (BFS, browser)", and adds an outcome
classification step before scoring.

```
run_audit:
  Phase 1b  seeds        = discovery.seed_urls(root, max_pages)   # root + sitemap.xml
  Phase 2   raw_pages    = crawler.crawl_site(seeds, max_pages,   # BFS render + link-expand
                              page_renderer, robots, rps)
            extracted    = extraction.extract_pages(raw_pages)    # unchanged
  Phase 2b  scan_quality = classify_scan_outcome(root_page, extracted)  # NEW
            (unreachable / render_failed  -> raise to FAILED path)
  Phase 3-8 profile / classify / gaps / scoring / persist        # unchanged
```

### Component changes

**`app/auditor/crawler.py`**

- `RawPage` gains `error_kind: RawPageError` (enum). `render_page` sets it:
  `OK`, `LAUNCH_FAILED` (browser executable / launch failure),
  `TIMEOUT`, `HTTP_ERROR` (status ≥ 400 or 0), `BLOCKED`
  (`UNSAFE_URL`/`UNSAFE_REDIRECT`), `SIZE_CAP`, `ROBOTS`.
  Existing string `error` field is retained for logging/back-compat.
- `_playwright_render`: `goto(url, wait_until="load", timeout=…)` then
  `try: await page.wait_for_load_state("networkidle", timeout=settings.AUDITOR_HYDRATION_SETTLE_MS)`
  `except PlaywrightTimeoutError: pass` — always call `page.content()` after.
  This lets SPAs hydrate while never dropping content for sites that never idle.
- New `crawl_site(seeds, max_pages, *, page_renderer=None, robots_disallowed=None,
  rps=None, extract_links=True) -> list[RawPage]`:
  - Frontier = deque of `(url, depth)` seeded from `seeds` at depth 0.
  - `seen: set[str]` to dedupe; a page is rendered at most once.
  - For each dequeued URL (while `len(rendered) < max_pages`): honor robots →
    `render_page` → append result. On a successful render, extract links from
    the **rendered** HTML via `discovery._extract_links`, then for each link:
    `normalize_url` → same-host check → `is_structurally_prunable` skip →
    `guard_url` SSRF skip → if unseen and `depth+1 <= _MAX_DEPTH`, enqueue.
  - Sleep `1/rps` between renders (reuse existing politeness).
  - Returns rendered `RawPage`s in visitation order (the root first).

**`app/auditor/discovery.py`**

- `discover(...)` is replaced by `seed_urls(root_url, max_pages, fetch_text=None)`
  returning root + `sitemap.xml` `<loc>` URLs only (normalized, pruned,
  SSRF-guarded, same-host, capped at `max_pages`). The HTTP-BFS link-following
  loop is removed (superseded by `crawl_site`). `normalize_url`,
  `is_structurally_prunable`, `guard_url`, `_extract_links`, `_parse_sitemap`
  are kept (now shared with `crawl_site`).

**`app/auditor/scan_quality.py`** (new, small)

- `RawPageError` enum (or colocate in crawler; single source of truth).
- `classify_scan_outcome(root_page: RawPage | None, extracted: list, *,
  thin_min_content_chars: int) -> tuple[ScanQuality, str]` returning
  `(quality, reason)`:
  - root missing or `error_kind == LAUNCH_FAILED` → `RENDER_FAILED`.
  - root `error_kind in {TIMEOUT, HTTP_ERROR, BLOCKED}` → `UNREACHABLE`.
  - root rendered but `sum(len(p.text) for p in extracted) < thin_min_content_chars`
    → `THIN`.
  - else → `OK`.
- `ScanQuality` enum: `ok / thin / unreachable / render_failed`.

**`app/db/models/audit.py`**

- `AuditReport` gains `scan_quality: Mapped[ScanQuality | None]`
  (`_audit_enum(ScanQuality, "audit_scan_quality")`, nullable, default `None`)
  and `scan_quality_reason: Mapped[str | None]` (`String(255)`, nullable).

**`app/auditor/pipeline.py`**

- Phase 1b: `discovery.seed_urls(...)`; Phase 2: `crawler.crawl_site(...)`.
- After extraction: `quality, reason = classify_scan_outcome(...)`; set
  `report.scan_quality`/`report.scan_quality_reason`. For `UNREACHABLE` /
  `RENDER_FAILED`, raise a typed `ScanUnreadable` exception so the existing
  FAILED path marks the report + job FAILED — but first persist
  `scan_quality`/`reason` so the FE can explain *why*. (`THIN` and `OK`
  continue through scoring normally.)
- `pages_discovered` = count of unique URLs the crawl reached; `pages_scanned` =
  extracted count (existing semantics preserved incl. resume `existing_entries`).

**`app/core/config.py`**

- `AUDITOR_HYDRATION_SETTLE_MS: int = 3000`
- `AUDITOR_THIN_MIN_CONTENT_CHARS: int = 800`

**`app/schemas/auditor.py` + endpoints**

- `PublicReportResponse` and the dashboard report response gain
  `scan_quality: str | None` and `scan_quality_reason: str | None`.
- `public_auditor.get_public_report` and the dashboard report endpoint include
  the two fields from the model.

### Frontend (`luran-dashboard`)

- `app/auditor/page.tsx`, `app/auditor/report/[auditId]/page.tsx`,
  `app/components/luran/auditor/AuditReportView.tsx`, and the dashboard
  `app/(dashboard)/dashboard/grow/auditor/[auditId]/page.tsx` branch on
  `scan_quality`:
  - `unreachable` → "We couldn't reach **{domain}**" panel + **Retry scan**.
  - `render_failed` → "Something went wrong on our end while reading your site"
    + **Retry scan** (copy blames Luran infra, not the site).
  - `thin` → normal report **plus** a banner: "We could only partially read your
    site — you may get a fuller picture by retrying or importing manually."
  - `ok` → unchanged report.
- **Retry scan** re-POSTs the same URL to the existing public/dashboard start
  endpoint and routes to the new report id. No new backend endpoint.
- Visuals follow the Luran v2 constraints above; reuse existing auditor
  primitives (`AuditUrlBox`, score components) for consistency.

---

## Data flow (happy path, SPA site)

1. `seed_urls("https://site.com")` → `["https://site.com", …sitemap locs…]`.
2. `crawl_site` renders root with hydration wait → 14 nav links found →
   enqueue `/features`, `/faq`, `/help`, … (depth 1) → render each (depth ≤ 3),
   respecting robots/rps/budget.
3. `extract_pages` → per-page title/text/hash (unchanged).
4. `classify_scan_outcome` → `OK` (content ≥ threshold).
5. Profile → classify → gaps → score → `COMPLETED` with a real, non-zero score.

## Error handling

- Renderer never idles (persistent connections) → `wait_for_load_state`
  times out, is swallowed, `content()` still captured. No content lost.
- Browser launch fails → `error_kind=LAUNCH_FAILED` → root triggers
  `RENDER_FAILED` → report FAILED with an honest reason, not a fake 0%.
- Root unreachable/blocked → `UNREACHABLE` → FAILED with reason.
- Non-root page fails mid-crawl → captured `RawPage`, crawl continues; it simply
  isn't extracted.
- SSRF: every enqueued link passes `guard_url`; the renderer keeps its
  pre-launch `resolve_and_validate_host`, `--host-resolver-rules` pinning, and
  subresource route interception unchanged.

## Testing strategy

**Backend unit** (injected fake renderer — no real browser):
- `crawl_site`: SPA fixture whose fake renderer returns HTML with JS-style
  links → frontier expands; assert depth-3 cap, `max_pages` budget, same-host
  filter, robots disallow, asset/auth pruning, SSRF skip, single-render dedupe.
- `_playwright_render` settle path: fake `wait_for_load_state` raising timeout →
  content still returned (test via a thin wrapper/monkeypatch).
- `error_kind` mapping for each render outcome.
- `classify_scan_outcome` truth table: ok / thin / unreachable / render_failed.
- `seed_urls`: sitemap present → locs seeded; absent → root only; SSRF/prune
  applied; `max_pages` cap.

**Backend integration**:
- `run_audit` with a fake multi-page SPA renderer → `pages_scanned > 1`,
  `scan_quality == ok`, non-zero score, entries persisted.
- Thin fixture (root renders, near-empty text) → `scan_quality == thin`,
  `status == completed`.
- Unreachable fixture (root `error_kind=TIMEOUT`) → `status == failed`,
  `scan_quality == unreachable`, reason persisted.
- render_failed fixture (root `error_kind=LAUNCH_FAILED`) → `status == failed`,
  `scan_quality == render_failed`.
- Resume path unchanged: `already_have` URLs skipped, new pages appended.

**Frontend (Playwright)**: one spec per state (mock the report API) asserting
the correct panel/banner + a working **Retry scan** affordance; plus the
existing report spec stays green for `ok`.

**Live**: re-scan `weysure.luranai.com` end-to-end → ≥ ~11 content pages,
`scan_quality == ok`, non-zero overall score.

## Migration

- Alembic migration adding `scan_quality` (enum `audit_scan_quality`) and
  `scan_quality_reason` (`varchar(255)`), both nullable. Existing rows stay
  `NULL` (rendered as `ok`/legacy by the FE). Dev DB picks the columns up via
  `create_all`; prod requires the migration (dev DB is not under alembic — known
  follow-up).

## Sequencing

One spec, two implementation plans:
1. **Backend plan** (Parts A–C + migration + tests) on a branch off `develop`.
2. **Frontend plan** (Part D) on a branch off `staging`, once the API contract
   (`scan_quality`, `scan_quality_reason`) is merged/frozen.
