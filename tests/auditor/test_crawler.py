from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import crawler


@pytest.mark.asyncio
async def test_ssrf_route_handler_aborts_non_http_and_internal_continues_safe():
    # non-http scheme -> abort, never validated
    route = AsyncMock()
    await crawler._ssrf_route_handler(
        route, SimpleNamespace(url="data:text/html,hi", resource_type="document")
    )
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()

    # internal host (guard raises) -> abort
    route2 = AsyncMock()
    with patch("app.auditor.crawler.resolve_and_validate_host", side_effect=ValueError("private")):
        await crawler._ssrf_route_handler(
            route2, SimpleNamespace(url="http://10.0.0.1/x", resource_type="document")
        )
    route2.abort.assert_awaited_once()
    route2.continue_.assert_not_awaited()

    # safe public host -> continue
    route3 = AsyncMock()
    with patch("app.auditor.crawler.resolve_and_validate_host", return_value="93.184.216.34"):
        await crawler._ssrf_route_handler(
            route3, SimpleNamespace(url="https://example.com/page", resource_type="document")
        )
    route3.continue_.assert_awaited_once()
    route3.abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_ssrf_route_handler_aborts_heavy_resource_types():
    """image/media/font/stylesheet are aborted before any DNS validation (perf +
    reduced SSRF surface); allowed types still get validated."""
    for rtype in ("image", "media", "font", "stylesheet"):
        route = AsyncMock()
        resolved = []
        with patch(
            "app.auditor.crawler.resolve_and_validate_host",
            side_effect=lambda h: resolved.append(h) or "1.2.3.4",
        ):
            await crawler._ssrf_route_handler(
                route, SimpleNamespace(url="https://cdn.example.com/a", resource_type=rtype)
            )
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
        assert resolved == [], f"{rtype} must be aborted WITHOUT a DNS lookup"


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
        type("S", (), {"AUDITOR_PER_PAGE_TIMEOUT_S": 0,
                       "AUDITOR_CRAWL_RPS": 1,
                       "AUDITOR_HYDRATION_SETTLE_MS": 0,
                       "AUDITOR_SOFT_BLOCK_RETRIES": 0,
                       "AUDITOR_SOFT_BLOCK_BACKOFF_S": []})(),
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
                       "AUDITOR_HYDRATION_SETTLE_MS": 0,
                       "AUDITOR_SOFT_BLOCK_RETRIES": 0,
                       "AUDITOR_SOFT_BLOCK_BACKOFF_S": []})(),
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


@pytest.mark.asyncio
async def test_crawl_site_rejects_off_host_seed(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    rendered = []

    async def renderer(url):
        rendered.append(url)
        return 200, "<html>ok</html>"

    pages = await crawler.crawl_site(
        ["https://x.com/", "https://evil.com/x"],  # second seed is off-host
        max_pages=50, rps=1000, page_renderer=renderer,
    )
    assert "https://x.com/" in rendered
    assert not any("evil.com" in u for u in rendered)


@pytest.mark.asyncio
async def test_crawl_site_no_html_page_skips_expansion_without_crash(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.discovery.guard_url", lambda url: True)

    async def renderer(url):
        if url.endswith("/boom"):
            raise ConnectionError("network down")  # -> render_page returns html=None, HTTP_ERROR
        return 200, '<html><a href="/boom">B</a><a href="/ok">O</a></html>'

    pages = await crawler.crawl_site(
        ["https://x.com/"], max_pages=50, rps=1000, page_renderer=renderer,
    )
    boom = next(p for p in pages if p.url.endswith("/boom"))
    assert boom.html is None                     # errored render, no html
    assert boom.error_kind == crawler.RawPageError.HTTP_ERROR
    # crawl did not crash extracting links from the no-html page
    assert any(p.url.endswith("/ok") for p in pages)


def _fast_soft_block_settings():
    """Stub settings: retries=2, zero backoff (fast), generous timeout."""
    return type("S", (), {
        "AUDITOR_PER_PAGE_TIMEOUT_S": 30,
        "AUDITOR_HYDRATION_SETTLE_MS": 0,
        "AUDITOR_CRAWL_RPS": 1,
        "AUDITOR_SOFT_BLOCK_RETRIES": 2,
        "AUDITOR_SOFT_BLOCK_BACKOFF_S": [0, 0],
    })()


@pytest.mark.asyncio
async def test_render_page_retries_soft_block_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        if calls["n"] < 3:
            return 403, "<html>challenge</html>"
        return 200, "<html>real</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 3
    assert page.status == 200
    assert page.error_kind == crawler.RawPageError.OK


@pytest.mark.asyncio
async def test_render_page_soft_block_exhausts_retries(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return 403, "<html>challenge</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 3  # 1 initial + 2 retries
    assert page.status == 403
    assert page.error_kind == crawler.RawPageError.SOFT_BLOCKED


@pytest.mark.asyncio
async def test_render_page_429_treated_as_soft_block(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return (429, "<html>rl</html>") if calls["n"] == 1 else (200, "<html>ok</html>")

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert page.status == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_render_page_200_not_retried(monkeypatch):
    monkeypatch.setattr("app.auditor.crawler.guard_url", lambda url: True)
    monkeypatch.setattr("app.auditor.crawler.settings", _fast_soft_block_settings())
    calls = {"n": 0}

    async def renderer(url):
        calls["n"] += 1
        return 200, "<html>ok</html>"

    page = await crawler.render_page("https://x.com/a", page_renderer=renderer)
    assert calls["n"] == 1
    assert page.error_kind == crawler.RawPageError.OK
