"""Pinned outbound HTTP for tenant- or user-controlled URLs.

Every outbound request to a URL that a tenant or end user can influence goes
through this module. The hostname is resolved ONCE, every resolved address is
validated, and the connection is made to that validated IP, so a second DNS
lookup (DNS rebinding) can never redirect the request to an internal address.

Contract (see ``resolve_and_validate_host``): connect to the pinned IP, send
the ORIGINAL hostname as the ``Host`` header, and use the original hostname for
TLS SNI and certificate verification (httpx ``sni_hostname`` extension).

Redirects are not followed unless the caller opts in with ``max_redirects``;
when they do, every hop is re-validated and re-pinned from scratch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from app.utils.network_security import resolve_and_validate_host

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Credentials that must not follow a redirect to a different origin.
_CROSS_ORIGIN_STRIPPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


_ORIGINAL_URL_KEY = "pinned_original_url"


def original_url(response: httpx.Response) -> str:
    """The URL the caller asked for (``response.url`` carries the pinned IP)."""
    return str(response.extensions.get(_ORIGINAL_URL_KEY) or response.url)


class UnsafeOutboundURLError(ValueError):
    """The URL is not an allowed outbound target (scheme, host, resolved IP, redirect).

    A ValueError subclass so existing ``except ValueError`` fail-closed gates keep
    working; callers that must tell a blocked target from a transport failure
    catch this type specifically.
    """


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str  # ASCII (punycode) hostname or IP literal, no brackets
    port: int | None


def _parse_target(url: str) -> _Target:
    try:
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii") if parsed.raw_host else ""
        scheme = parsed.scheme
        port = parsed.port
    except (httpx.InvalidURL, UnicodeError, ValueError) as exc:
        raise UnsafeOutboundURLError("Invalid URL") from exc
    if scheme not in {"http", "https"}:
        raise UnsafeOutboundURLError("Only http/https URLs are allowed")
    if not host:
        raise UnsafeOutboundURLError("URL hostname is required")
    return _Target(scheme=scheme, host=host, port=port)


def resolve_pinned_ip(url: str, allowed_hostnames: set[str] | None = None) -> str:
    """Validate ``url`` and return the single IP to connect to (or the hostname
    unchanged when it is explicitly allowlisted).

    Blocking (``socket.getaddrinfo``): async callers use ``apinned_request``.
    """
    target = _parse_target(url)
    try:
        return resolve_and_validate_host(target.host, allowed_hostnames)
    except UnsafeOutboundURLError:
        raise
    except ValueError as exc:
        raise UnsafeOutboundURLError(str(exc)) from exc


def build_pinned_request(
    method: str,
    url: str,
    safe_ip: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> httpx.Request:
    """Build a request that connects to ``safe_ip`` but presents ``url``'s host.

    Pure (no DNS). ``safe_ip`` is the value returned by ``resolve_and_validate_host``;
    when it equals the URL hostname (allowlisted host) the request connects by name.
    """
    target = _parse_target(url)
    # Host header: hostname (+ explicit port) only. Never userinfo.
    host_for_header = f"[{target.host}]" if ":" in target.host else target.host
    host_header = f"{host_for_header}:{target.port}" if target.port else host_for_header

    pin = safe_ip != target.host
    pinned_url = httpx.URL(url).copy_with(host=safe_ip) if pin else httpx.URL(url)

    request_headers = dict(headers or {})
    for name in [k for k in request_headers if k.lower() == "host"]:
        del request_headers[name]
    request_headers["Host"] = host_header

    request = httpx.Request(
        method=method,
        url=pinned_url,
        headers=request_headers,
        params=params or None,
        json=json,
    )
    if pin:
        # TLS SNI and certificate verification use the real hostname, not the IP.
        request.extensions["sni_hostname"] = target.host
    return request


@dataclass
class _Hop:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any] | None
    json: Any
    followed: int = 0
    _origin: tuple[str, str, int | None] = field(init=False)

    def __post_init__(self) -> None:
        t = _parse_target(self.url)
        self._origin = (t.scheme, t.host, t.port)


def _next_hop(hop: _Hop, response: httpx.Response, max_redirects: int) -> _Hop | None:
    """Return the next hop for a followable redirect, else None (final response)."""
    if max_redirects <= 0 or response.status_code not in REDIRECT_STATUSES:
        return None
    location = response.headers.get("location")
    if not location:
        return None
    if hop.followed >= max_redirects:
        raise UnsafeOutboundURLError("Too many redirects")
    next_url = urljoin(hop.url, location)

    method, body = hop.method, hop.json
    if response.status_code == 303 and method != "HEAD":
        method, body = "GET", None
    elif response.status_code in {301, 302} and method == "POST":
        method, body = "GET", None

    headers = dict(hop.headers)
    nxt = _Hop(method=method, url=next_url, headers=headers, params=None, json=body)
    nxt.followed = hop.followed + 1
    if nxt._origin != hop._origin:
        nxt.headers = {
            k: v for k, v in headers.items() if k.lower() not in _CROSS_ORIGIN_STRIPPED_HEADERS
        }
    return nxt


async def apinned_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float | httpx.Timeout = 10.0,
    max_redirects: int = 0,
    allowed_hostnames: set[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Async pinned request. Raises ``UnsafeOutboundURLError`` for blocked targets;
    httpx transport errors propagate unchanged. ``transport`` is a test seam."""
    hop: _Hop | None = _Hop(method.upper(), url, dict(headers or {}), params, json)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        while hop is not None:
            # getaddrinfo blocks: keep it off the event loop.
            safe_ip = await asyncio.to_thread(resolve_pinned_ip, hop.url, allowed_hostnames)
            request = build_pinned_request(
                hop.method, hop.url, safe_ip, headers=hop.headers, params=hop.params, json=hop.json
            )
            response = await client.send(request)
            nxt = _next_hop(hop, response, max_redirects)
            if nxt is None:
                response.extensions[_ORIGINAL_URL_KEY] = hop.url
                return response
            await response.aclose()
            hop = nxt
    raise AssertionError("unreachable")  # pragma: no cover


def pinned_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float | httpx.Timeout = 10.0,
    max_redirects: int = 0,
    allowed_hostnames: set[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    """Sync twin of ``apinned_request`` for synchronous workers."""
    hop: _Hop | None = _Hop(method.upper(), url, dict(headers or {}), params, json)
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        while hop is not None:
            safe_ip = resolve_pinned_ip(hop.url, allowed_hostnames)
            request = build_pinned_request(
                hop.method, hop.url, safe_ip, headers=hop.headers, params=hop.params, json=hop.json
            )
            response = client.send(request)
            nxt = _next_hop(hop, response, max_redirects)
            if nxt is None:
                response.extensions[_ORIGINAL_URL_KEY] = hop.url
                return response
            response.close()
            hop = nxt
    raise AssertionError("unreachable")  # pragma: no cover
