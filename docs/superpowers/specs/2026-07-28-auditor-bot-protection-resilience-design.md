# Auditor bot-protection resilience + split-host canonicalization — Design

**Date:** 2026-07-28
**Area:** `app/auditor/` (discovery, crawler, scan_quality, pipeline) + one enum migration
**Status:** Design approved — proceeding to implementation plan
**Grounding:** Stage 4 Website Auditor (`docs/luran_prd.md` §4). This is a robustness/correctness
fix to the existing auditor — it makes the crawl survive bot-protected and split-host sites and
report honestly when it cannot — not a new PRD feature.

---

## Problem

Investigation on 2026-07-28 (auditing `luranai.com`) surfaced two independent auditor faults
(a third, an un-migrated local dev DB, was separate and already fixed):

1. **Bot-firewall blocking (Fault 1).** `www.luranai.com` moved behind Vercel bot mitigation
   (Attack-Challenge-Mode / BotID). It returns **HTTP 403 + a challenge page to ~2 of every 3
   requests** from the audit worker's datacenter IP, **regardless of user-agent** (confirmed by
   worker-side repro: `default 403,200,403` / `realUA 403,403,200`). A 403 root yields no content,
   so `classify_scan_outcome` returns `UNREACHABLE` and the report FAILS with the misleading
   message *"We couldn't reach your website. Check the URL and try again."* — but the site is
   perfectly reachable; it is **refusing our bot**.

2. **Split-host sitemap (Fault 2).** The site is split-hosted (apex `luranai.com` → Cloudflare,
   307→www; `www` → Vercel), and its sitemap lists all 19 URLs on the **apex**. `seed_urls._accept`
   (`discovery.py`) and the `crawl_site` link filter (`crawler.py`) reject any URL whose
   `netloc != root_host` (exact string match), so **every apex `<loc>` is dropped** when auditing
   www. Confirmed by repro: `seed_urls(www)` returns only the homepage. luranai.com survives
   because its nav links are relative, but a site with absolute cross-host links + no relative nav
   would collapse to one page.

## Global Constraints

- Commits in Luran repos MUST NOT include a Claude co-author trailer.
- API stays additive on `/api/v1` (no breaking changes; 3 clients on v1).
- SSRF guard (`resolve_and_validate_host`, per-hop, IP-pinned, fail-closed) is preserved on every
  new/changed fetch path — no URL is rendered or fetched without passing the existing guard.
- The crawl fetch stays `follow_redirects=False`; `resolve_redirect_chain` remains the only
  redirect follower.

## Non-goals (documented follow-ups, NOT in this spec)

- Active challenge-solving (detect challenge page → wait-for-JS + reload to earn the clearance
  cookie).
- Residential / rotating proxy egress for datacenter-IP blocks.
- Frontend "import content manually" CTA for the new `BLOCKED` state (separate repo; the honest
  `scan_quality_reason` string already renders, so users see the truth immediately).
- Relaxing our own site's Vercel firewall — handled out-of-band by allowlisting the worker's
  egress IP (owner action), not by code.

---

## Design

Four small, independently-testable units, all in `app/auditor/`.

### Unit 1 — Split-host URL canonicalization (Fault 2)

**New helper** in `discovery.py`:

```python
def canonicalize_to_root(url: str, root_host: str) -> str:
    """If url is the same registrable host as root_host (differs only by a leading
    'www.', case-insensitive) but its netloc is not identical, rewrite url's host
    to root_host. Otherwise return url unchanged.

    Genuinely different hosts (different subdomain/domain) are returned unchanged
    so the caller's same-host filter still drops them. Never changes scheme/path/query.
    """
```

- Reuses the existing `_same_registrable_host(a, b)` (`a.removeprefix("www.") == b.removeprefix("www.")`)
  to decide eligibility.
- Applied in `seed_urls`: after `normalize_url(loc)`, rewrite via `canonicalize_to_root(n, root_host)`
  **before** `_accept`. The rewritten URL then matches `root_host` and passes the existing filter.
- Applied in `crawl_site`'s link-discovery loop: rewrite each extracted link via
  `canonicalize_to_root(link, root_host)` before the `netloc != root_host` check.
- **Order preserved:** rewrite → then the unchanged structural-prune + SSRF `guard_url`. A rewritten
  URL that fails SSRF is still dropped.
- **Why safe on the IP pin:** the root is already the canonical host (`start_audit` sets
  `report.url = final_url` after resolving the apex→www redirect). Rewritten URLs therefore share
  `root_host` and use the crawl's existing single-host IP pin — no cross-host fetch is introduced.

### Unit 2 — Retry-with-backoff on soft-block (Fault 1a)

- **New `RawPageError.SOFT_BLOCKED`** (`crawler.py`) for HTTP **403 / 429** — split out from the
  current `HTTP_ERROR` (which today swallows any status ≥ 400). Other ≥400 statuses stay
  `HTTP_ERROR`.
- In `render_page`, when a render resolves to status **403 or 429**, retry up to
  `settings.AUDITOR_SOFT_BLOCK_RETRIES` (default **2**) times, sleeping
  `settings.AUDITOR_SOFT_BLOCK_BACKOFF_S[attempt]` (default **[1, 3]** seconds) between attempts,
  reusing the **same injected renderer / browser session** so any challenge-clearance cookie set on
  a prior request carries into the retry and into later pages.
- If all attempts still return 403/429, the returned `RawPage` carries
  `status=<last>`, `error_kind=SOFT_BLOCKED`.
- Backoff list shorter than retry count → clamp to the last value; retries=0 → no retry (single
  attempt). Retry only on 403/429 — never on 2xx, timeouts, or other errors.

### Unit 3 — Honest `BLOCKED` scan-state (Fault 1b)

- **New `ScanQuality.BLOCKED = "blocked"`** (`app/db/models/audit.py`) — additive Postgres enum
  value via one Alembic migration (`ALTER TYPE ... ADD VALUE 'blocked'`).
- New reason constant in `scan_quality.py`:
  `_BLOCKED_REASON = "This site's bot/firewall protection blocked our scanner. Ask them to allowlist Luran, or import your content manually."`
- `classify_scan_outcome`: add `SOFT_BLOCKED` handling. When there is **no usable content** and the
  root page's `error_kind == RawPageError.SOFT_BLOCKED`, return `(ScanQuality.BLOCKED, _BLOCKED_REASON)`.
  Checked **before** the existing `_UNREACHABLE_KINDS` branch. `SOFT_BLOCKED` is NOT added to
  `_UNREACHABLE_KINDS`.
- `pipeline.run_audit`: treat `BLOCKED` like `UNREACHABLE`/`RENDER_FAILED` for control flow — persist
  `scan_quality` + `scan_quality_reason`, then raise `ScanUnreadable(quality, reason)` so the report
  is marked FAILED (carrying the honest reason). Extend the
  `if quality in (UNREACHABLE, RENDER_FAILED)` guard to include `BLOCKED`.

### Unit 4 — Realistic user-agent

- **New `settings.AUDITOR_USER_AGENT`** defaulting to a current desktop Chrome UA string (no
  `HeadlessChrome` token).
- `_browser_render_session` / `_render_page_in_browser`: create the page from a browser **context**
  that sets `user_agent=settings.AUDITOR_USER_AGENT` (today `browser.new_page()` is called directly,
  inheriting the default HeadlessChrome UA). Route guard and DNS pin unchanged.

### New settings (`app/core/config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `AUDITOR_SOFT_BLOCK_RETRIES` | `2` | Retries on a 403/429 render |
| `AUDITOR_SOFT_BLOCK_BACKOFF_S` | `[1, 3]` | Backoff seconds per retry attempt |
| `AUDITOR_USER_AGENT` | current Chrome UA | Browser context UA |

---

## Data flow (unchanged except where noted)

`start_audit` (canonicalizes root apex→www) → `seed_urls` (**Unit 1** rewrites apex `<loc>`s to
root host) → `crawl_site` (**Unit 1** rewrites links; **Unit 4** UA context) → `render_page`
(**Unit 2** retries 403/429) → `classify_scan_outcome` (**Unit 3** → `BLOCKED` on soft-blocked
root) → `run_audit` (**Unit 3** FAILS with honest reason).

## Error handling

- A soft-blocked page that never recovers → `SOFT_BLOCKED` RawPage; if it's the root and no content
  → `BLOCKED` scan-state (honest). If other pages recovered and there IS content → `OK`/`THIN` as
  today (a partial crawl is still a real result).
- Canonicalization never raises; a rewritten URL still faces SSRF/structural filters.
- All existing states (OK/THIN/UNREACHABLE/RENDER_FAILED) keep their current triggers.

## Testing

- `canonicalize_to_root`: apex→www rewrite; www→apex rewrite; identical host unchanged; different
  subdomain/domain unchanged; scheme/path/query preserved.
- `seed_urls`: www root + sitemap listing apex `<loc>`s → seeds are rewritten www URLs (not dropped);
  a genuinely off-host `<loc>` is still dropped; SSRF-failing rewrite still dropped.
- `render_page` retry: injected renderer returning `403,403,200` with `RETRIES=2` → one `RawPage`,
  `status=200`, `error_kind=OK`; `403,403,403` → `SOFT_BLOCKED`; `429` treated same as `403`; a
  `200` never retries.
- `classify_scan_outcome`: no content + root `SOFT_BLOCKED` → `BLOCKED` + reason; no content + root
  `TIMEOUT`/`HTTP_ERROR` → still `UNREACHABLE`; content present + root `SOFT_BLOCKED` → `OK`/`THIN`.
- Migration: enum value `blocked` present after upgrade; downgrade is a no-op/documented (Postgres
  enum value removal is non-trivial — downgrade leaves the value, which is safe/additive).

## Operate / roll back

- The enum migration must reach an environment **before** code writes `BLOCKED`. Confirmed 2026-07-28
  that CD runs `alembic upgrade` (prod was already at head); the local 500 that started this
  investigation was an un-deployed local dev DB, since fixed.
- Additive migration, additive settings, no API breakage → roll back = revert the branch (the extra
  enum value is inert if unused).

## Follow-ups

- FE: dedicated `BLOCKED` treatment + "import content manually" CTA (luran-dashboard).
- Active challenge-solving and/or residential proxy egress if data shows retries are insufficient
  for a meaningful share of customer sites.
- Own-site: allowlist the worker egress IP in the luranai.com Vercel firewall (owner action).
