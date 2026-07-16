"""URL discovery: sitemap-first seeding, structural prune, SSRF guard."""

from __future__ import annotations

import re
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

# Paths worth spending a limited page budget on.
#
# The budget is small (10 pages for a public audit) and the sitemap is not
# ordered for us. A real audit of stage.luranai.com spent all 10 pages on the
# homepage, /pricing, /platform, /auditor and five sibling /solutions/* pages,
# and never fetched /faq, /contact or /security — which the site has. The report
# then listed all three as gaps, because the scorer reads "no faq kind found" as
# "no FAQ exists".
#
# These patterns map onto the score's dimensions (knowledge / support /
# conversion), so a page matching one carries far more signal than the Nth
# member of a product family. This ranks candidates; it never adds any. The
# structural prune and the SSRF guard still decide what is eligible at all.
_HIGH_SIGNAL_PATH_RE = re.compile(
    r"/(faq|faqs|help|support|contact|pricing|plans?|security|privacy|terms|legal"
    r"|policy|policies|about|docs?|documentation|guides?|tutorials?|getting-started)\b",
    re.IGNORECASE,
)


def signal_rank(url: str) -> tuple[int, int]:
    """Sort key for when the page budget is smaller than the site.

    Returns (keyword_rank, depth): lower is fetched first.

    Two signals, deliberately in this order:

    1. keyword_rank — 0 if the path names something the score measures. This is
       a convention heuristic and an English one; a site using /tarifs or /aide
       matches nothing and scores 1 across the board.
    2. depth — path segments. Language-independent, so it is what carries a site
       whose vocabulary we cannot read: a top-level page outranks one buried in
       a product family on every site, in every language. It is also what stops
       one large family eating a small budget, which is the failure this exists
       to prevent (/solutions/* is depth 2; /faq, /pricing, /contact are 1).

    Used for both sitemap seeds and links discovered mid-crawl — the budget can
    run out on either path. Python's sort is stable, so equal-ranked URLs keep
    their original order (sitemap order, or DOM order for discovered links).

    This only ranks candidates. A site matching nothing keeps its original
    order, so no site is ever made worse.
    """
    path = urlparse(url).path
    keyword = 0 if _HIGH_SIGNAL_PATH_RE.search(path) else 1
    depth = len([seg for seg in path.split("/") if seg])
    return (keyword, depth)


def _seed_rank(url: str, root: str) -> tuple[int, int, int]:
    """Seed ordering: the root first (the scan-quality diagnosis reads it), then
    by signal_rank."""
    if url == root:
        return (0, 0, 0)
    keyword, depth = signal_rank(url)
    return (1, keyword, depth)


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
        (k, v)
        for k, v in parse_qsl(parsed.query)
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            urlencode(query_pairs),
            "",
        )
    )


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
        logger.warning("discovery _default_fetch_text SSRF guard blocked: {}", url)
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
        # Collect every eligible URL before truncating. Stopping at max_pages
        # here is what lost /faq to a product family: the cap fell where the
        # sitemap happened to list things, so the pages that decide the score
        # were never even candidates.
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if n and _accept(n):
                seen.add(n)
                found.append(n)

    # Rank, then cut. Stable sort keeps sitemap order within each rank.
    found.sort(key=lambda u: _seed_rank(u, root))
    return found[:max_pages]
