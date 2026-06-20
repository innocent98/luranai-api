from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import crawler


@pytest.mark.asyncio
async def test_ssrf_route_handler_aborts_non_http_and_internal_continues_safe():
    # non-http scheme -> abort, never validated
    route = AsyncMock()
    await crawler._ssrf_route_handler(route, SimpleNamespace(url="data:text/html,hi"))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()

    # internal host (guard raises) -> abort
    route2 = AsyncMock()
    with patch("app.auditor.crawler.resolve_and_validate_host", side_effect=ValueError("private")):
        await crawler._ssrf_route_handler(route2, SimpleNamespace(url="http://10.0.0.1/x.js"))
    route2.abort.assert_awaited_once()
    route2.continue_.assert_not_awaited()

    # safe public host -> continue
    route3 = AsyncMock()
    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="93.184.216.34"):
        await crawler._ssrf_route_handler(route3, SimpleNamespace(url="https://example.com/s.css"))
    route3.continue_.assert_awaited_once()
    route3.abort.assert_not_awaited()


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
    from unittest.mock import patch

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

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await crawler._default_fetch_text("https://example.com/robots.txt")
    assert result is None


@pytest.mark.asyncio
async def test_crawler_default_fetch_text_returns_text_on_200():
    """_default_fetch_text returns body text on a normal 200 response."""
    import httpx

    async def mock_send(self_or_request, *args, **kwargs):
        return httpx.Response(200, text="User-agent: *\nDisallow: /admin\n")

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await crawler._default_fetch_text("https://example.com/robots.txt")
    assert result is not None
    assert "Disallow" in result


# ---------------------------------------------------------------------------
# httpx PIN tests — verify resolve-and-pin contract in crawler._default_fetch_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawler_fetch_text_connects_to_pinned_ip():
    """_default_fetch_text must connect to the pinned IP, not re-resolve by hostname."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await crawler._default_fetch_text("https://example.com/robots.txt")

    assert len(captured) == 1
    req = captured[0]
    assert req.url.host == "1.2.3.4", f"Expected pinned IP, got: {req.url.host}"


@pytest.mark.asyncio
async def test_crawler_fetch_text_sends_original_host_header():
    """_default_fetch_text must set the Host header to the original hostname."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await crawler._default_fetch_text("https://example.com/robots.txt")

    assert len(captured) == 1
    req = captured[0]
    assert req.headers.get("host") == "example.com", (
        f"Expected Host: example.com, got: {req.headers.get('host')}"
    )


@pytest.mark.asyncio
async def test_crawler_fetch_text_sets_sni_hostname_extension():
    """_default_fetch_text must set sni_hostname when connecting to an IP."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await crawler._default_fetch_text("https://example.com/robots.txt")

    assert len(captured) == 1
    req = captured[0]
    assert req.extensions.get("sni_hostname") == "example.com", (
        f"Expected sni_hostname='example.com', got: {req.extensions.get('sni_hostname')}"
    )


@pytest.mark.asyncio
async def test_crawler_fetch_text_no_send_on_internal_host():
    """_default_fetch_text must NOT call send when resolve raises (fail closed)."""
    import httpx

    send_called = []

    async def should_not_be_called(self, request, *args, **kwargs):
        send_called.append(request)
        return httpx.Response(200, text="leaked")

    with patch(
        "app.auditor.crawler.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        with patch("httpx.AsyncClient.send", new=should_not_be_called):
            result = await crawler._default_fetch_text("http://10.0.0.1/robots.txt")

    assert result is None
    assert send_called == [], "send must not be called for an internal host"


@pytest.mark.asyncio
async def test_crawler_fetch_text_rejects_non_http_scheme():
    """_default_fetch_text must return None for non-http/https schemes without resolving."""
    resolve_called = []

    def should_not_resolve(hostname, *args, **kwargs):
        resolve_called.append(hostname)
        return "1.2.3.4"

    with patch("app.auditor.crawler.resolve_and_validate_host", side_effect=should_not_resolve):
        result_ftp = await crawler._default_fetch_text("ftp://example.com/robots.txt")
        result_file = await crawler._default_fetch_text("file:///etc/passwd")

    assert result_ftp is None
    assert result_file is None
    assert resolve_called == [], "resolve must not be called for non-http/https schemes"


# ---------------------------------------------------------------------------
# Playwright PIN tests — host-resolver-rules + route interception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playwright_render_launches_with_host_resolver_rules():
    """_playwright_render must pass --host-resolver-rules=MAP <host> <safe_ip> to Chromium."""
    from unittest.mock import AsyncMock, MagicMock

    launch_args: list[list[str]] = []

    def make_mock_playwright():
        mock_resp = MagicMock()
        mock_resp.status = 200

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_resp)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com/"
        mock_page.route = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.new_page = AsyncMock(return_value=mock_page)
        mock_browser.close = AsyncMock()

        mock_chromium = AsyncMock()

        async def fake_launch(headless, args):
            launch_args.append(args)
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
            await crawler._playwright_render("https://example.com/")

    assert len(launch_args) == 1
    args_list = launch_args[0]
    matching = [a for a in args_list if a.startswith("--host-resolver-rules=")]
    assert matching, f"--host-resolver-rules not found in launch args: {args_list}"
    assert "MAP example.com 1.2.3.4" in matching[0], (
        f"Expected MAP rule for example.com -> 1.2.3.4, got: {matching[0]}"
    )


@pytest.mark.asyncio
async def test_playwright_render_blocks_unsafe_host_before_launch():
    """_playwright_render must return failure tuple without launching browser on unsafe host."""
    from unittest.mock import AsyncMock, MagicMock

    launched = []

    def make_mock_playwright():
        mock_chromium = AsyncMock()

        async def fake_launch(**kwargs):
            launched.append(True)
            return AsyncMock()

        mock_chromium.launch = fake_launch
        mock_p = MagicMock()
        mock_p.chromium = mock_chromium

        class FakeCtx:
            async def __aenter__(self):
                return mock_p

            async def __aexit__(self, *_):
                pass

        return FakeCtx()

    with patch(
        "app.auditor.crawler.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        with patch("app.auditor.crawler.async_playwright", side_effect=make_mock_playwright):
            status, html, final_url = await crawler._playwright_render("http://10.0.0.1/")

    assert status == 0
    assert html == ""
    assert final_url == "http://10.0.0.1/"
    assert launched == [], "Browser must NOT be launched for an unsafe host"


@pytest.mark.asyncio
async def test_playwright_route_handler_aborts_internal_host():
    """Route handler must call route.abort() for subresource requests to internal hosts."""
    from unittest.mock import AsyncMock, MagicMock

    # Extract and directly test the route handler logic by capturing it.
    captured_handler = []

    def make_mock_playwright():
        mock_resp = MagicMock()
        mock_resp.status = 200

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_resp)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com/"

        async def capture_route(pattern, handler):
            captured_handler.append(handler)

        mock_page.route = capture_route

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

    def selective_resolve(hostname):
        if hostname in {"example.com"}:
            return "1.2.3.4"
        raise ValueError(f"internal: {hostname}")

    with patch("app.auditor.crawler.resolve_and_validate_host", side_effect=selective_resolve):
        with patch("app.auditor.crawler.async_playwright", side_effect=make_mock_playwright):
            await crawler._playwright_render("https://example.com/")

        # Handler must be invoked while the patch is still active.
        assert len(captured_handler) == 1, "Route handler must be registered"
        handler = captured_handler[0]

        mock_route_internal = AsyncMock()
        mock_request_internal = MagicMock()
        mock_request_internal.url = "http://10.0.0.1/api/secret"
        await handler(mock_route_internal, mock_request_internal)

    mock_route_internal.abort.assert_called_once()
    mock_route_internal.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_playwright_route_handler_continues_safe_host():
    """Route handler must call route.continue_() for subresource requests to safe hosts."""
    from unittest.mock import AsyncMock, MagicMock

    captured_handler = []

    def make_mock_playwright():
        mock_resp = MagicMock()
        mock_resp.status = 200

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_resp)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com/"

        async def capture_route(pattern, handler):
            captured_handler.append(handler)

        mock_page.route = capture_route

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
            await crawler._playwright_render("https://example.com/")

        # Handler must be invoked while the patch is still active so
        # resolve_and_validate_host returns the safe IP instead of doing a
        # real DNS lookup.
        assert len(captured_handler) == 1
        handler = captured_handler[0]

        mock_route_safe = AsyncMock()
        mock_request_safe = MagicMock()
        mock_request_safe.url = "https://cdn.example.com/style.css"
        await handler(mock_route_safe, mock_request_safe)

    mock_route_safe.continue_.assert_called_once()
    mock_route_safe.abort.assert_not_called()
