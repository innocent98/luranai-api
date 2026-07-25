import httpx
import pytest

from app.auditor.discovery import resolve_redirect_chain, ResolvedRoot


def _pass_through_guard(monkeypatch):
    """Bypass the real SSRF guard so tests exercise http_get + the resolver's
    own loop/hop logic, with zero network/DNS dependence."""
    monkeypatch.setattr(
        "app.auditor.discovery.resolve_and_validate_host",
        lambda h, *a, **k: "93.184.216.34",
    )


def _chain(*pairs):
    """Build a fake http_get from {url: (status, location)} pairs."""
    table = dict(pairs)

    async def _get(url):
        if url not in table:
            raise AssertionError(f"unexpected GET {url}")
        return table[url]

    return _get


@pytest.mark.asyncio
async def test_no_redirect_returns_same_url_unchanged(monkeypatch):
    _pass_through_guard(monkeypatch)
    get = _chain(("https://example.com", (200, None)))
    r = await resolve_redirect_chain("https://example.com", http_get=get)
    assert isinstance(r, ResolvedRoot)
    assert r.final_url == "https://example.com"
    assert r.changed is False


@pytest.mark.asyncio
async def test_apex_to_www_is_followed(monkeypatch):
    _pass_through_guard(monkeypatch)
    get = _chain(
        ("https://luranai.com", (307, "https://www.luranai.com/")),
        ("https://www.luranai.com/", (200, None)),
    )
    r = await resolve_redirect_chain("https://luranai.com", http_get=get)
    assert r.final_url == "https://www.luranai.com/"
    assert r.changed is True


@pytest.mark.asyncio
async def test_redirect_loop_raises(monkeypatch):
    _pass_through_guard(monkeypatch)
    get = _chain(
        ("https://a.com", (302, "https://b.com")),
        ("https://b.com", (302, "https://a.com")),
    )
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://a.com", http_get=get)


@pytest.mark.asyncio
async def test_exceeding_max_hops_raises(monkeypatch):
    _pass_through_guard(monkeypatch)
    get = _chain(
        ("https://a.com", (302, "https://b.com")),
        ("https://b.com", (302, "https://c.com")),
        ("https://c.com", (302, "https://d.com")),
        ("https://d.com", (302, "https://e.com")),
    )
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://a.com", max_hops=3, http_get=get)


@pytest.mark.asyncio
async def test_internal_hop_is_blocked(monkeypatch):
    # resolve_redirect_chain must re-guard each hop itself, independent of the
    # http_get in use. Point the SSRF guard at a link-local target and confirm
    # it raises even though a fake http_get is injected.
    def fake_resolve(host, *a, **k):
        if host == "internal.example":
            raise ValueError("URL resolves to disallowed IP: 169.254.169.254")
        return "93.184.216.34"

    monkeypatch.setattr("app.auditor.discovery.resolve_and_validate_host", fake_resolve)
    get = _chain(("https://internal.example", (200, None)))
    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://internal.example", http_get=get)


@pytest.mark.asyncio
async def test_guarded_get_transport_error_raises_value_error(monkeypatch):
    # _guarded_get is the default http_get used by resolve_redirect_chain.
    # A transport failure (host unreachable, connection reset, timeout, ...)
    # must surface as ValueError, not the raw httpx exception, so callers that
    # `except ValueError` (e.g. start_audit) fail closed instead of 500ing.
    _pass_through_guard(monkeypatch)

    async def fake_send(self, request, **kwargs):
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    with pytest.raises(ValueError):
        await resolve_redirect_chain("https://unreachable.example")
