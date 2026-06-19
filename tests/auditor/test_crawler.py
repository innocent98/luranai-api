from unittest.mock import patch

import pytest

from app.auditor import crawler


def test_robots_blocks_disallowed_path():
    disallowed = {"/private", "/admin"}
    assert not crawler.is_allowed_by_robots("https://x.com/private/page", disallowed)
    assert crawler.is_allowed_by_robots("https://x.com/about", disallowed)


@pytest.mark.asyncio
async def test_fetch_robots_parses_disallow(monkeypatch):
    async def fake_fetch(url):
        return "User-agent: *\nDisallow: /secret\nDisallow: /tmp\nAllow: /\n"
    out = await crawler.fetch_robots("https://x.com", fetch_text=fake_fetch)
    assert "/secret" in out and "/tmp" in out


@pytest.mark.asyncio
async def test_render_page_enforces_size_cap(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def big_renderer(url):
        return 200, "x" * 50

    page = await crawler.render_page("https://x.com/a", page_renderer=big_renderer, max_bytes=10)
    assert page.error == "SIZE_CAP_EXCEEDED"
    assert page.html is None


@pytest.mark.asyncio
async def test_crawl_renders_each_url(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def renderer(url):
        return 200, f"<html>{url}</html>"

    pages = await crawler.crawl(
        ["https://x.com/a", "https://x.com/b"], rps=1000, page_renderer=renderer
    )
    assert len(pages) == 2
    assert pages[0].status == 200 and pages[0].html.startswith("<html>")


@pytest.mark.asyncio
async def test_crawl_skips_robots_blocked_url(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def renderer(url):
        return 200, f"<html>{url}</html>"

    pages = await crawler.crawl(
        ["https://x.com/private/secret", "https://x.com/about"],
        rps=1000,
        page_renderer=renderer,
        robots_disallowed={"/private"},
    )
    assert len(pages) == 2
    blocked = next(p for p in pages if "private" in p.url)
    allowed = next(p for p in pages if "about" in p.url)
    assert blocked.error == "ROBOTS_BLOCKED"
    assert blocked.html is None
    assert allowed.html is not None
    assert allowed.error is None


@pytest.mark.asyncio
async def test_render_page_timeout_handled_gracefully(monkeypatch):
    """Timeout test: patches AUDITOR_PER_PAGE_TIMEOUT_S=0 so asyncio.wait_for fires
    almost immediately even with a 100s-sleeping renderer. Must be fast and prove
    the TIMEOUT branch fires — no vacuous assertion."""
    import asyncio

    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr(
        "app.auditor.crawler.settings",
        type("S", (), {"AUDITOR_PER_PAGE_TIMEOUT_S": 0, "AUDITOR_CRAWL_RPS": 1})(),
    )

    async def slow_renderer(url):
        await asyncio.sleep(100)
        return 200, "<html></html>"

    page = await crawler.render_page(
        "https://x.com/slow",
        page_renderer=slow_renderer,
        max_bytes=2_000_000,
    )
    assert page.error == "TIMEOUT"


@pytest.mark.asyncio
async def test_render_page_error_handled_gracefully(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)

    async def broken_renderer(url):
        raise ConnectionError("Network unreachable")

    page = await crawler.render_page(
        "https://x.com/broken",
        page_renderer=broken_renderer,
    )
    assert page.error is not None
    assert page.html is None
    assert page.status == 0


# ---------------------------------------------------------------------------
# SSRF guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_page_unsafe_url_is_blocked(monkeypatch):
    """Pre-render guard: a URL that fails guard_url must return UNSAFE_URL immediately."""
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: False)

    called = []

    async def renderer(url):
        called.append(url)
        return 200, "<html></html>"

    page = await crawler.render_page(
        "http://169.254.169.254/latest/meta-data/",
        page_renderer=renderer,
    )
    assert page.error == "UNSAFE_URL"
    assert page.html is None
    assert page.status == 0
    # Renderer must NOT have been called.
    assert called == []


@pytest.mark.asyncio
async def test_render_page_unsafe_redirect_discards_body(monkeypatch):
    """Post-redirect guard: if the renderer lands on an internal host after a redirect,
    the body must be discarded and error must be UNSAFE_REDIRECT."""
    internal_url = "http://10.0.0.1/"

    def _guard(url: str) -> bool:
        # Allow the initial public URL, block the internal redirect destination.
        return "example.com" in url

    monkeypatch.setattr("app.auditor.crawler.guard_url", _guard)

    async def redirecting_renderer(url):
        # Simulates Playwright following a redirect to an internal host.
        return 200, "<html>secret data</html>", internal_url

    page = await crawler.render_page(
        "https://example.com/page",
        page_renderer=redirecting_renderer,
    )
    assert page.error == "UNSAFE_REDIRECT"
    assert page.html is None
    assert page.status == 0
    assert page.final_url == internal_url


# ---------------------------------------------------------------------------
# Fix 1 + Fix 2 SSRF guard tests (robots fetch + _default_fetch_text)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_robots_internal_host_returns_empty_disallow(monkeypatch):
    """fetch_robots with an internal host is blocked; returns empty disallow set, no exception."""
    # guard_url returns False for internal hosts
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: False)

    called = []

    async def should_not_be_called(url):
        called.append(url)
        return "User-agent: *\nDisallow: /secret\n"

    result = await crawler.fetch_robots("http://10.0.0.1", fetch_text=should_not_be_called)
    assert result == set()
    assert called == [], "fetch_text must NOT be called for an internal host"


@pytest.mark.asyncio
async def test_crawler_default_fetch_text_blocks_internal_host(monkeypatch):
    """_default_fetch_text returns None for an internal/private host (no request made)."""
    from unittest.mock import patch, AsyncMock

    with patch(
        "app.auditor.crawler.resolve_and_validate_host" if hasattr(crawler, "resolve_and_validate_host") else
        "app.utils.network_security.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        # Patch at the module level where _default_fetch_text imports it
        with patch("app.utils.network_security.resolve_and_validate_host", side_effect=ValueError("private IP")):
            result = await crawler._default_fetch_text("http://169.254.169.254/latest/meta-data/")
    assert result is None


@pytest.mark.asyncio
async def test_crawler_default_fetch_text_returns_none_on_3xx():
    """_default_fetch_text with follow_redirects=False returns None on a 3xx response."""
    import httpx

    async def mock_send(self_or_request, *args, **kwargs):
        return httpx.Response(302, headers={"Location": "http://internal.example.com/"})

    with patch("app.utils.network_security.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await crawler._default_fetch_text("https://example.com/robots.txt")
    assert result is None


@pytest.mark.asyncio
async def test_crawler_default_fetch_text_returns_text_on_200():
    """_default_fetch_text returns body text on a normal 200 response."""
    import httpx

    async def mock_send(self_or_request, *args, **kwargs):
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin\n")

    with patch("app.utils.network_security.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await crawler._default_fetch_text("https://example.com/robots.txt")
    assert result is not None
    assert "Disallow" in result
