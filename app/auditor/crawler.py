"""Playwright render + robots.txt + 1rps politeness + per-page timeout/size caps."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urljoin, urlparse

import httpx
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from loguru import logger
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import settings
from app.auditor import discovery
from app.auditor.discovery import guard_url
from app.utils.network_security import resolve_and_validate_host

# How many crawls may hold a Chromium at once.
#
# The bound is memory, not CPU. A browser with one page measured 394 MiB in the
# worker container, and the worker's ARQ settings allow max_jobs=5 — five audits
# can start together and ask for five browsers. Against WORKER_MEM_LIMIT=1200m:
#   1 crawl   554 MiB (160 base + 394)   fits
#   2 crawls  948 MiB                    fits
#   3 crawls 1342 MiB                    OOM kill
# and an OOM is not quiet — ARQ retries (max_tries=3), so the audit burns its
# retries and reports "failed" to the tenant.
#
# max_jobs stays at 5 so the cheap jobs (email, Slack, CRM) keep their slots;
# only the expensive one is throttled. Raise this only together with
# WORKER_MEM_LIMIT: 160 + 394 * N MiB.
MAX_CONCURRENT_CRAWLS: int = settings.AUDITOR_MAX_CONCURRENT_CRAWLS

# Module-level and shared by every job in the worker: ARQ runs max_jobs as
# asyncio tasks in a single process and event loop, so one semaphore gates them.
_CRAWL_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_CRAWLS)


class RawPageError(str, Enum):
    OK = "ok"
    LAUNCH_FAILED = "launch_failed"  # browser binary / launch failure (our infra)
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"  # network error, or status 0 / >= 400
    SOFT_BLOCKED = "soft_blocked"  # HTTP 403/429 — bot firewall / rate limit; retryable
    BLOCKED = "blocked"  # SSRF pre-guard or post-redirect guard
    SIZE_CAP = "size_cap"
    ROBOTS = "robots"


_MAX_DEPTH = 3  # PRD 4.1: follow internal links up to 3 levels deep


@dataclass
class RawPage:
    url: str
    status: int
    html: str | None
    bytes_len: int
    error: str | None = None
    # The final URL after any redirects (defaults to the requested URL).
    # Added for post-redirect SSRF guard; safe to ignore by callers that don't
    # care about redirects.
    final_url: str | None = None
    error_kind: RawPageError = RawPageError.OK


async def _default_fetch_text(url: str) -> str | None:
    """Fetch URL text with SSRF guard and DNS-rebinding protection.

    Security notes:
    - Rejects non-http/https schemes before any resolution.
    - Resolves the hostname ONCE via resolve_and_validate_host and pins the
      connection to the returned IP so DNS cannot be rebound between validation
      and connect time.
    - Sends the original hostname as the Host header and sets sni_hostname so
      TLS cert verification uses the real hostname, not the IP.
    - follow_redirects=False: any 3xx response returns None to avoid blind
      redirect SSRF.
    - Returns None on any ValueError (fail closed).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    original_host = parsed.hostname or ""
    if not original_host:
        return None
    try:
        safe_ip = resolve_and_validate_host(original_host)
    except ValueError:
        logger.warning("crawler _default_fetch_text SSRF guard blocked: {}", url)
        return None
    # Build the Host header: bracket IPv6, append port when explicit.
    host_for_header = f"[{original_host}]" if ":" in original_host else original_host
    host_header = f"{host_for_header}:{parsed.port}" if parsed.port else host_for_header
    # Pin the connection to the validated IP; keep connecting by hostname when
    # resolve_and_validate_host returned the hostname unchanged (allowlisted).
    connect_host = safe_ip if safe_ip != original_host else original_host
    pinned_url = httpx.URL(url).copy_with(host=connect_host)
    req = httpx.Request("GET", pinned_url, headers={"Host": host_header})
    if connect_host != original_host:
        req.extensions["sni_hostname"] = original_host
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.send(req)
            return resp.text if resp.status_code == 200 else None
    except Exception as exc:
        logger.debug("robots fetch failed {}: {}", url, exc)
        return None


async def fetch_robots(domain_root: str, fetch_text=None) -> set[str]:
    """Fetch and parse robots.txt for the given domain root.

    Returns a set of disallowed path prefixes for the wildcard user-agent.
    The robots URL host is validated via guard_url before fetching; if validation
    fails the fetch is skipped and an empty disallow set is returned (safe default).
    """
    fetch_text = fetch_text or _default_fetch_text
    robots_url = urljoin(domain_root.rstrip("/") + "/", "robots.txt")
    # SSRF guard: validate the robots URL host before fetching.
    if not guard_url(robots_url):
        logger.warning("fetch_robots SSRF guard blocked robots URL: {}", robots_url)
        return set()
    body = await fetch_text(robots_url)
    disallowed: set[str] = set()
    if not body:
        return disallowed
    applies = False
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            applies = value == "*"
        elif key == "disallow" and applies and value:
            disallowed.add(value)
    return disallowed


def is_allowed_by_robots(url: str, disallowed: set[str]) -> bool:
    """Return True if url is not blocked by any disallowed prefix."""
    path = urlparse(url).path or "/"
    return not any(path.startswith(rule) for rule in disallowed)


# Resource types the auditor never needs (it reads DOM text + links, not
# pixels/styling). Aborting them makes "load" fire far sooner on ad/image-heavy
# pages and shrinks the per-subresource SSRF-validation surface — the single
# biggest render-speed win on content sites (news, blogs). Documents, scripts,
# XHR/fetch (needed for SPA hydration) are always allowed through the guard.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})


async def _validate_subresource_host(host: str, cache: dict[str, bool] | None) -> bool:
    """Return True if `host` is SSRF-safe for a subresource fetch.

    resolve_and_validate_host does a blocking socket.getaddrinfo; the route
    handler runs on the crawl's event loop and is called once per allowed
    subresource, so calling it inline serialised every render behind DNS. Two
    fixes here:

    - Resolution runs off the loop via asyncio.to_thread, so concurrent pages
      and the rest of the render proceed while a lookup is in flight.
    - An optional per-crawl cache collapses the many same-host subresources a
      single page fires (a Next.js page pulls dozens of /_next chunks from the
      root host) into one lookup. The cache's lifetime is a single crawl session
      — the same span the root host is already IP-pinned for — so it does not
      widen the DNS-rebinding window the guard exists to close.
    """
    if cache is not None and host in cache:
        return cache[host]
    try:
        await asyncio.to_thread(resolve_and_validate_host, host)
        ok = True
    except ValueError:
        ok = False
    if cache is not None:
        cache[host] = ok
    return ok


def _make_ssrf_route_handler(cache: dict[str, bool] | None = None):
    """Build a Playwright request interceptor: abort heavy resource types the
    auditor doesn't need, then abort any remaining subresource/redirect to a
    non-http(s) or unvalidated/internal host (defense-in-depth vs subresource
    SSRF). Pass a dict to memoise host validation for the caller's lifetime."""

    async def _handler(route, request) -> None:
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        req_url = request.url
        req_parsed = urlparse(req_url)
        req_host = req_parsed.hostname or ""
        if req_parsed.scheme not in {"http", "https"} or not req_host:
            await route.abort()
            return
        if await _validate_subresource_host(req_host, cache):
            await route.continue_()
        else:
            logger.warning("_ssrf_route_handler blocked subresource: {}", req_url)
            await route.abort()

    return _handler


# Backwards-compatible module-level handler (no cache) for standalone renders and
# the unit tests that call it directly. crawl_site builds a cached handler per
# session in _browser_render_session.
_ssrf_route_handler = _make_ssrf_route_handler()


async def _render_page_in_context(context, url: str, route_handler=None) -> tuple[int, str, str]:
    """Render one URL in an already-created context and return
    (status, html, final_url). Opens and closes a fresh page (tab), applies the
    SSRF/asset route guard, waits for hydration best-effort, and snapshots.

    route_handler defaults to the module-level uncached handler; crawl_site
    passes a per-session cached handler so repeated same-host subresource
    validations across the crawl are memoised.
    """
    page = await context.new_page()
    try:
        # Block unneeded resources + SSRF-guard remaining subresources/redirects.
        await page.route("**/*", route_handler or _ssrf_route_handler)

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
    finally:
        await page.close()


@asynccontextmanager
async def _browser_render_session(host: str, safe_ip: str):
    """Launch ONE headless Chromium for a whole same-host crawl and yield a
    per-URL renderer that reuses it.

    Reusing one browser across the crawl (instead of launching/tearing one down
    per page) removes ~1s+ of process overhead per page and prevents the render
    slowdown-under-load that pushed pages past the per-page timeout on large
    sites. The DNS-rebinding pin (--host-resolver-rules) is set once for the
    crawl's single host — safe because crawl_site is same-host only.
    """
    resolver_rule = f"MAP {host} {safe_ip}, EXCLUDE localhost"
    # Hold a slot for the whole crawl, not per page: the browser lives for the
    # crawl's duration, so that is the span that costs 394 MiB. `async with`
    # returns the permit on every exit path — a crawl that raises mid-page must
    # not leak one, or the worker deadlocks after MAX_CONCURRENT_CRAWLS
    # failures and every later audit hangs to job_timeout (1h).
    async with _CRAWL_SLOTS:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    f"--host-resolver-rules={resolver_rule}",
                    # Docker's default /dev/shm is 64 MiB; Chromium renders blank or
                    # crashes on heavy pages once it fills. Back shared memory with
                    # /tmp instead so renders don't fail on memory-constrained hosts.
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = await browser.new_context(user_agent=settings.AUDITOR_USER_AGENT)

                # One host-validation cache for the whole crawl session: the many
                # same-host subresources each page fires are resolved once, not
                # per request. Lifetime == the crawl (same as the root-host pin).
                subresource_host_cache: dict[str, bool] = {}
                session_route_handler = _make_ssrf_route_handler(subresource_host_cache)

                async def _render(url: str) -> tuple[int, str, str]:
                    return await _render_page_in_context(context, url, session_route_handler)

                yield _render
            finally:
                await browser.close()


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


async def _curl_cffi_fetch(url: str, *, max_hops: int = 3) -> tuple[int, str, str]:
    """Fetch a URL over curl_cffi (Chrome TLS/JA3) with SSRF pinning.

    Resolves + validates the host, pins the connection to that IP via the
    session-level ``curl_options={CurlOpt.RESOLVE: [...]}`` (curl_cffi has no
    per-request ``resolve=`` kwarg — the pin must go on the AsyncSession
    constructor, so a fresh session is opened per hop), and re-guards every
    redirect hop (follow_redirects is disabled and handled manually so a
    Location cannot bounce us to an internal host). Returns
    (status, html, final_url); (0, "", url) on any SSRF failure, bad scheme, or
    exceeding max_hops — treated downstream as blocked/error.
    """
    current = url
    for _ in range(max_hops + 1):
        parsed = urlparse(current)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host:
            return 0, "", current
        try:
            safe_ip = resolve_and_validate_host(host)
        except ValueError:
            logger.warning("_curl_cffi_fetch SSRF guard blocked: {}", current)
            return 0, "", current
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # Pin only when the guard returned an IP (not an allowlisted hostname).
        curl_options = {CurlOpt.RESOLVE: [f"{host}:{port}:{safe_ip}"]} if safe_ip != host else None
        try:
            async with AsyncSession(curl_options=curl_options) as session:
                resp = await session.get(
                    current,
                    impersonate=settings.AUDITOR_HTTP_IMPERSONATE,
                    allow_redirects=False,
                    timeout=settings.AUDITOR_HTTP_TIMEOUT_S,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("_curl_cffi_fetch error {}: {}", current, exc)
            return 0, "", current
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return resp.status_code, resp.text or "", current
            current = urljoin(current, location)
            continue
        return resp.status_code, resp.text or "", current
    return 0, "", current


async def _http_first_render(url: str) -> tuple[int, str, str]:
    """HTTP-first renderer: fetch raw HTML over curl_cffi and fall back to a
    headless render only when the HTTP HTML is a thin/SPA shell.

    SSR/SSG sites (the common case, and the ones bot-blocked on the headless
    path) resolve here with no browser launched. A genuine client-rendered page
    yields too little text and falls back to _playwright_render for that one URL.
    """
    # Local import: extraction imports crawler (RawPage), so importing it at
    # module top would be a cycle.
    from app.auditor.extraction import extract_main

    status, html, final_url = await _curl_cffi_fetch(url)
    if 200 <= status < 400 and html:
        _, text = extract_main(html)
        if len(text) >= settings.AUDITOR_HTTP_MIN_CONTENT_CHARS:
            return status, html, final_url
    # Thin, empty, or non-2xx/3xx → headless fallback for this URL.
    return await _playwright_render(url)


async def _playwright_render(url: str) -> tuple[int, str, str]:
    """Default single-page renderer: launch a browser, render one URL, close.

    Used for standalone renders (the legacy ``crawl``/direct ``render_page``
    path). ``crawl_site`` uses ``_browser_render_session`` instead to reuse one
    browser across the whole crawl.

    DNS-rebinding protection: resolves the hostname ONCE before launch and pins
    the browser's resolver to that IP via --host-resolver-rules; the route
    handler validates every remaining subresource/redirect. Returns (0, "", url)
    on a pre-launch SSRF failure so render_page treats it as blocked.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return 0, "", url

    try:
        safe_ip = resolve_and_validate_host(host)
    except ValueError:
        logger.warning("_playwright_render SSRF guard blocked pre-launch: {}", url)
        return 0, "", url

    async with _browser_render_session(host, safe_ip) as render:
        return await render(url)


async def _render_once(url: str, renderer, max_bytes: int) -> RawPage:
    """One render attempt. Returns a RawPage (never raises). SOFT_BLOCKED for 403/429."""
    try:
        result = await asyncio.wait_for(
            renderer(url),
            timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S
            + settings.AUDITOR_HYDRATION_SETTLE_MS / 1000
            + 2,
        )
    except TimeoutError:
        return RawPage(
            url=url,
            status=0,
            html=None,
            bytes_len=0,
            error="TIMEOUT",
            error_kind=RawPageError.TIMEOUT,
        )
    except Exception as exc:
        logger.debug("render_page error {}: {}", url, exc)
        msg = str(exc).lower()
        kind = (
            RawPageError.LAUNCH_FAILED
            if ("executable doesn't exist" in msg or "browsertype.launch" in msg)
            else RawPageError.HTTP_ERROR
        )
        return RawPage(url=url, status=0, html=None, bytes_len=0, error=str(exc), error_kind=kind)

    # Support both 2-tuple (status, html) and 3-tuple (status, html, final_url).
    if len(result) == 3:
        status, html, final_url = result
    else:
        status, html = result
        final_url = url

    # Post-redirect SSRF guard — discard body if the browser landed on an unsafe host.
    if final_url != url and not guard_url(final_url):
        logger.warning("render_page SSRF post-redirect guard blocked: {} -> {}", url, final_url)
        return RawPage(
            url=url,
            status=0,
            html=None,
            bytes_len=0,
            error="UNSAFE_REDIRECT",
            error_kind=RawPageError.BLOCKED,
            final_url=final_url,
        )

    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(
            url=url,
            status=status,
            html=None,
            bytes_len=byte_len,
            error="SIZE_CAP_EXCEEDED",
            error_kind=RawPageError.SIZE_CAP,
            final_url=final_url,
        )

    if 200 <= status < 400:
        error_kind = RawPageError.OK
    elif status in (403, 429):
        error_kind = RawPageError.SOFT_BLOCKED
    else:
        error_kind = RawPageError.HTTP_ERROR
    return RawPage(
        url=url,
        status=status,
        html=html,
        bytes_len=byte_len,
        error_kind=error_kind,
        final_url=final_url,
    )


async def render_page(
    url: str,
    *,
    page_renderer=None,
    max_bytes: int = 2_000_000,
) -> RawPage:
    """Render a single page with SSRF guards and bounded soft-block retries.

    Handles timeout and errors gracefully — returns a RawPage with error set
    rather than raising. Enforces max_bytes content-size cap.

    SSRF guards:
    - Pre-render: guard_url(url) is checked before rendering; unsafe URLs are
      returned immediately as RawPage(error="UNSAFE_URL").
    - Post-redirect: the final URL the renderer landed on is re-checked;
      unsafe post-redirect hosts yield RawPage(error="UNSAFE_REDIRECT") with
      no body returned.

    The injectable page_renderer must return either:
      (status, html)               — final_url defaults to the requested url
      (status, html, final_url)    — explicit final URL after redirects
    Both forms are accepted for backwards compatibility.

    Soft-block retries: when the render returns HTTP 403/429 (bot firewall /
    rate limit), retries up to AUDITOR_SOFT_BLOCK_RETRIES times, sleeping
    AUDITOR_SOFT_BLOCK_BACKOFF_S[i] between attempts. On the crawl_site path the
    renderer is a shared browser context, so a challenge-clearance cookie set on
    one attempt carries into the next (and to later pages); on the standalone
    path each attempt launches a fresh browser. Only 403/429 are retried —
    timeouts and other errors are returned immediately.
    """
    # Pre-render SSRF guard.
    if not guard_url(url):
        logger.warning("render_page SSRF pre-guard blocked: {}", url)
        return RawPage(
            url=url,
            status=0,
            html=None,
            bytes_len=0,
            error="UNSAFE_URL",
            error_kind=RawPageError.BLOCKED,
        )

    renderer = page_renderer or _playwright_render
    retries = settings.AUDITOR_SOFT_BLOCK_RETRIES
    backoff = settings.AUDITOR_SOFT_BLOCK_BACKOFF_S
    attempt = 0
    while True:
        page = await _render_once(url, renderer, max_bytes)
        if page.error_kind != RawPageError.SOFT_BLOCKED or attempt >= retries:
            return page
        if backoff:
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
        attempt += 1


async def crawl(
    urls: list[str],
    *,
    rps: int | None = None,
    page_renderer=None,
    robots_disallowed: set[str] | None = None,
) -> list[RawPage]:
    """Crawl a list of URLs, respecting robots rules and rate limiting.

    Args:
        urls: Ordered list of URLs to render.
        rps: Requests per second cap per domain (defaults to settings.AUDITOR_CRAWL_RPS).
        page_renderer: Injectable async callable ``(url) -> (status, html[, final_url])``
            for testing.
        robots_disallowed: Set of disallowed path prefixes from fetch_robots().

    Returns:
        List of RawPage results in the same order as urls.
    """
    rps = rps if rps is not None else settings.AUDITOR_CRAWL_RPS
    delay = 1.0 / rps if rps > 0 else 0.0
    robots_disallowed = robots_disallowed or set()
    pages: list[RawPage] = []

    for url in urls:
        if not is_allowed_by_robots(url, robots_disallowed):
            logger.debug("robots blocked: {}", url)
            pages.append(
                RawPage(
                    url=url,
                    status=0,
                    html=None,
                    bytes_len=0,
                    error="ROBOTS_BLOCKED",
                    error_kind=RawPageError.ROBOTS,
                )
            )
            continue
        page = await render_page(url, page_renderer=page_renderer)
        pages.append(page)
        if delay:
            await asyncio.sleep(delay)

    return pages


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
    root_hostname = ""
    for raw in seeds:
        n = discovery.normalize_url(raw)
        if not n or n in seen:
            continue
        host = urlparse(n).netloc
        if not root_host:
            root_host = host  # first valid seed defines the same-host boundary
            root_hostname = urlparse(n).hostname or ""  # host w/o port, for the resolver pin
        elif host != root_host:
            continue  # off-host seed rejected
        if discovery.is_structurally_prunable(n):
            seen.add(n)
            continue
        if not discovery.guard_url(n):
            seen.add(n)
            continue
        seen.add(n)
        frontier.append((n, 0))

    async def _run(renderer) -> list[RawPage]:
        pages: list[RawPage] = []
        while frontier and len(pages) < max_pages:
            url, depth = frontier.popleft()

            if not is_allowed_by_robots(url, robots_disallowed):
                pages.append(
                    RawPage(
                        url=url,
                        status=0,
                        html=None,
                        bytes_len=0,
                        error="ROBOTS_BLOCKED",
                        error_kind=RawPageError.ROBOTS,
                    )
                )
                continue

            page = await render_page(url, page_renderer=renderer)
            pages.append(page)
            if delay:
                await asyncio.sleep(delay)

            if not page.html or depth + 1 > _MAX_DEPTH:
                continue

            new_links: list[str] = []
            for link in discovery._extract_links(page.html, url):
                link = discovery.canonicalize_to_root(link, root_host)
                if link in seen:
                    continue
                if urlparse(link).netloc != root_host:
                    seen.add(link)
                    continue
                if discovery.is_structurally_prunable(link):
                    seen.add(link)
                    continue
                if not discovery.guard_url(link):
                    seen.add(link)
                    continue
                seen.add(link)
                new_links.append(link)

            # Enqueue the pages that decide the score first. The budget is small
            # (10 pages for a public audit) and DOM order is the site's nav
            # order, not ours: on stage.luranai.com the nav lists /platform,
            # /auditor and five /solutions/* pages before the footer's /faq, so
            # a FIFO frontier spent the whole budget before reaching it — and
            # the report then called /faq, /contact and /security missing.
            #
            # Ranking the sitemap seeds did not cover this: that site's sitemap
            # lists 4 URLs and hardcodes the luranai.com host, so auditing
            # stage.luranai.com filters them all out as off-host and the crawl
            # frontier chooses every page.
            #
            # This only reorders; it never admits a link. Same-host, prune, SSRF
            # and robots all still apply. Stable sort keeps DOM order within a
            # rank.
            new_links.sort(key=discovery.signal_rank)
            for link in new_links:
                frontier.append((link, depth + 1))

        return pages

    # Injected renderer (tests) or empty frontier: no shared browser needed.
    if page_renderer is not None or not root_hostname:
        return await _run(page_renderer)

    # Production: launch ONE browser for the whole same-host crawl and reuse it
    # across every page (avoids a browser launch per page).
    try:
        safe_ip = resolve_and_validate_host(root_hostname)
    except ValueError:
        logger.warning("crawl_site SSRF guard blocked root host: {}", root_hostname)
        return []
    async with _browser_render_session(root_hostname, safe_ip) as renderer:
        return await _run(renderer)
