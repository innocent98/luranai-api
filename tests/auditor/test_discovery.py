"""Tests for app/auditor/discovery.py — URL discovery + SSRF guard."""

from unittest.mock import patch

import pytest

from app.auditor import discovery


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------


def test_normalize_strips_fragment_and_tracking():
    out = discovery.normalize_url("https://Example.com/a?utm_source=x&id=5#frag")
    assert out == "https://example.com/a?id=5"


def test_normalize_rejects_non_http():
    assert discovery.normalize_url("ftp://example.com/x") is None
    assert discovery.normalize_url("mailto:a@b.com") is None


def test_normalize_lowercases_host():
    out = discovery.normalize_url("HTTPS://EXAMPLE.COM/Path")
    assert out == "https://example.com/Path"


def test_normalize_strips_fragment_only():
    out = discovery.normalize_url("https://example.com/page#section")
    assert out == "https://example.com/page"


def test_normalize_with_base_resolves_relative():
    out = discovery.normalize_url("/about", base="https://example.com/home")
    assert out == "https://example.com/about"


def test_normalize_strips_multiple_tracking_params():
    out = discovery.normalize_url(
        "https://example.com/?utm_source=fb&utm_campaign=x&page=2&fbclid=abc"
    )
    assert out == "https://example.com/?page=2"


def test_normalize_preserves_non_tracking_params():
    out = discovery.normalize_url("https://example.com/search?q=ai&sort=asc")
    assert out == "https://example.com/search?q=ai&sort=asc"


# ---------------------------------------------------------------------------
# is_structurally_prunable
# ---------------------------------------------------------------------------


def test_structural_prune():
    assert discovery.is_structurally_prunable("https://x.com/login")
    assert discovery.is_structurally_prunable("https://x.com/cart")
    assert discovery.is_structurally_prunable("https://x.com/assets/app.js")
    assert not discovery.is_structurally_prunable("https://x.com/about")


def test_prune_logout():
    assert discovery.is_structurally_prunable("https://x.com/logout")


def test_prune_signup_variants():
    assert discovery.is_structurally_prunable("https://x.com/signup")
    assert discovery.is_structurally_prunable("https://x.com/sign-up")
    assert discovery.is_structurally_prunable("https://x.com/register")


def test_prune_checkout():
    assert discovery.is_structurally_prunable("https://x.com/checkout/summary")


def test_prune_admin():
    assert discovery.is_structurally_prunable("https://x.com/admin/dashboard")


def test_prune_assets_by_extension():
    assert discovery.is_structurally_prunable("https://x.com/style.css")
    assert discovery.is_structurally_prunable("https://x.com/logo.png")
    assert discovery.is_structurally_prunable("https://x.com/font.woff2")


def test_not_prunable_legitimate_pages():
    assert not discovery.is_structurally_prunable("https://x.com/pricing")
    assert not discovery.is_structurally_prunable("https://x.com/features")
    assert not discovery.is_structurally_prunable("https://x.com/contact")
    assert not discovery.is_structurally_prunable("https://x.com/blog/ai-tips")


# ---------------------------------------------------------------------------
# guard_url
# ---------------------------------------------------------------------------


def test_guard_url_passes_valid_host():
    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        assert discovery.guard_url("https://example.com/") is True


def test_guard_url_blocks_when_resolve_raises():
    with patch(
        "app.auditor.discovery.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        assert discovery.guard_url("https://internal.example.com/") is False


def test_guard_url_rejects_no_hostname():
    assert discovery.guard_url("not-a-url") is False


def test_guard_url_calls_resolve_with_correct_hostname():
    """Verify resolve_and_validate_host is called with the URL hostname."""
    calls = []

    def fake_resolve(hostname, **kwargs):
        calls.append(hostname)
        return "1.2.3.4"

    with patch("app.auditor.discovery.resolve_and_validate_host", side_effect=fake_resolve):
        discovery.guard_url("https://example.com/some/path")

    assert calls == ["example.com"]


# ---------------------------------------------------------------------------
# discover — sitemap + prune + cap + SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_uses_sitemap_prunes_caps_and_guards(monkeypatch):
    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset><url><loc>https://ex.com/</loc></url>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://ex.com/login</loc></url>"
            "<url><loc>https://ex.com/pricing</loc></url></urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    found = await discovery.discover("https://ex.com", max_pages=2, fetch_text=fake_fetch)
    assert "https://ex.com/login" not in found  # pruned
    assert len(found) == 2  # capped
    assert "https://ex.com/" in found


@pytest.mark.asyncio
async def test_discover_drops_ssrf_blocked_urls(monkeypatch):
    pages = {"https://ex.com/sitemap.xml": "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"}

    async def fake_fetch(url):
        return pages.get(url)

    monkeypatch.setattr(discovery, "guard_url", lambda u: False)
    found = await discovery.discover("https://ex.com", max_pages=10, fetch_text=fake_fetch)
    assert found == []  # everything blocked by SSRF guard


@pytest.mark.asyncio
async def test_discover_ssrf_guard_called_for_every_candidate_url():
    """Prove resolve_and_validate_host is invoked for every candidate, including root."""
    guarded: list[str] = []

    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://ex.com/pricing</loc></url>"
            "</urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    def tracking_guard(url: str) -> bool:
        guarded.append(url)
        return True

    with patch("app.auditor.discovery.guard_url", side_effect=tracking_guard):
        await discovery.discover("https://ex.com", max_pages=10, fetch_text=fake_fetch)

    # Root + every sitemap loc must be guarded
    assert "https://ex.com" in guarded or "https://ex.com/" in guarded
    assert "https://ex.com/about" in guarded
    assert "https://ex.com/pricing" in guarded


@pytest.mark.asyncio
async def test_discover_rejected_urls_excluded_from_result():
    """URLs for which resolve_and_validate_host raises are not in the result."""
    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://ex.com/safe</loc></url>"
            "<url><loc>https://internal.local/secret</loc></url>"
            "</urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    def selective_guard(url: str) -> bool:
        # Block anything on internal.local
        if "internal.local" in url:
            return False
        return True

    with patch("app.auditor.discovery.guard_url", side_effect=selective_guard):
        found = await discovery.discover("https://ex.com", max_pages=50, fetch_text=fake_fetch)

    assert "https://internal.local/secret" not in found
    assert "https://ex.com/safe" in found


@pytest.mark.asyncio
async def test_discover_respects_max_pages_cap():
    """discover must not return more URLs than max_pages."""
    sitemap_locs = "".join(
        f"<url><loc>https://ex.com/page{i}</loc></url>" for i in range(20)
    )
    pages = {
        "https://ex.com/sitemap.xml": f"<urlset>{sitemap_locs}</urlset>",
    }

    async def fake_fetch(url):
        return pages.get(url)

    with patch("app.auditor.discovery.guard_url", return_value=True):
        found = await discovery.discover("https://ex.com", max_pages=5, fetch_text=fake_fetch)

    assert len(found) <= 5


@pytest.mark.asyncio
async def test_discover_returns_empty_when_root_fails_guard():
    """If the root URL itself fails SSRF guard, discover returns []."""

    async def fake_fetch(url):
        return None

    with patch("app.auditor.discovery.guard_url", return_value=False):
        found = await discovery.discover("https://private.local", max_pages=10, fetch_text=fake_fetch)

    assert found == []


@pytest.mark.asyncio
async def test_discover_deduplicates_urls():
    """Same URL appearing multiple times in sitemap is only returned once."""
    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://ex.com/about</loc></url>"
            "</urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    with patch("app.auditor.discovery.guard_url", return_value=True):
        found = await discovery.discover("https://ex.com", max_pages=50, fetch_text=fake_fetch)

    assert found.count("https://ex.com/about") == 1


@pytest.mark.asyncio
async def test_discover_bfs_fallback_when_no_sitemap():
    """When sitemap is absent, BFS link-following is used as fallback."""
    pages = {
        "https://ex.com/sitemap.xml": None,  # no sitemap
        "https://ex.com/": '<html><a href="/about">About</a><a href="/pricing">Pricing</a></html>',
        "https://ex.com/about": "<html><p>About us</p></html>",
        "https://ex.com/pricing": "<html><p>Pricing</p></html>",
    }

    async def fake_fetch(url):
        return pages.get(url)

    with patch("app.auditor.discovery.guard_url", return_value=True):
        found = await discovery.discover("https://ex.com", max_pages=10, fetch_text=fake_fetch)

    assert "https://ex.com/about" in found
    assert "https://ex.com/pricing" in found


@pytest.mark.asyncio
async def test_discover_excludes_cross_domain_urls():
    """URLs on different domains from root are excluded."""
    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://other.com/evil</loc></url>"
            "</urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    with patch("app.auditor.discovery.guard_url", return_value=True):
        found = await discovery.discover("https://ex.com", max_pages=50, fetch_text=fake_fetch)

    assert "https://other.com/evil" not in found
    assert "https://ex.com/about" in found


# ---------------------------------------------------------------------------
# Fix 1 + Fix 2 SSRF guard tests (sitemap fetch + _default_fetch_text)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_sitemap_ssrf_blocked_host_skips_fetch():
    """When guard_url blocks the sitemap URL, fetch_text is NOT called for it."""
    fetch_calls = []

    async def spy_fetch(url):
        fetch_calls.append(url)
        return None

    # guard_url: block the sitemap URL (contains 'sitemap'), allow everything else
    def selective_guard(url: str) -> bool:
        return "sitemap" not in url

    with patch("app.auditor.discovery.guard_url", side_effect=selective_guard):
        found = await discovery.discover("https://ex.com", max_pages=10, fetch_text=spy_fetch)

    # fetch_text should NOT have been called for sitemap.xml
    assert not any("sitemap" in c for c in fetch_calls), (
        f"sitemap URL was fetched despite SSRF guard: {fetch_calls}"
    )


@pytest.mark.asyncio
async def test_discovery_default_fetch_text_blocks_internal_host():
    """_default_fetch_text returns None for an internal host (no HTTP request)."""
    with patch(
        "app.auditor.discovery.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        result = await discovery._default_fetch_text("http://192.168.1.1/sitemap.xml")
    assert result is None


@pytest.mark.asyncio
async def test_discovery_default_fetch_text_returns_none_on_3xx():
    """_default_fetch_text returns None on a 3xx redirect (follow_redirects=False)."""
    import httpx

    async def mock_send(self_or_request, *args, **kwargs):
        return httpx.Response(301, headers={"Location": "http://internal.host/new"})

    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await discovery._default_fetch_text("https://example.com/sitemap.xml")
    assert result is None


@pytest.mark.asyncio
async def test_discovery_default_fetch_text_returns_text_on_200():
    """_default_fetch_text returns body text on a normal 200 response."""
    import httpx

    async def mock_send(self_or_request, *args, **kwargs):
        return httpx.Response(200, text="<urlset><url><loc>https://ex.com/</loc></url></urlset>")

    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=mock_send):
            result = await discovery._default_fetch_text("https://example.com/sitemap.xml")
    assert result is not None
    assert "<urlset>" in result


# ---------------------------------------------------------------------------
# httpx PIN tests — verify resolve-and-pin contract in _default_fetch_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_fetch_text_connects_to_pinned_ip():
    """_default_fetch_text must connect to the pinned IP, not re-resolve by hostname."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await discovery._default_fetch_text("https://example.com/sitemap.xml")

    assert len(captured) == 1
    req = captured[0]
    # Connection must go to the pinned IP, not the original hostname.
    assert req.url.host == "1.2.3.4", f"Expected pinned IP, got: {req.url.host}"


@pytest.mark.asyncio
async def test_discovery_fetch_text_sends_original_host_header():
    """_default_fetch_text must set the Host header to the original hostname."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await discovery._default_fetch_text("https://example.com/sitemap.xml")

    assert len(captured) == 1
    req = captured[0]
    assert req.headers.get("host") == "example.com", (
        f"Expected Host: example.com, got: {req.headers.get('host')}"
    )


@pytest.mark.asyncio
async def test_discovery_fetch_text_sets_sni_hostname_extension():
    """_default_fetch_text must set sni_hostname when connecting to an IP."""
    import httpx

    captured: list[httpx.Request] = []

    async def capturing_send(self, request, *args, **kwargs):
        captured.append(request)
        return httpx.Response(200, text="ok")

    with patch("app.auditor.discovery.resolve_and_validate_host", return_value="1.2.3.4"):
        with patch("httpx.AsyncClient.send", new=capturing_send):
            await discovery._default_fetch_text("https://example.com/sitemap.xml")

    assert len(captured) == 1
    req = captured[0]
    assert req.extensions.get("sni_hostname") == "example.com", (
        f"Expected sni_hostname='example.com', got: {req.extensions.get('sni_hostname')}"
    )


@pytest.mark.asyncio
async def test_discovery_fetch_text_no_send_on_internal_host():
    """_default_fetch_text must NOT call send when resolve raises (fail closed)."""
    import httpx

    send_called = []

    async def should_not_be_called(self, request, *args, **kwargs):
        send_called.append(request)
        return httpx.Response(200, text="leaked")

    with patch(
        "app.auditor.discovery.resolve_and_validate_host",
        side_effect=ValueError("private IP"),
    ):
        with patch("httpx.AsyncClient.send", new=should_not_be_called):
            result = await discovery._default_fetch_text("http://10.0.0.1/sitemap.xml")

    assert result is None
    assert send_called == [], "send must not be called for an internal host"


@pytest.mark.asyncio
async def test_discovery_fetch_text_rejects_non_http_scheme():
    """_default_fetch_text must return None for non-http/https schemes without resolving."""
    resolve_called = []

    def should_not_resolve(hostname, *args, **kwargs):
        resolve_called.append(hostname)
        return "1.2.3.4"

    with patch("app.auditor.discovery.resolve_and_validate_host", side_effect=should_not_resolve):
        result_ftp = await discovery._default_fetch_text("ftp://example.com/file.txt")
        result_file = await discovery._default_fetch_text("file:///etc/passwd")

    assert result_ftp is None
    assert result_file is None
    assert resolve_called == [], "resolve must not be called for non-http/https schemes"
