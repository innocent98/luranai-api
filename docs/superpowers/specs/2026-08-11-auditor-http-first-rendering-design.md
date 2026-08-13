# Auditor HTTP-first rendering (self-hosted bot-block workaround) — Design

**Status:** Approved (brainstorm) — pending spec review
**Date:** 2026-08-11
**Scope:** Backend (`luran`) only. FE `blocked`-state and async-DNS perf already
shipped (2026-08-11); out of scope here.

> **Approach history:** managed unblockers (Bright Data, then ZenRows) were
> spec'd first. Bright Data was KYC-rejected; before adopting a third party we
> re-checked our own evidence and confirmed a **self-hosted** fix works. Managed
> rendering is retained only as a *documented, un-implemented* future backend
> (see Follow-ups) for the residual case self-hosting cannot cover.

## Problem

The auditor works in dev but fails in staging/production with `BLOCKED`
("bot/firewall blocked") or returns only ~2 pages. Root cause: the failing
targets (luranai.com, beyrictech.com, havenrespite.co.uk) are Vercel-hosted; Vercel
bot protection 403-challenges the auditor's **headless-Chromium render from the
VPS datacenter IP** (~2/3 of requests). Dev egresses from a non-datacenter IP and
is not challenged — the classic dev/prod divergence.

## The linchpin (measured, 2026-08-12)

Plain HTTP from the **production worker** is NOT blocked — only the headless render
is. Ten consecutive `httpx` GETs from inside `luran_worker`:

| Target | Result |
| --- | --- |
| `https://www.beyrictech.com/` | 10/10 × `200`, 89,126-char body |
| `https://www.luranai.com/` | 10/10 × `200`, 179,271-char body |

Those are full server-rendered pages (SSR/SSG Next.js), not shells — the content
and links the auditor needs are already in the HTTP response. So for these sites a
headless browser is both unnecessary AND the sole trigger of the block.

## Goal

Render auditor pages via a **self-hosted HTTP-first path**: fetch raw HTML over a
browser-fingerprinted HTTP client (`curl_cffi`), and fall back to the existing
local headless render only when the HTTP HTML is too thin (a genuine client-side
SPA). No third-party dependency. Removing routine Chromium use also lifts the
auditor's long-standing OOM/memory pressure.

## What this does and does not solve (honest boundary)

- **Solves:** SSR/SSG sites behind IP/fingerprint bot protection (the reported
  failures, and the large majority of real audit traffic).
- **Does NOT solve:** a site that is BOTH client-rendered-only AND behind
  aggressive protection (Turnstile/DataDome/BotID) from a datacenter IP — plain
  HTTP yields a shell and headless still 403s. That residual is out of reach for
  any purely self-hosted solution from a datacenter IP; it is the (documented)
  reason the managed backend seam is kept.

## Global constraints

- Change lives in `crawler.py` + `config.py`, behind the existing `page_renderer`
  seam. **`pipeline.py` / `service.py` untouched.**
- **SSRF parity is mandatory on the HTTP path.** Unlike a managed API, this
  egresses from OUR network, so it MUST reuse the pin used by
  `_default_fetch_text` / `_guarded_get`: `resolve_and_validate_host(host)` →
  connect pinned to that IP, original hostname in the `Host` header + SNI,
  redirects re-guarded, fail-closed on `ValueError`.
- The `local` headless backend is **kept** (dev + fallback), never deleted.

## Design

### Unit 1 — Render-backend config (`config.py`)

```python
# "http"  = HTTP-first fetch (curl_cffi) with per-page headless fallback (default).
# "local" = headless-only (debugging / explicit override).
AUDITOR_RENDER_BACKEND: Literal["http", "local"] = "http"
# Below this many extracted chars, the HTTP fetch is deemed a thin/SPA shell and
# the page falls back to a headless render.
AUDITOR_HTTP_MIN_CONTENT_CHARS: int = 500
# curl_cffi browser-impersonation target (TLS/JA3 + default headers).
AUDITOR_HTTP_IMPERSONATE: str = "chrome"
AUDITOR_HTTP_TIMEOUT_S: int = 15
```

Making `http` the default in **both** dev and prod is deliberate: identical render
path everywhere ends the dev/prod divergence that caused the original failure.

### Unit 2 — SSRF-guarded HTTP fetch (`crawler.py`)

`_curl_cffi_fetch(url) -> tuple[int, str, str]` — `(status, html, final_url)`,
same contract as the other renderers:

- Resolve + validate host (`resolve_and_validate_host`); on `ValueError` return
  `(0, "", url)` → treated as blocked (parity with `_playwright_render`).
- `curl_cffi.requests.AsyncSession(impersonate=AUDITOR_HTTP_IMPERSONATE)`, pin the
  connection to the validated IP via curl's `resolve` (host:port:ip), send the
  real hostname as `Host`, realistic `Accept` / `Accept-Language`.
- **Redirects:** do not blindly auto-follow to unvalidated hosts. Follow at most a
  few hops, re-running `resolve_and_validate_host` on each hop's host (mirrors
  `resolve_redirect_chain`); `final_url` = the last validated URL, fed to
  `render_page`'s existing post-redirect guard.
- Return `(status_code, response_text, final_url)`.

### Unit 3 — HTTP-first renderer with headless fallback (`crawler.py`)

`_http_first_render(url)`:

1. `status, html, final_url = await _curl_cffi_fetch(url)`.
2. If `status` is 2xx/3xx AND `html` extracts ≥ `AUDITOR_HTTP_MIN_CONTENT_CHARS`
   of text → return it. (No browser launched — the common SSR path.)
3. Else (thin/shell, or an HTTP error) → fall back to the existing local headless
   render for this one URL and return its result.

The thinness test reuses the extraction logic already applied downstream, so
"enough content" means the same thing here as in scan-quality classification.

### Unit 4 — Backend wiring (`crawler.py`)

`_select_backend()` returns `"http"` unless `AUDITOR_RENDER_BACKEND == "local"`.
`crawl_site` / `render_page` consume the chosen renderer through the existing
`page_renderer` argument, so injected test renderers still override everything and
`pipeline.py` keeps passing `page_renderer=None`.

- `http`: per-URL renderer is `_http_first_render`. `_browser_render_session`
  (shared browser) is used only for the pages that actually fall back — most
  crawls launch no browser at all.
- `local`: unchanged (`_browser_render_session` path).

The `_CRAWL_SLOTS` semaphore still gates any headless fallbacks; its memory bound
now applies only to the rare fallback pages, so concurrency can be relaxed later.

## Data flow (unchanged except the renderer)

`pipeline.run_audit` → `discovery.seed_urls` → `crawler.crawl_site(..., page_renderer=None)`.
Inside `crawl_site`, each URL is rendered by `_http_first_render` (HTTP, with
per-page headless fallback). Link extraction, ranking, robots, size caps,
extraction, scan-quality, scoring — all untouched.

## Error handling

- **HTTP transport/timeout/error** → mapped through the existing
  `_render_once` classification (`TIMEOUT` / `HTTP_ERROR` / `SOFT_BLOCKED` for
  403/429). A soft-block or thin body triggers the headless fallback (Unit 3), not
  an immediate failure.
- **Both HTTP and headless thin/blocked** (true SPA-behind-WAF) → honest
  `THIN` / `BLOCKED` as today. No silent fake report.
- SSRF pre-guard, post-redirect guard, size caps, robots — unchanged.

## Security

- HTTP path keeps full SSRF parity (see Global constraints) — this is the load-
  bearing security requirement of the change, since egress is from our network.
- No third-party data egress; customer page content is fetched directly by us,
  same trust boundary as the existing sitemap/robots fetches.

## Testing

Unit (injected-renderer pattern; no network):
- `_select_backend()` → `http` by default, `local` when overridden.
- `_curl_cffi_fetch`: mocked session asserts host-pin/`Host`/redirect re-guard,
  maps status/text/final_url; SSRF `ValueError` → `(0,"",url)`.
- `_http_first_render`: SSR HTML (≥ threshold) returns without invoking the
  headless fallback (assert fallback not called); thin HTML invokes the fallback.
- `crawl_site` with an injected renderer behaves identically (regression).

Live smoke (staging): audit `beyrictech.com` / `luranai.com` end-to-end; expect
COMPLETED with real content and **no headless launch** in logs for SSR pages.

## Operate / roll back

- **Enable:** default `AUDITOR_RENDER_BACKEND=http`; add `curl_cffi` to deps and
  the worker image; redeploy. Validate on staging first.
- **Roll back:** set `AUDITOR_RENDER_BACKEND=local` and redeploy — instant return
  to the prior headless path, no code change. Chromium stays in the image.
- After bake-in, memory/concurrency caps can be relaxed (separate follow-up).

## Follow-ups

1. Relax `AUDITOR_MAX_CONCURRENT_CRAWLS` / worker memory now that Chromium is off
   the hot path.
2. Metrics: count HTTP-served vs headless-fallback vs blocked pages, to size the
   residual SPA-behind-WAF tail with real data.
3. **Managed backend (ZenRows) as a documented, un-implemented option** behind the
   same seam — add ONLY if (2) shows the residual tail is material. Free tier
   5K credits/mo, no-KYC; `AUDITOR_RENDER_BACKEND=zenrows`.
4. Vercel/Cloudflare worker-IP allowlist for own sites — partial optimization, not
   a general fix.
