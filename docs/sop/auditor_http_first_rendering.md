# SOP — Auditor HTTP-first rendering (default render backend)

**Status:** Implemented on branch `feat/auditor-http-first-rendering` (off `develop`) · 2026-08-13
**Area:** `app/auditor/crawler.py`, `app/core/config.py`

---

## What shipped

The website auditor's crawl now fetches pages over plain HTTP (via `curl_cffi`, impersonating
Chrome's TLS/JA3 fingerprint) by default, instead of launching a headless Chromium browser for
every page. A page only pays for a browser launch when the HTTP fetch comes back thin (an
SPA/JS-rendered shell) or non-2xx/3xx. This is a pure backend-selection change — same `RawPage`
contract, same SSRF guards, same crawl/BFS logic — so nothing downstream of `crawl_site`/
`render_page` needed to change.

Commits (branch, chronological):
- `a40aeba` — `curl_cffi` dependency + `AUDITOR_RENDER_BACKEND`/`AUDITOR_HTTP_*` config
- `107603d` — SSRF-pinned `_curl_cffi_fetch` with per-hop redirect re-guard
- `cf4c78b` — fix: pin DNS via `AsyncSession(curl_options=...)`, not a per-request `resolve=` kwarg
- `5c72c55` — `_http_first_render`: HTTP-first with per-page headless (`_playwright_render`) fallback
- `887914a` — **this task**: `_select_backend()`/`_default_renderer()` wired into `render_page` and
  `crawl_site`, making `http` the default path end-to-end

## Why

Headless Chromium is the auditor's biggest cost and reliability liability: a browser launch per
page is slow, memory-hungry (see `auditor_worker_memory_and_reaper.md`), and — per
`auditor_bot_protection_resilience.md` — some bot firewalls (Vercel BotID) challenge headless
requests from the worker's datacenter IP at a much higher rate than plain HTTP requests. Most
audited sites are server-rendered (SSR/SSG marketing sites), so a full browser is unnecessary for
the common case. HTTP-first fetches the raw HTML directly and only escalates to a browser for
genuinely client-rendered pages, cutting both cost and bot-block exposure for the majority case.

## How it works

| Unit | Behavior |
|------|----------|
| `_select_backend()` | Returns `"local"` only when `settings.AUDITOR_RENDER_BACKEND == "local"`; `"http"` otherwise (including any unexpected/unset value) — `"http"` is the safe default. |
| `_default_renderer()` | Returns `_http_first_render` (http backend) or `_playwright_render` (local backend) — the per-URL callable used when no `page_renderer` is injected. |
| `render_page` | `renderer = page_renderer or _default_renderer()` — was previously hardcoded to `_playwright_render`. An injected `page_renderer` (tests, or explicit caller override) always wins. |
| `crawl_site` tail | Three-way branch, in order: (1) injected `page_renderer` or an empty frontier → run with whatever was injected (`None` is fine — `render_page` resolves it per-page); (2) `_select_backend() == "http"` → run every page through `_http_first_render` directly, **no shared browser session** launched; (3) local backend → launch one shared `_browser_render_session` for the whole same-host crawl (unchanged legacy path) and reuse it across pages. |
| `_http_first_render` (already shipped, unchanged by this task) | Fetches via `_curl_cffi_fetch`; if the response is 2xx/3xx and `extract_main` finds ≥ `AUDITOR_HTTP_MIN_CONTENT_CHARS` (500) of text, returns it as-is. Otherwise falls back to `_playwright_render` for that one URL — so even under the http backend, thin/SPA pages still get a real browser render, just per-page instead of a whole-crawl browser. |

Net effect: under the default `http` backend, `crawl_site` never opens Chromium unless at least
one page needs the per-page fallback — SSR sites complete an entire crawl with zero browser
launches.

## What's involved (files)

- `app/auditor/crawler.py` — `_select_backend`, `_default_renderer` (new, ~line 405); `render_page`
  line ~525 (`renderer = page_renderer or _default_renderer()`); `crawl_site` tail ~line 699-711.
- `app/core/config.py:255` — `AUDITOR_RENDER_BACKEND: Literal["http", "local"] = "http"` (shipped in
  an earlier task on this branch, consumed here).
- `tests/auditor/test_crawler.py` — 3 new tests appended at end of file:
  `test_select_backend_defaults_to_http_local_when_overridden`,
  `test_crawl_site_http_backend_uses_http_renderer_no_shared_browser`,
  `test_crawl_site_injected_renderer_still_overrides_backend`.

No other files were touched — `pipeline.py`, `service.py`, and `config.py` were explicitly
out of scope for this task (config already carried the settings from an earlier task).

## Verification

- TDD: wrote the 3 new tests first, confirmed 2 of 3 failed RED (`_select_backend`/
  `_default_renderer` undefined; `crawl_site` still launched the shared browser) — the third
  ("injected renderer overrides backend") already passed because that precedence pre-existed.
  Implemented the brief's exact diff, reran → 3/3 GREEN.
- Full `tests/auditor/` suite: **279 passed** (including all 58 in `test_crawler.py`). All
  pre-existing `render_page`/`crawl_site` call sites in `test_crawler.py` already inject an
  explicit `page_renderer`, so flipping the default to `http` caused **zero pre-existing test
  fallout** — no tests needed adjusting.
- 30 unrelated errors in `test_dashboard_endpoints.py` / `test_start_audit_redirect.py` are a
  local-environment Postgres connection failure (`localhost:5432` not running), confirmed via
  `pg_isready` — unrelated to this change and untouched by it.
- `ruff check` clean on the lines this task added (crawler.py additions and the 3 new test
  functions); a handful of pre-existing lint findings elsewhere in `test_crawler.py` (unused
  `pages` vars, one `E702`) predate this task and were left as-is per scope.

## Operate / roll back

- Roll back to headless-only globally: set `AUDITOR_RENDER_BACKEND=local` in env — no code change,
  no migration. Per-page fallback inside `_http_first_render` already guarantees correctness for
  SPA/JS-heavy sites even with the default `http` backend, so the override is an ops escape hatch
  (debugging a specific site, or a temporary revert) rather than something expected in normal
  operation.
- No DB migration, no API contract change — safe to deploy without coordinating other services.

## Follow-ups

- Monitor bot-block rate on `http` vs the old all-headless baseline in production (ties into the
  open egress-hardening item in `auditor_bot_protection_resilience.md`) — HTTP-first should reduce
  Vercel BotID challenges for SSR sites, but per-page Playwright fallback on thin pages is still
  exposed to the same datacenter-IP challenge rate as before.
- No SOP update needed for `auditor_bot_protection_resilience.md` or
  `auditor_worker_memory_and_reaper.md` — this change doesn't alter their documented behavior, it
  reduces how often the browser path they describe is invoked.
