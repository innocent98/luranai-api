# Auditor SPA-aware Crawl + Honest Scan States — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Website Auditor discover and crawl JavaScript-rendered sites, and give every audit an honest scan outcome (`ok` / `thin` / `unreachable` / `render_failed`) exposed via the API.

**Architecture:** Replace the "discover (plain HTTP) then crawl (render)" split with "seed (cheap HTTP: root + sitemap) then render-crawl (one BFS loop that renders each page once, extracts links from the *rendered* DOM, and expands the frontier)". The renderer waits for client hydration so SPA content and links are visible. After extraction, a classifier stamps a scan-quality outcome on the report; unreadable outcomes fail the audit with a reason instead of a misleading 0% score.

**Tech Stack:** Python 3.11, FastAPI, sync SQLAlchemy, Alembic, Playwright (async Chromium), pytest. The auditor pipeline runs async inside the ARQ worker.

## Global Constraints

- Ground every requirement in `docs/luran_prd.md`; this plan fulfils Feature 4.1 (`:311–336`): follow internal links to depth 3 (`:316`), handle JS-rendered pages via headless browser (`:335`), respect `robots.txt` (`:332`), max pages per crawl per plan (`:334`), 1 req/sec polite crawl (`:336`).
- Commits in Luran repos MUST NOT include a Claude co-author trailer.
- Preserve every existing safety rail verbatim: SSRF IP-pinning (`resolve_and_validate_host` + Chromium `--host-resolver-rules` + subresource route interception), `robots.txt`, per-page timeout, content size cap, 1 rps politeness, `max_pages` per plan.
- The auditor pipeline must never raise to the ARQ entrypoint; per-page failures are captured on `RawPage`; `run_audit` wraps everything in a FAILED-marking try/except.
- Tests run in the API container against the test DB. Command form:
  `docker compose exec -T -e PYTHONPATH=/app api pytest <path> -v` (run from `luran/`).
- Branch: `fix/auditor-spa-crawl-scan-states` (already created off `develop`; the Chromium Dockerfile fix and the design spec are already committed on it).

## Interfaces at a glance (names later tasks rely on)

- `crawler.RawPageError` — `str, Enum`: `OK, LAUNCH_FAILED, TIMEOUT, HTTP_ERROR, BLOCKED, SIZE_CAP, ROBOTS`.
- `crawler.RawPage.error_kind: RawPageError` (default `OK`).
- `crawler.crawl_site(seeds: list[str], max_pages: int, *, page_renderer=None, robots_disallowed: set[str] | None = None, rps: int | None = None, skip_urls: set[str] | None = None) -> list[RawPage]`.
- `discovery.seed_urls(root_url: str, max_pages: int, fetch_text=None) -> list[str]` (replaces `discovery.discover`).
- `audit.ScanQuality` — `str, Enum`: `OK, THIN, UNREACHABLE, RENDER_FAILED`.
- `AuditReport.scan_quality: ScanQuality | None`, `AuditReport.scan_quality_reason: str | None`.
- `scan_quality.classify_scan_outcome(root_page, extracted, *, thin_min_content_chars) -> tuple[ScanQuality, str]`.
- `scan_quality.ScanUnreadable(Exception)` with `.quality`, `.reason`.
- Settings: `AUDITOR_HYDRATION_SETTLE_MS = 3000`, `AUDITOR_THIN_MIN_CONTENT_CHARS = 800`.

---

## Task 1: `RawPageError` enum + `RawPage.error_kind`

Deterministic, structured error categorisation on every rendered page, so the outcome classifier (Task 6) never string-matches exceptions.

**Files:**
- Modify: `app/auditor/crawler.py` (add enum, add field, set it on every `render_page` return path, set it on the robots-blocked path in `crawl`)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Produces: `crawler.RawPageError` enum; `RawPage.error_kind` field.

- [ ] **Step 1: Write the failing tests**

Append to `tests/auditor/test_crawler.py`:

```python
# ---------------------------------------------------------------------------
# error_kind classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_page_success_sets_error_kind_ok(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def renderer(url):
        return 200, "<html>ok</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert page.error is None
    assert page.error_kind == crawler.RawPageError.OK


@pytest.mark.asyncio
async def test_render_page_unsafe_url_error_kind_blocked(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: False)

    async def renderer(url):
        return 200, "<html></html>"

    page = await crawler.render_page("http://169.254.169.254/", page_renderer=renderer)
    assert page.error == "UNSAFE_URL"
    assert page.error_kind == crawler.RawPageError.BLOCKED


@pytest.mark.asyncio
async def test_render_page_timeout_error_kind_timeout(monkeypatch):
    import asyncio
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr(
        "app.auditor.crawler.settings",
        type("S", (), {"AUDITOR_PER_PAGE_TIMEOUT_S": 0,
                       "AUDITOR_CRAWL_RPS": 1,
                       "AUDITOR_HYDRATION_SETTLE_MS": 0})(),
    )

    async def slow(url):
        await asyncio.sleep(100)
        return 200, "<html></html>"

    page = await crawler.render_page("https://x.com/s", page_renderer=slow)
    assert page.error == "TIMEOUT"
    assert page.error_kind == crawler.RawPageError.TIMEOUT


@pytest.mark.asyncio
async def test_render_page_launch_failure_error_kind_launch_failed(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def broken(url):
        raise Exception(
            "BrowserType.launch: Executable doesn't exist at "
            "/home/appuser/.cache/ms-playwright/chromium/headless_shell"
        )

    page = await crawler.render_page("https://x.com/b", page_renderer=broken)
    assert page.error_kind == crawler.RawPageError.LAUNCH_FAILED


@pytest.mark.asyncio
async def test_render_page_generic_error_kind_http_error(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def broken(url):
        raise ConnectionError("Network unreachable")

    page = await crawler.render_page("https://x.com/c", page_renderer=broken)
    assert page.error_kind == crawler.RawPageError.HTTP_ERROR


@pytest.mark.asyncio
async def test_render_page_size_cap_error_kind(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def big(url):
        return 200, "x" * 50

    page = await crawler.render_page("https://x.com/a", page_renderer=big, max_bytes=10)
    assert page.error == "SIZE_CAP_EXCEEDED"
    assert page.error_kind == crawler.RawPageError.SIZE_CAP


@pytest.mark.asyncio
async def test_render_page_non_2xx_status_error_kind_http_error(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def renderer(url):
        return 404, "<html>not found</html>"

    page = await crawler.render_page("https://x.com/missing", page_renderer=renderer)
    assert page.error_kind == crawler.RawPageError.HTTP_ERROR
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py -k error_kind -v`
Expected: FAIL with `AttributeError: module 'app.auditor.crawler' has no attribute 'RawPageError'`.

- [ ] **Step 3: Add the enum and field**

In `app/auditor/crawler.py`, add the enum near the top (after the imports, before `RawPage`):

```python
from enum import Enum


class RawPageError(str, Enum):
    OK = "ok"
    LAUNCH_FAILED = "launch_failed"   # browser binary / launch failure (our infra)
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"         # network error, or status 0 / >= 400
    BLOCKED = "blocked"               # SSRF pre-guard or post-redirect guard
    SIZE_CAP = "size_cap"
    ROBOTS = "robots"
```

Add the field to the `RawPage` dataclass (after `final_url`):

```python
    error_kind: RawPageError = RawPageError.OK
```

- [ ] **Step 4: Set `error_kind` on every return path**

Rewrite the body of `render_page` so each `RawPage(...)` carries an `error_kind`. Replace the existing function body from the pre-render guard through the final return with:

```python
    # Pre-render SSRF guard.
    if not guard_url(url):
        logger.warning("render_page SSRF pre-guard blocked: {}", url)
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="UNSAFE_URL", error_kind=RawPageError.BLOCKED)

    renderer = page_renderer or _playwright_render
    try:
        result = await asyncio.wait_for(
            renderer(url),
            timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S
            + settings.AUDITOR_HYDRATION_SETTLE_MS / 1000
            + 2,
        )
    except TimeoutError:
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="TIMEOUT", error_kind=RawPageError.TIMEOUT)
    except Exception as exc:
        logger.debug("render_page error {}: {}", url, exc)
        msg = str(exc).lower()
        kind = (
            RawPageError.LAUNCH_FAILED
            if ("executable doesn't exist" in msg or "browsertype.launch" in msg)
            else RawPageError.HTTP_ERROR
        )
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error=str(exc), error_kind=kind)

    # Support both 2-tuple (status, html) and 3-tuple (status, html, final_url).
    if len(result) == 3:
        status, html, final_url = result
    else:
        status, html = result
        final_url = url

    # Post-redirect SSRF guard — discard body if the browser landed on an unsafe host.
    if final_url != url and not guard_url(final_url):
        logger.warning("render_page SSRF post-redirect guard blocked: {} -> {}", url, final_url)
        return RawPage(url=url, status=0, html=None, bytes_len=0,
                       error="UNSAFE_REDIRECT", error_kind=RawPageError.BLOCKED,
                       final_url=final_url)

    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(url=url, status=status, html=None, bytes_len=byte_len,
                       error="SIZE_CAP_EXCEEDED", error_kind=RawPageError.SIZE_CAP,
                       final_url=final_url)

    error_kind = RawPageError.OK if 200 <= status < 400 else RawPageError.HTTP_ERROR
    return RawPage(url=url, status=status, html=html, bytes_len=byte_len,
                   error_kind=error_kind, final_url=final_url)
```

Note: `render_page` now reads `settings.AUDITOR_HYDRATION_SETTLE_MS` in its timeout budget. That setting is added in Task 2; add it there. The `test_render_page_timeout_error_kind_timeout` test above injects a fake `settings` object that already includes `AUDITOR_HYDRATION_SETTLE_MS`.

In `crawl`, set the robots-blocked page's `error_kind`:

```python
        if not is_allowed_by_robots(url, robots_disallowed):
            logger.debug("robots blocked: {}", url)
            pages.append(
                RawPage(url=url, status=0, html=None, bytes_len=0,
                        error="ROBOTS_BLOCKED", error_kind=RawPageError.ROBOTS)
            )
            continue
```

- [ ] **Step 5: Add `AUDITOR_HYDRATION_SETTLE_MS` to settings now (needed by Step 4's timeout)**

In `app/core/config.py`, add after `AUDITOR_CRAWL_RPS: int = 1` (line ~214):

```python
    AUDITOR_HYDRATION_SETTLE_MS: int = 3000
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py -v`
Expected: PASS (all pre-existing crawler tests plus the 7 new `error_kind` tests).

- [ ] **Step 7: Commit**

```bash
git add app/auditor/crawler.py app/core/config.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): structured RawPage.error_kind classification"
```

---

## Task 2: Renderer waits for client hydration

Snapshot the page *after* the SPA paints, so rendered HTML contains JS-injected content and links.

**Files:**
- Modify: `app/auditor/crawler.py` (`_playwright_render`)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_HYDRATION_SETTLE_MS` (added in Task 1 Step 5).

- [ ] **Step 1: Write the failing test**

Append to `tests/auditor/test_crawler.py`:

```python
@pytest.mark.asyncio
async def test_playwright_render_waits_networkidle_then_captures(monkeypatch):
    """goto uses wait_until='load'; a networkidle settle is attempted; a settle
    timeout is swallowed and content() is still captured."""
    from unittest.mock import AsyncMock, MagicMock
    from playwright.async_api import TimeoutError as PWTimeout

    goto_kwargs = {}

    def make_mock_playwright():
        mock_resp = MagicMock()
        mock_resp.status = 200

        mock_page = AsyncMock()

        async def fake_goto(url, **kwargs):
            goto_kwargs.update(kwargs)
            return mock_resp

        mock_page.goto = fake_goto
        # networkidle never settles -> raises PWTimeout, which must be swallowed.
        mock_page.wait_for_load_state = AsyncMock(side_effect=PWTimeout("no idle"))
        mock_page.content = AsyncMock(return_value="<html>hydrated</html>")
        mock_page.url = "https://example.com/"
        mock_page.route = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()

        async def fake_launch(headless, args):
            return mock_browser

        mock_chromium.launch = fake_launch
        mock_p = MagicMock()
        mock_p.chromium = mock_chromium

        class FakeCtx:
            async def __aenter__(self):
                return mock_p

            async def __aexit__(self, *_):
                pass

        return FakeCtx()

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("app.auditor.crawler.async_playwright", side_effect=make_mock_playwright):
            status, html, final_url = await crawler._playwright_render("https://example.com/")

    assert status == 200
    assert html == "<html>hydrated</html>"          # captured despite settle timeout
    assert goto_kwargs.get("wait_until") == "load"   # not domcontentloaded
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py::test_playwright_render_waits_networkidle_then_captures -v`
Expected: FAIL (current code uses `wait_until="domcontentloaded"` and never calls `wait_for_load_state`; `goto_kwargs["wait_until"]` is `"domcontentloaded"`).

- [ ] **Step 3: Implement the hydration wait**

In `app/auditor/crawler.py`, update the import:

```python
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
```

In `_playwright_render`, replace the `resp = await page.goto(...)` block with:

```python
            resp = await page.goto(
                url,
                timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S * 1000,
                wait_until="load",
            )
            # Give client-rendered (SPA) pages time to hydrate so JS-injected
            # content and links are present. Best-effort: if the page never
            # reaches network idle (persistent connections, analytics, sockets),
            # swallow the timeout and snapshot whatever has rendered so far.
            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=settings.AUDITOR_HYDRATION_SETTLE_MS,
                )
            except PlaywrightTimeoutError:
                pass
            html = await page.content()
            final_url = page.url or url
            return (resp.status if resp else 0), html, final_url
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py -v`
Expected: PASS (new test + all existing Playwright mock tests still green).

- [ ] **Step 5: Commit**

```bash
git add app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): render waits for client hydration (networkidle, best-effort)"
```

---

## Task 3: `crawl_site` — unified render-crawl with link expansion

One BFS loop that renders each page once and expands the frontier from the *rendered* DOM. This is what makes SPA discovery work (PRD 4.1 depth-3 link following via the headless browser).

**Files:**
- Modify: `app/auditor/crawler.py` (add `crawl_site` + `_MAX_DEPTH`)
- Test: `tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `discovery.normalize_url`, `discovery.is_structurally_prunable`, `discovery.guard_url`, `discovery._extract_links`; `render_page`; `is_allowed_by_robots`.
- Produces: `crawler.crawl_site(seeds, max_pages, *, page_renderer=None, robots_disallowed=None, rps=None, skip_urls=None) -> list[RawPage]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/auditor/test_crawler.py`:

```python
# ---------------------------------------------------------------------------
# crawl_site — unified render + link expansion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crawl_site_expands_frontier_from_rendered_links(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    html_by_url = {
        "https://x.com/": '<html><a href="/features">F</a><a href="/faq">Q</a></html>',
        "https://x.com/features": "<html>features</html>",
        "https://x.com/faq": "<html>faq</html>",
    }

    async def renderer(url):
        return 200, html_by_url.get(url, "<html>empty</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer
    )
    urls = {p.url for p in pages}
    assert "https://x.com/" in urls
    assert "https://x.com/features" in urls   # discovered from rendered root
    assert "https://x.com/faq" in urls


@pytest.mark.asyncio
async def test_crawl_site_respects_max_pages_budget(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        # every page links to two fresh pages -> unbounded without a cap
        return 200, f'<html><a href="{url}a">A</a><a href="{url}b">B</a></html>'

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=5, rps=1000, page_renderer=renderer
    )
    assert len(pages) == 5


@pytest.mark.asyncio
async def test_crawl_site_enforces_depth_cap(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    # A chain deeper than _MAX_DEPTH: / -> /1 -> /2 -> /3 -> /4
    chain = {
        "https://x.com/": '<html><a href="/1">1</a></html>',
        "https://x.com/1": '<html><a href="/2">2</a></html>',
        "https://x.com/2": '<html><a href="/3">3</a></html>',
        "https://x.com/3": '<html><a href="/4">4</a></html>',
        "https://x.com/4": "<html>deep</html>",
    }

    async def renderer(url):
        return 200, chain.get(url, "<html>x</html>")

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer
    )
    urls = {p.url for p in pages}
    # depth 0..3 reachable (/, /1, /2, /3); /4 is depth 4 -> excluded
    assert "https://x.com/3" in urls
    assert "https://x.com/4" not in urls


@pytest.mark.asyncio
async def test_crawl_site_stays_on_same_host(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        return 200, '<html><a href="https://evil.com/x">E</a><a href="/ok">O</a></html>'

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer
    )
    urls = {p.url for p in pages}
    assert "https://x.com/ok" in urls
    assert not any("evil.com" in u for u in urls)


@pytest.mark.asyncio
async def test_crawl_site_skips_urls_and_dedupes(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    rendered = []

    async def renderer(url):
        rendered.append(url)
        return 200, '<html><a href="/seen">S</a><a href="/new">N</a></html>'

    pages = await crawler.crawl_site(
        ["https://x.com/", "https://x.com/"],   # duplicate seed
        max_pages=50, rps=1000, page_renderer=renderer,
        skip_urls={"https://x.com/seen"},
    )
    # root rendered once (dedup); /seen skipped; /new rendered
    assert rendered.count("https://x.com/") == 1
    assert "https://x.com/seen" not in rendered
    assert "https://x.com/new" in rendered


@pytest.mark.asyncio
async def test_crawl_site_respects_robots(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        return 200, '<html><a href="/private/x">P</a><a href="/pub">Y</a></html>'

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer,
        robots_disallowed={"/private"},
    )
    blocked = [p for p in pages if "/private" in p.url]
    # /private/x is enqueued (link found) but rendered as ROBOTS-blocked, no html
    assert blocked and blocked[0].error == "ROBOTS_BLOCKED"
    assert blocked[0].html is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py -k crawl_site -v`
Expected: FAIL with `AttributeError: module 'app.auditor.crawler' has no attribute 'crawl_site'`.

- [ ] **Step 3: Implement `crawl_site`**

In `app/auditor/crawler.py`, ensure the module imports `discovery` (top of file):

```python
from app.auditor import discovery
```

(Keep the existing `from app.auditor.discovery import guard_url` import; both forms coexist.)

Add the depth constant near the top (after `RawPageError`):

```python
_MAX_DEPTH = 3  # PRD 4.1: follow internal links up to 3 levels deep
```

Add the function after `crawl`:

```python
async def crawl_site(
    seeds: list[str],
    max_pages: int,
    *,
    page_renderer=None,
    robots_disallowed: set[str] | None = None,
    rps: int | None = None,
    skip_urls: set[str] | None = None,
) -> list[RawPage]:
    """Render + BFS-expand from seed URLs in a single loop.

    Renders each same-host, non-pruned, SSRF-safe URL exactly once (browser),
    then extracts links from the *rendered* HTML and enqueues newly discovered
    same-host links up to _MAX_DEPTH and max_pages. Reuses the rendered pages so
    the caller need not fetch them again.

    Args:
        seeds: Seed URLs (root + sitemap locs). The first seed's host defines the
            same-host boundary for the whole crawl.
        max_pages: Hard cap on the number of pages rendered.
        page_renderer: Injectable async ``(url) -> (status, html[, final_url])``.
        robots_disallowed: Disallowed path prefixes from fetch_robots().
        rps: Requests/sec cap (defaults to settings.AUDITOR_CRAWL_RPS).
        skip_urls: URLs to never render (already-seen entries during an
            incremental resume). They are pre-marked seen so they are neither
            rendered nor enqueued.
    """
    rps = rps if rps is not None else settings.AUDITOR_CRAWL_RPS
    delay = 1.0 / rps if rps > 0 else 0.0
    robots_disallowed = robots_disallowed or set()

    seen: set[str] = {n for n in (discovery.normalize_url(u) for u in (skip_urls or set())) if n}

    frontier: deque[tuple[str, int]] = deque()
    root_host = ""
    for raw in seeds:
        n = discovery.normalize_url(raw)
        if not n or n in seen:
            continue
        if not root_host:
            root_host = urlparse(n).netloc
        seen.add(n)
        frontier.append((n, 0))

    pages: list[RawPage] = []
    while frontier and len(pages) < max_pages:
        url, depth = frontier.popleft()

        if not is_allowed_by_robots(url, robots_disallowed):
            pages.append(
                RawPage(url=url, status=0, html=None, bytes_len=0,
                        error="ROBOTS_BLOCKED", error_kind=RawPageError.ROBOTS)
            )
            continue

        page = await render_page(url, page_renderer=page_renderer)
        pages.append(page)
        if delay:
            await asyncio.sleep(delay)

        if not page.html or depth + 1 > _MAX_DEPTH:
            continue

        for link in discovery._extract_links(page.html, url):
            if link in seen:
                continue
            if urlparse(link).netloc != root_host:
                continue
            if discovery.is_structurally_prunable(link):
                seen.add(link)
                continue
            if not discovery.guard_url(link):
                seen.add(link)
                continue
            seen.add(link)
            frontier.append((link, depth + 1))

    return pages
```

Ensure `from collections import deque` and `from urllib.parse import urlparse` are imported in `crawler.py` (add if missing).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_crawler.py -v`
Expected: PASS (all crawl_site tests + all prior crawler tests).

- [ ] **Step 5: Commit**

```bash
git add app/auditor/crawler.py tests/auditor/test_crawler.py
git commit -m "feat(auditor): crawl_site unified render + BFS link expansion"
```

---

## Task 4: `discovery.seed_urls` replaces `discovery.discover`

Discovery becomes a cheap seed provider (root + sitemap only). The HTTP-BFS link-following is removed — that job now belongs to `crawl_site`, which sees rendered links.

**Files:**
- Modify: `app/auditor/discovery.py` (rename `discover` → `seed_urls`; drop the BFS Phase 2; remove the now-unused `_MAX_DEPTH` and `deque` import)
- Test: `tests/auditor/test_discovery.py`

**Interfaces:**
- Produces: `discovery.seed_urls(root_url, max_pages, fetch_text=None) -> list[str]`.
- Unchanged and still exported: `normalize_url`, `is_structurally_prunable`, `guard_url`, `_extract_links`, `_parse_sitemap`, `_default_fetch_text`.

- [ ] **Step 1: Update the discovery tests**

In `tests/auditor/test_discovery.py`:

1. Rename every `discovery.discover(` call to `discovery.seed_urls(` in these tests (mechanical): `test_discover_uses_sitemap_prunes_caps_and_guards`, `test_discover_drops_ssrf_blocked_urls`, `test_discover_ssrf_guard_called_for_every_candidate_url`, `test_discover_rejected_urls_excluded_from_result`, `test_discover_respects_max_pages_cap`, `test_discover_returns_empty_when_root_fails_guard`, `test_discover_deduplicates_urls`, `test_discover_excludes_cross_domain_urls`, `test_discover_sitemap_ssrf_blocked_host_skips_fetch`.
2. **Delete** `test_discover_bfs_fallback_when_no_sitemap` (its behavior moves to `crawl_site`, covered by `test_crawl_site_expands_frontier_from_rendered_links`).
3. Add a test asserting no BFS happens (sitemap absent → only root):

```python
@pytest.mark.asyncio
async def test_seed_urls_no_sitemap_returns_root_only(monkeypatch):
    async def fake_fetch(url):
        return None  # no sitemap

    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    found = await discovery.seed_urls("https://ex.com", max_pages=10, fetch_text=fake_fetch)
    assert found == ["https://ex.com/"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_discovery.py -v`
Expected: FAIL with `AttributeError: module 'app.auditor.discovery' has no attribute 'seed_urls'`.

- [ ] **Step 3: Replace `discover` with `seed_urls`**

In `app/auditor/discovery.py`, replace the entire `discover` function with:

```python
async def seed_urls(root_url: str, max_pages: int, fetch_text=None) -> list[str]:
    """Return seed URLs for the crawl: the root plus sitemap.xml <loc> URLs.

    Cheap HTTP-only seeding. Link discovery on rendered pages is done later by
    crawler.crawl_site (which can see JS-injected links). All candidate URLs are
    normalized, structurally pruned, SSRF-guarded, same-host filtered, deduped,
    and capped at max_pages.
    """
    fetch_text = fetch_text or _default_fetch_text
    root = normalize_url(root_url)
    if not root or not guard_url(root):
        return []
    root_host = urlparse(root).netloc

    found: list[str] = []
    seen: set[str] = set()

    def _accept(url: str) -> bool:
        if url in seen:
            return False
        if urlparse(url).netloc != root_host:
            return False
        if is_structurally_prunable(url):
            seen.add(url)
            return False
        if not guard_url(url):
            seen.add(url)
            return False
        return True

    if _accept(root):
        seen.add(root)
        found.append(root)

    sitemap_url = urljoin(root + "/", "sitemap.xml")
    if not guard_url(sitemap_url):
        logger.warning("seed_urls SSRF guard blocked sitemap URL: {}", sitemap_url)
        return found[:max_pages]

    sitemap = await fetch_text(sitemap_url)
    if sitemap:
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if n and _accept(n):
                seen.add(n)
                found.append(n)
                if len(found) >= max_pages:
                    return found[:max_pages]

    return found[:max_pages]
```

Remove the now-unused `_MAX_DEPTH = 3` constant and the `from collections import deque` import at the top of `discovery.py` (the BFS loop that used them is gone). Keep all other functions.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_discovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/auditor/discovery.py tests/auditor/test_discovery.py
git commit -m "refactor(auditor): discover -> seed_urls (root + sitemap only)"
```

---

## Task 5: `ScanQuality` enum + `AuditReport` fields + migration

The persisted contract for honest scan outcomes.

**Files:**
- Modify: `app/db/models/audit.py` (add enum + 2 columns)
- Create: `alembic/versions/<rev>_add_audit_scan_quality.py`
- Test: `tests/auditor/test_audit_models.py`

**Interfaces:**
- Produces: `audit.ScanQuality` enum; `AuditReport.scan_quality`, `AuditReport.scan_quality_reason`.

- [ ] **Step 1: Write the failing test**

Append to `tests/auditor/test_audit_models.py`:

```python
def test_audit_report_scan_quality_fields(db):
    import uuid
    from app.db.models.audit import AuditReport, AuditScope, AuditStatus, ScanQuality

    r = AuditReport(
        tenant_id=None,
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        scan_quality=ScanQuality.THIN,
        scan_quality_reason="Only the homepage could be read.",
    )
    db.add(r)
    db.flush()
    db.refresh(r)
    assert r.scan_quality == ScanQuality.THIN
    assert r.scan_quality_reason == "Only the homepage could be read."


def test_audit_report_scan_quality_defaults_none(db):
    import uuid
    from app.db.models.audit import AuditReport, AuditScope, AuditStatus

    r = AuditReport(tenant_id=None, url="https://ex.com", domain="ex.com",
                    scope=AuditScope.PUBLIC, status=AuditStatus.QUEUED)
    db.add(r)
    db.flush()
    db.refresh(r)
    assert r.scan_quality is None
    assert r.scan_quality_reason is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_audit_models.py -k scan_quality -v`
Expected: FAIL with `ImportError: cannot import name 'ScanQuality'`.

- [ ] **Step 3: Add the enum and columns**

In `app/db/models/audit.py`, add the enum after `AuditStatus` (line ~160):

```python
class ScanQuality(str, enum.Enum):
    OK = "ok"
    THIN = "thin"
    UNREACHABLE = "unreachable"
    RENDER_FAILED = "render_failed"
```

Add the columns to `AuditReport` (after `completed_at`, line ~234):

```python
    scan_quality: Mapped[ScanQuality | None] = mapped_column(
        _audit_enum(ScanQuality, "audit_scan_quality"), nullable=True, default=None
    )
    scan_quality_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 4: Create the Alembic migration**

Create `alembic/versions/a1b2c3d4e5f6_add_audit_scan_quality.py`:

```python
"""add audit scan_quality + scan_quality_reason

Revision ID: a1b2c3d4e5f6
Revises: 9b886c489f29
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "9b886c489f29"
branch_labels = None
depends_on = None

SCAN_QUALITY = sa.Enum(
    "ok", "thin", "unreachable", "render_failed", name="audit_scan_quality"
)


def upgrade() -> None:
    bind = op.get_bind()
    SCAN_QUALITY.create(bind, checkfirst=True)
    op.add_column(
        "audit_reports",
        sa.Column("scan_quality", SCAN_QUALITY, nullable=True),
    )
    op.add_column(
        "audit_reports",
        sa.Column("scan_quality_reason", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_reports", "scan_quality_reason")
    op.drop_column("audit_reports", "scan_quality")
    SCAN_QUALITY.drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 5: Verify the migration applies on the test DB and run the test**

Run: `docker compose exec -T -e PYTHONPATH=/app api alembic upgrade head`
Expected: applies `a1b2c3d4e5f6` with no error (`Running upgrade 9b886c489f29 -> a1b2c3d4e5f6`).

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_audit_models.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db/models/audit.py alembic/versions/a1b2c3d4e5f6_add_audit_scan_quality.py tests/auditor/test_audit_models.py
git commit -m "feat(auditor): persist scan_quality + reason on audit_reports"
```

---

## Task 6: `scan_quality` classifier module

Deterministic outcome from the root page's `error_kind` and total extracted content.

**Files:**
- Create: `app/auditor/scan_quality.py`
- Modify: `app/core/config.py` (add `AUDITOR_THIN_MIN_CONTENT_CHARS`)
- Test: `tests/auditor/test_scan_quality.py`

**Interfaces:**
- Consumes: `crawler.RawPage`, `crawler.RawPageError`, `audit.ScanQuality`.
- Produces: `scan_quality.classify_scan_outcome(...)`, `scan_quality.ScanUnreadable`.

- [ ] **Step 1: Write the failing tests**

Create `tests/auditor/test_scan_quality.py`:

```python
"""Tests for app/auditor/scan_quality.py — outcome classification."""

from app.auditor.crawler import RawPage, RawPageError
from app.auditor.scan_quality import classify_scan_outcome
from app.db.models.audit import ScanQuality


def _extracted(total_chars: int):
    """Return a list with one object exposing .text of the given length."""
    class _P:
        text = "x" * total_chars
    return [_P()] if total_chars else []


def _root(error_kind, html="<html>ok</html>"):
    return RawPage(url="https://x.com/", status=200 if error_kind == RawPageError.OK else 0,
                   html=html if error_kind == RawPageError.OK else None,
                   bytes_len=len(html), error_kind=error_kind)


def test_render_failed_when_root_launch_failed():
    q, reason = classify_scan_outcome(_root(RawPageError.LAUNCH_FAILED), _extracted(0),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.RENDER_FAILED
    assert reason


def test_render_failed_when_root_missing():
    q, reason = classify_scan_outcome(None, _extracted(0), thin_min_content_chars=800)
    assert q == ScanQuality.RENDER_FAILED


def test_unreachable_when_root_timeout():
    q, reason = classify_scan_outcome(_root(RawPageError.TIMEOUT), _extracted(0),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE
    assert reason


def test_unreachable_when_root_http_error():
    q, _ = classify_scan_outcome(_root(RawPageError.HTTP_ERROR), _extracted(0),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE


def test_unreachable_when_root_blocked():
    q, _ = classify_scan_outcome(_root(RawPageError.BLOCKED), _extracted(0),
                                 thin_min_content_chars=800)
    assert q == ScanQuality.UNREACHABLE


def test_thin_when_root_ok_but_little_content():
    q, reason = classify_scan_outcome(_root(RawPageError.OK), _extracted(100),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.THIN
    assert reason


def test_ok_when_root_ok_and_enough_content():
    q, reason = classify_scan_outcome(_root(RawPageError.OK), _extracted(1200),
                                      thin_min_content_chars=800)
    assert q == ScanQuality.OK
    assert reason == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_scan_quality.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auditor.scan_quality'`.

- [ ] **Step 3: Implement the classifier**

Create `app/auditor/scan_quality.py`:

```python
"""Classify the outcome of an audit crawl into an honest scan-quality state."""

from __future__ import annotations

from app.auditor.crawler import RawPage, RawPageError
from app.db.models.audit import ScanQuality

_UNREACHABLE_KINDS = {
    RawPageError.TIMEOUT,
    RawPageError.HTTP_ERROR,
    RawPageError.BLOCKED,
}

_RENDER_FAILED_REASON = (
    "Something went wrong on our end while reading your site. Please try again."
)
_UNREACHABLE_REASON = "We couldn't reach your website. Check the URL and try again."
_THIN_REASON = (
    "We could only partially read your site — you may get a fuller picture by "
    "retrying or importing content manually."
)


class ScanUnreadable(Exception):
    """Raised when a crawl produced no usable content (unreachable / render_failed).

    Carries the quality + reason so the pipeline can persist them before the
    report is marked FAILED.
    """

    def __init__(self, quality: ScanQuality, reason: str) -> None:
        self.quality = quality
        self.reason = reason
        super().__init__(reason)


def classify_scan_outcome(
    root_page: RawPage | None,
    extracted,
    *,
    thin_min_content_chars: int,
) -> tuple[ScanQuality, str]:
    """Return (quality, reason) for a completed crawl.

    - RENDER_FAILED: our renderer could not launch, or the root page is missing.
    - UNREACHABLE:   the root page timed out, errored, or was SSRF-blocked.
    - THIN:          the root rendered but total extracted content is below the
                     threshold (e.g. only an empty SPA shell was captured).
    - OK:            otherwise.
    """
    if root_page is None or root_page.error_kind == RawPageError.LAUNCH_FAILED:
        return ScanQuality.RENDER_FAILED, _RENDER_FAILED_REASON
    if root_page.error_kind in _UNREACHABLE_KINDS:
        return ScanQuality.UNREACHABLE, _UNREACHABLE_REASON
    total_chars = sum(len(p.text) for p in extracted)
    if total_chars < thin_min_content_chars:
        return ScanQuality.THIN, _THIN_REASON
    return ScanQuality.OK, ""
```

- [ ] **Step 4: Add the threshold setting**

In `app/core/config.py`, add after `AUDITOR_HYDRATION_SETTLE_MS: int = 3000` (added in Task 1):

```python
    AUDITOR_THIN_MIN_CONTENT_CHARS: int = 800
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_scan_quality.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/auditor/scan_quality.py app/core/config.py tests/auditor/test_scan_quality.py
git commit -m "feat(auditor): scan-outcome classifier (ok/thin/unreachable/render_failed)"
```

---

## Task 7: Rewire the pipeline (seed → crawl_site → classify outcome)

Wire the new pieces into `run_audit`, persisting the scan outcome and failing unreadable audits with a reason.

**Files:**
- Modify: `app/auditor/pipeline.py` (Phase 1b/2 + new Phase 2b)
- Test: `tests/auditor/test_pipeline.py`

**Interfaces:**
- Consumes: `discovery.seed_urls`, `crawler.crawl_site`, `scan_quality.classify_scan_outcome`, `scan_quality.ScanUnreadable`, `audit.ScanQuality`, `settings.AUDITOR_THIN_MIN_CONTENT_CHARS`, `discovery.normalize_url`.

- [ ] **Step 1: Update the existing resume test's patch target + add outcome tests**

In `tests/auditor/test_pipeline.py`, in `test_incremental_resume_appends_only_new_urls`, change the patch target from `discover` to `seed_urls`:

```python
        patch(
            "app.auditor.discovery.seed_urls",
            AsyncMock(return_value=all_discovered),
        ),
```

Append new outcome tests:

```python
# ---------------------------------------------------------------------------
# Test 8: scan outcome — unreachable root fails the audit with a reason
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_unreachable_root_fails_with_reason(db):
    from app.db.models.audit import ScanQuality
    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def refusing_renderer(url):
        raise ConnectionError("connection refused")

    async def no_sitemap(url):
        return None

    out = await pipeline.run_audit(
        db, report.id, page_renderer=refusing_renderer, fetch_text=no_sitemap
    )
    db.refresh(out)
    assert out.status == AuditStatus.FAILED
    assert out.scan_quality == ScanQuality.UNREACHABLE
    assert out.scan_quality_reason  # non-empty


# ---------------------------------------------------------------------------
# Test 9: scan outcome — browser launch failure => render_failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_launch_failure_is_render_failed(db):
    from app.db.models.audit import ScanQuality
    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def no_browser(url):
        raise Exception("BrowserType.launch: Executable doesn't exist at /x")

    async def no_sitemap(url):
        return None

    out = await pipeline.run_audit(
        db, report.id, page_renderer=no_browser, fetch_text=no_sitemap
    )
    db.refresh(out)
    assert out.status == AuditStatus.FAILED
    assert out.scan_quality == ScanQuality.RENDER_FAILED


# ---------------------------------------------------------------------------
# Test 10: scan outcome — thin content completes but is flagged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_thin_content_completes_flagged(db):
    from app.db.models.audit import ScanQuality
    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    async def tiny_renderer(url):
        return 200, "<html><body><p>hi</p></body></html>"

    async def no_sitemap(url):
        return None

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=tiny_renderer, fetch_text=no_sitemap
        )
    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.scan_quality == ScanQuality.THIN


# ---------------------------------------------------------------------------
# Test 11: scan outcome — rich multi-page SPA => ok, multiple pages scanned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_audit_multipage_ok(db):
    from app.db.models.audit import ScanQuality
    job, report = _make_job_and_report(db, tenant_id=uuid.uuid4())

    long_text = "Luran builds AI customer agents. " * 60  # > 800 chars/page
    html_by_url = {
        "https://ex.com/": f'<html><body><article><p>{long_text}</p>'
                           f'<a href="/features">F</a></article></body></html>',
        "https://ex.com/features": f'<html><body><article><p>{long_text} features</p>'
                                   f'</article></body></html>',
    }

    async def spa_renderer(url):
        return 200, html_by_url.get(url, "<html><body><p>x</p></body></html>")

    async def no_sitemap(url):
        return None

    with (
        patch("app.auditor.profile.complete_json", AsyncMock(return_value=_PROF)),
        patch("app.auditor.classify.complete_json", AsyncMock(return_value=_CLS)),
        patch("app.auditor.gaps.complete_json", AsyncMock(return_value=_EXPECTED)),
    ):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=spa_renderer, fetch_text=no_sitemap
        )
    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.scan_quality == ScanQuality.OK
    assert out.pages_scanned >= 2  # root + /features discovered from rendered link
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_pipeline.py -k "unreachable or launch_failure or thin_content or multipage" -v`
Expected: FAIL (`seed_urls`/`crawl_site` not wired; `scan_quality` never set).

- [ ] **Step 3: Rewire Phase 1b + Phase 2**

In `app/auditor/pipeline.py`, replace the Phase 1b discovery block (currently `all_urls = await discovery.discover(...)` through `db.commit()` at the end of Phase 1b) with:

```python
        # ------------------------------------------------------------------ #
        # Phase 1b: seed URLs (root + sitemap.xml)                            #
        # ------------------------------------------------------------------ #
        cap = _max_pages(db, report)
        seeds = await discovery.seed_urls(report.url, cap, fetch_text=fetch_text)

        report.pages_discovered = len(already_have) + len(seeds)
        _update_job(
            db, report.processing_job_id,
            total_items=len(existing_entries) + len(seeds),
            processed_items=0,
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 2: render-crawl (BFS from seeds) + extract                    #
        # ------------------------------------------------------------------ #
        robots = await crawler.fetch_robots(report.url, fetch_text=fetch_text)
        raw_pages = await crawler.crawl_site(
            seeds,
            cap,
            page_renderer=page_renderer,
            robots_disallowed=robots,
            skip_urls=already_have if resume else None,
        )
        # pages_discovered now reflects what the crawl actually reached.
        report.pages_discovered = len(already_have) + len(raw_pages)
        extracted = extraction.extract_pages(raw_pages)
        report.pages_scanned = len(existing_entries) + len(extracted)
        _update_job(
            db, report.processing_job_id,
            processed_items=len(existing_entries) + len(extracted),
        )
        db.commit()

        # ------------------------------------------------------------------ #
        # Phase 2b: classify scan outcome (honest states)                    #
        # ------------------------------------------------------------------ #
        from app.auditor.scan_quality import classify_scan_outcome, ScanUnreadable
        from app.db.models.audit import ScanQuality

        if resume:
            report.scan_quality = ScanQuality.OK
            report.scan_quality_reason = None
            db.commit()
        else:
            root_norm = discovery.normalize_url(report.url)
            root_page = next(
                (p for p in raw_pages if p.url == root_norm),
                raw_pages[0] if raw_pages else None,
            )
            quality, reason = classify_scan_outcome(
                root_page, extracted,
                thin_min_content_chars=settings.AUDITOR_THIN_MIN_CONTENT_CHARS,
            )
            report.scan_quality = quality
            report.scan_quality_reason = reason or None
            db.commit()  # persist BEFORE any raise so the FAILED path keeps it
            if quality in (ScanQuality.UNREACHABLE, ScanQuality.RENDER_FAILED):
                raise ScanUnreadable(quality, reason)
```

Leave Phases 3–8 unchanged. The `ScanUnreadable` raise is caught by the existing generic `except Exception` block, which marks the report + job FAILED; because `scan_quality`/`reason` were committed first, they survive the `expire_all()` + reload in that recovery block.

- [ ] **Step 4: Run the full pipeline test file**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor/test_pipeline.py -v`
Expected: PASS — the 4 new outcome tests plus all 7 pre-existing tests. (Note: `test_run_audit_end_to_end_with_mocked_llm` still passes: its tiny fixture content classifies as THIN, which still COMPLETES with a score, satisfying every assertion in that test.)

- [ ] **Step 5: Commit**

```bash
git add app/auditor/pipeline.py tests/auditor/test_pipeline.py
git commit -m "feat(auditor): pipeline uses seed_urls + crawl_site + scan-outcome gating"
```

---

## Task 8: Expose `scan_quality` on the API

Surface the new fields so the frontend (separate plan) can render honest states.

**Files:**
- Modify: `app/schemas/auditor.py` (`PublicReportResponse`)
- Modify: `app/api/v1/endpoints/public_auditor.py` (`get_public_report`)
- Modify: `app/api/v1/endpoints/auditor.py` (`list_audits`)
- Test: `tests/api/test_auditor_entry_detail.py` (or a new `tests/api/test_auditor_scan_quality.py`)

**Interfaces:**
- Consumes: `AuditReport.scan_quality`, `AuditReport.scan_quality_reason`.

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_auditor_scan_quality.py`:

```python
"""API exposes scan_quality on public + dashboard auditor responses."""

import uuid

from app.db.models.audit import AuditReport, AuditScope, AuditStatus, ScanQuality


def test_public_report_includes_scan_quality(client, db):
    r = AuditReport(
        tenant_id=None, url="https://ex.com", domain="ex.com",
        scope=AuditScope.PUBLIC, status=AuditStatus.FAILED,
        scan_quality=ScanQuality.UNREACHABLE,
        scan_quality_reason="We couldn't reach your website.",
        pages_scanned=0, gaps=[],
    )
    db.add(r)
    db.commit()

    resp = client.get(f"/api/v1/public/audit/{r.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scan_quality"] == "unreachable"
    assert data["scan_quality_reason"] == "We couldn't reach your website."
```

Note: the public report route is `GET /api/v1/public/audit/{audit_id}` (`public_auditor.router` mounted at prefix `/public` under the `/api/v1` router; handler is `@router.get("/audit/{audit_id}")`). If a `client`/`db` fixture pair is not already available for API tests, mirror the setup used in `tests/api/test_auditor_entry_detail.py`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/api/test_auditor_scan_quality.py -v`
Expected: FAIL (`KeyError: 'scan_quality'` — field not in the response).

- [ ] **Step 3: Add fields to the schema**

In `app/schemas/auditor.py`, add to `PublicReportResponse`:

```python
class PublicReportResponse(AuditScoresResponse):
    id: UUID
    status: str
    pages_scanned: int
    gaps: list[BlurredGap]
    scan_quality: str | None = None
    scan_quality_reason: str | None = None
```

- [ ] **Step 4: Populate them in the public endpoint**

In `app/api/v1/endpoints/public_auditor.py`, in `get_public_report`, add to the `PublicReportResponse(...)` constructor call:

```python
        scan_quality=report.scan_quality.value if report.scan_quality else None,
        scan_quality_reason=report.scan_quality_reason,
```

- [ ] **Step 5: Add fields to the dashboard `list_audits` rows**

In `app/api/v1/endpoints/auditor.py`, in `list_audits`, extend each row dict:

```python
            {
                "id": str(r.id),
                "url": r.url,
                "status": r.status.value,
                "overall_score": r.overall_score,
                "pages_scanned": r.pages_scanned,
                "scan_quality": r.scan_quality.value if r.scan_quality else None,
                "scan_quality_reason": r.scan_quality_reason,
                "created_at": r.created_at.isoformat(),
            }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `docker compose exec -T -e PYTHONPATH=/app api pytest tests/api/test_auditor_scan_quality.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/auditor.py app/api/v1/endpoints/public_auditor.py app/api/v1/endpoints/auditor.py tests/api/test_auditor_scan_quality.py
git commit -m "feat(auditor): expose scan_quality on public + dashboard responses"
```

---

## Final verification (after all tasks)

- [ ] **Full auditor suite green:**
  `docker compose exec -T -e PYTHONPATH=/app api pytest tests/auditor tests/api/test_auditor_scan_quality.py -v`
  Expected: all pass.

- [ ] **No regressions across the backend suite:**
  `docker compose exec -T -e PYTHONPATH=/app api pytest -q`
  Expected: same pass count as before + the new tests (no new failures).

- [ ] **Live smoke against a real SPA** (the bug that started this):
  Re-run a fresh audit of `https://weysure.luranai.com` end-to-end (via the public start endpoint or `run_website_audit`), then assert on the report row:
  `pages_scanned >= 2`, `scan_quality == 'ok'`, `overall_score` non-zero, `pages_discovered` > 1. This proves the SPA is now traversed (the 14 nav links found in the design validation) and scored honestly.

## Notes for the executor

- Dev DB (5432) is bootstrapped via `create_all` and picks up the two new columns automatically; the Alembic migration (Task 5) is what makes prod correct. Run `alembic upgrade head` against the **test** DB (5433) so the API-test suite has the columns.
- Do not touch classification, scoring, gap analysis, profile synthesis, or notification code — this plan is crawl + outcome + contract only.
- Keep every SSRF/robots/rps/size-cap guard exactly as-is; the reviewer will reject any weakening of them.
