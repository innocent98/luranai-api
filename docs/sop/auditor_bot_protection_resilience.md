# SOP — Auditor bot-protection resilience + split-host canonicalization

**Status:** Implemented on branch `fix/auditor-bot-protection-resilience` (off `develop`) · 2026-07-28
**Area:** `app/auditor/` (discovery, crawler, scan_quality, pipeline) + `app/core/config.py`, `app/db/models/audit.py`, one enum migration

---

## What shipped

The website auditor now survives two failure modes that made it report a healthy site as
unreachable:

1. **Split-host sites** — sitemap/link URLs that differ from the crawl root only by a leading
   `www.` (apex↔www on split-hosted sites) are rewritten to the root host and crawled, instead of
   being dropped by the exact-`netloc` same-host filter.
2. **Bot-protected sites** — an HTTP 403/429 challenge on a page is retried with backoff on the same
   browser session, the crawl uses a realistic user-agent on a shared context (so a challenge
   clearance cookie carries across pages), and if the site still refuses us the report shows an
   honest `BLOCKED` state ("this site's bot/firewall protection blocked our scanner") instead of the
   false "we couldn't reach your website — check the URL."

Commits (branch): `cf668b6` (canonicalize) · `725ca32` (soft-block retry) · `76da9fe` (BLOCKED
state + migration) · `b1db991` (UA context) · `f036a47` (concurrency test mock fix).

## Why (root cause)

Auditing `luranai.com` on 2026-07-28 returned failed/unreachable. A worker-side reproduction found
the site had moved to split hosting (**apex `luranai.com` → Cloudflare 307→www; `www` → Vercel**)
with **Vercel bot mitigation** that returns a **403 challenge to ~2 of every 3 requests** from the
audit worker's datacenter IP — *regardless of user-agent* (`default 403,200,403` / `realUA
403,403,200`). A 403 root yielded no content → `UNREACHABLE` → FAILED with a misleading message.
Separately, the www sitemap listed all 19 URLs on the **apex**, which the exact-`netloc` filter
dropped (`seed_urls(www)` returned only the homepage). Neither was an auditor regression — the site
infra changed — but the auditor handled both dishonestly and fragilely. (A third symptom, a local
dev-DB `500` on `GET /api/v1/audit`, was an un-applied `entered_url` migration on the *local* DB
only; production was already migrated. Fixed separately.)

## How it works

| Unit | Change |
|------|--------|
| Canonicalization | `discovery.canonicalize_to_root(url, root_host)` rewrites a URL's host to `root_host` when they are the same registrable host (differ only by leading `www.`) but the netloc isn't already `root_host`; genuinely different hosts are returned unchanged. Applied in `seed_urls` (sitemap loop) and `crawl_site` (link loop), **before** the same-host/structural-prune/SSRF filters. |
| Soft-block retry | `RawPageError.SOFT_BLOCKED` for HTTP 403/429. `render_page` was split into `_render_once` (all original branches preserved) + a retry wrapper that retries **only** 403/429 up to `AUDITOR_SOFT_BLOCK_RETRIES` (2) with `AUDITOR_SOFT_BLOCK_BACKOFF_S` (`[1,3]`s) backoff, reusing the same renderer. The pre-render SSRF `guard_url` runs once, outside the loop. |
| Honest BLOCKED | `ScanQuality.BLOCKED` (additive enum + migration `ab20blockedquality`). `classify_scan_outcome` returns `BLOCKED` + an actionable reason when there is no content **and** the root page is `SOFT_BLOCKED` (not added to `_UNREACHABLE_KINDS`). `pipeline.run_audit` persists the state+reason then raises `ScanUnreadable` (report FAILED, honest reason). |
| Realistic UA + shared context | `AUDITOR_USER_AGENT` (desktop Chrome 126, no `HeadlessChrome` token). `_browser_render_session` opens **one** `browser.new_context(user_agent=...)` per crawl and every page comes from it (was `browser.new_page()`, an isolated context per page) — so a challenge-clearance cookie carries across pages, making the retry effective. |

## What's involved (files)

`app/auditor/discovery.py`, `app/auditor/crawler.py`, `app/auditor/scan_quality.py`,
`app/auditor/pipeline.py`, `app/core/config.py`, `app/db/models/audit.py`,
`alembic/versions/ab20_add_blocked_scan_quality.py`; tests in `tests/auditor/`
(`test_canonicalize.py` new; `test_crawler.py`, `test_scan_quality.py`, `test_crawl_concurrency.py`
extended).

## Verification

- Built subagent-driven (fresh implementer + spec/quality reviewer per task); all four
  implementation tasks Approved.
- **136 pure auditor unit tests** pass (canonicalize, crawler incl. retry + UA context, scan_quality
  incl. BLOCKED, discovery, seed priority/depth, link priority, concurrency, classify, extraction).
- **20 DB-dependent tests** (`test_pipeline.py`, `test_audit_models.py`) pass against the dev
  Postgres — the BLOCKED pipeline guard and the enum round-trip end-to-end.
- `ruff check app/` clean (the CI gate; `tests/` and `alembic` are out of ruff scope).
- Migration proven on **real Postgres 15**: applied to the local dev DB, `enum_range` =
  `[ok, thin, unreachable, render_failed, blocked]`, `alembic current = ab20blockedquality`.
- The bug itself was found and characterized by a worker-side Playwright reproduction (curl 200 but
  render 403 on the 2nd request; identical crawl succeeds from a residential IP).

## Operate / roll back

- The enum migration must reach an environment **before** code writes `BLOCKED`. Confirmed CD runs
  `alembic upgrade` (prod was already at head during the investigation). Additive + reversible-as-
  no-op (Postgres can't drop an enum value; the extra label is inert when unused).
- Roll back = revert the branch; the enum label is harmless if left behind.

## Follow-ups

- **Own site:** allowlist the audit worker's egress IP in the `luranai.com` Vercel firewall (owner
  action) — the code retry is best-effort against a ~2/3 challenge rate, not a guaranteed unblock.
- **Frontend:** dedicated `BLOCKED` treatment + an "import content manually" CTA (luran-dashboard).
  ✅ **Done 2026-08-11** — see [`auditor_blocked_state_and_dns_perf.md`](auditor_blocked_state_and_dns_perf.md).
  (The FE had been *mislabeling* `blocked` as `render_failed`/"on our end", not just rendering the
  reason plainly — that is now fixed with a first-class amber `blocked` state.)
- **Deferred v1 non-goals:** active challenge-solving (wait-for-JS + reload for the clearance
  cookie) and residential/rotating proxy egress for strict datacenter-IP blocks — revisit if data
  shows retries are insufficient for a meaningful share of customer sites.

Spec: `docs/superpowers/specs/2026-07-28-auditor-bot-protection-resilience-design.md`
Plan: `docs/superpowers/plans/2026-07-28-auditor-bot-protection-resilience.md`
