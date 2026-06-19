"""Playwright render + robots.txt + 1rps politeness + per-page timeout/size caps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from app.core.config import settings
from app.auditor.discovery import guard_url


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


async def _default_fetch_text(url: str) -> str | None:
    """Fetch URL text with SSRF guard and no redirect-following.

    Security notes:
    - Guards the initial host via resolve_and_validate_host before any request.
    - follow_redirects=False: any 3xx response returns None (robots/sitemap are
      best-effort; not following redirects is safe and avoids blind redirect SSRF).
    """
    from urllib.parse import urlparse as _urlparse
    from app.utils.network_security import resolve_and_validate_host as _resolve
    hostname = _urlparse(url).hostname
    if not hostname:
        return None
    try:
        _resolve(hostname)
    except ValueError:
        logger.warning("crawler _default_fetch_text SSRF guard blocked: {}", url)
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.get(url)
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


async def _playwright_render(url: str) -> tuple[int, str, str]:
    """Default page renderer using Playwright headless Chromium.

    Returns (status, html, final_url) where final_url is the URL the browser
    landed on after any redirects.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            resp = await page.goto(
                url,
                timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S * 1000,
                wait_until="domcontentloaded",
            )
            html = await page.content()
            final_url = page.url or url
            return (resp.status if resp else 0), html, final_url
        finally:
            await browser.close()


async def render_page(
    url: str,
    *,
    page_renderer=None,
    max_bytes: int = 2_000_000,
) -> RawPage:
    """Render a single page and return a RawPage.

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
    """
    # Pre-render SSRF guard.
    if not guard_url(url):
        logger.warning("render_page SSRF pre-guard blocked: {}", url)
        return RawPage(url=url, status=0, html=None, bytes_len=0, error="UNSAFE_URL")

    renderer = page_renderer or _playwright_render
    try:
        result = await asyncio.wait_for(
            renderer(url),
            timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S + 2,
        )
    except asyncio.TimeoutError:
        return RawPage(url=url, status=0, html=None, bytes_len=0, error="TIMEOUT")
    except Exception as exc:
        logger.debug("render_page error {}: {}", url, exc)
        return RawPage(url=url, status=0, html=None, bytes_len=0, error=str(exc))

    # Support both 2-tuple (status, html) and 3-tuple (status, html, final_url).
    if len(result) == 3:
        status, html, final_url = result
    else:
        status, html = result
        final_url = url

    # Post-redirect SSRF guard — discard body if the browser landed on an unsafe host.
    if final_url != url and not guard_url(final_url):
        logger.warning("render_page SSRF post-redirect guard blocked: {} -> {}", url, final_url)
        return RawPage(url=url, status=0, html=None, bytes_len=0, error="UNSAFE_REDIRECT", final_url=final_url)

    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(
            url=url,
            status=status,
            html=None,
            bytes_len=byte_len,
            error="SIZE_CAP_EXCEEDED",
            final_url=final_url,
        )
    return RawPage(url=url, status=status, html=html, bytes_len=byte_len, final_url=final_url)


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
                RawPage(url=url, status=0, html=None, bytes_len=0, error="ROBOTS_BLOCKED")
            )
            continue
        page = await render_page(url, page_renderer=page_renderer)
        pages.append(page)
        if delay:
            await asyncio.sleep(delay)

    return pages
