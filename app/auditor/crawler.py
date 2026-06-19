"""Playwright render + robots.txt + 1rps politeness + per-page timeout/size caps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from app.core.config import settings


@dataclass
class RawPage:
    url: str
    status: int
    html: str | None
    bytes_len: int
    error: str | None = None


async def _default_fetch_text(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            return resp.text if resp.status_code == 200 else None
    except Exception as exc:
        logger.debug("robots fetch failed {}: {}", url, exc)
        return None


async def fetch_robots(domain_root: str, fetch_text=None) -> set[str]:
    """Fetch and parse robots.txt for the given domain root.

    Returns a set of disallowed path prefixes for the wildcard user-agent.
    """
    fetch_text = fetch_text or _default_fetch_text
    robots_url = urljoin(domain_root.rstrip("/") + "/", "robots.txt")
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


async def _playwright_render(url: str) -> tuple[int, str]:
    """Default page renderer using Playwright headless Chromium."""
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
            return (resp.status if resp else 0), html
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
    """
    renderer = page_renderer or _playwright_render
    try:
        status, html = await asyncio.wait_for(
            renderer(url),
            timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S + 2,
        )
    except asyncio.TimeoutError:
        return RawPage(url=url, status=0, html=None, bytes_len=0, error="TIMEOUT")
    except Exception as exc:
        logger.debug("render_page error {}: {}", url, exc)
        return RawPage(url=url, status=0, html=None, bytes_len=0, error=str(exc))

    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(
            url=url,
            status=status,
            html=None,
            bytes_len=byte_len,
            error="SIZE_CAP_EXCEEDED",
        )
    return RawPage(url=url, status=status, html=html, bytes_len=byte_len)


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
        page_renderer: Injectable async callable ``(url) -> (status, html)`` for testing.
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
