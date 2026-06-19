"""URL discovery: sitemap-first, BFS depth-3 fallback, structural prune, SSRF guard."""

from __future__ import annotations

import re
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import httpx
from loguru import logger

from app.utils.network_security import resolve_and_validate_host

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref")
_PRUNE_PATH_RE = re.compile(
    r"/(login|logout|signin|sign-in|register|signup|sign-up|cart|checkout|admin|account|wishlist)\b",
    re.IGNORECASE,
)
_ASSET_RE = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|pdf|zip|mp4|webp)(\?|$)", re.I)
_MAX_DEPTH = 3


def normalize_url(raw: str, base: str | None = None) -> str | None:
    """Lowercase host, strip fragment, drop tracking query params.

    Returns None for non-http(s) schemes or URLs without a netloc.
    """
    if base:
        raw = urljoin(base, raw)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    return urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        "",
        urlencode(query_pairs),
        "",
    ))


def is_structurally_prunable(url: str) -> bool:
    """Return True if the URL should be excluded based on structural path patterns.

    Drops: login/logout/signin/signup/register/cart/checkout/admin/account/wishlist
    paths and static asset extensions. Content relevance is decided downstream.
    """
    parsed = urlparse(url)
    if _ASSET_RE.search(parsed.path):
        return True
    if _PRUNE_PATH_RE.search(parsed.path):
        return True
    return False


def guard_url(url: str) -> bool:
    """Return True if resolve_and_validate_host passes for the URL's hostname.

    Returns False (never raises) for any URL that fails SSRF validation — these
    must be excluded from the crawl frontier.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    try:
        resolve_and_validate_host(parsed.hostname)
        return True
    except ValueError:
        logger.warning("Auditor SSRF guard blocked URL: {}", url)
        return False


async def _default_fetch_text(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.debug("discovery fetch failed for {}: {}", url, exc)
    return None


def _parse_sitemap(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, re.IGNORECASE)


def _extract_links(html: str, base: str) -> list[str]:
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    out = []
    for href in hrefs:
        n = normalize_url(href, base=base)
        if n:
            out.append(n)
    return out


async def discover(root_url: str, max_pages: int, fetch_text=None) -> list[str]:
    """Return up to max_pages discovered, pruned, SSRF-guarded URLs rooted at root_url.

    Algorithm:
      1. Normalize and SSRF-guard the root URL; return [] if it fails.
      2. Fetch sitemap.xml; parse all <loc> URLs.
      3. BFS depth-3 link-following to fill remaining budget.
      4. All candidate URLs are structurally pruned and SSRF-guarded before inclusion.

    Args:
        root_url: The root URL to start discovery from.
        max_pages: Maximum number of URLs to return (caller supplies plan cap).
        fetch_text: Optional async callable(url) -> str|None for testing injection.
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

    # Phase 1: sitemap.xml
    sitemap_url = urljoin(root + "/", "sitemap.xml")
    sitemap = await fetch_text(sitemap_url)
    if sitemap:
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if n and _accept(n):
                seen.add(n)
                found.append(n)
                if len(found) >= max_pages:
                    return found[:max_pages]

    # Phase 2: BFS depth-3 fallback to fill remaining budget
    queue: deque[tuple[str, int]] = deque((u, 0) for u in list(found))
    while queue and len(found) < max_pages:
        url, depth = queue.popleft()
        if depth >= _MAX_DEPTH:
            continue
        html = await fetch_text(url)
        if not html:
            continue
        for link in _extract_links(html, url):
            if len(found) >= max_pages:
                break
            if _accept(link):
                seen.add(link)
                found.append(link)
                queue.append((link, depth + 1))

    return found[:max_pages]
