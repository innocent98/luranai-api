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
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from app.utils.network_security import HostResolutionError, resolve_and_validate_hosts

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


class OutboundResolutionError(Exception):
    """The hostname did not resolve. Transient (DNS outage) or a typo - NOT a
    policy block, so retry-capable callers should treat it like a network error."""


class ResponseTooLargeError(ValueError):
    """The response body exceeded the caller's byte cap."""


DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_TOTAL_TIMEOUT = 30.0
_MAX_CONNECT_ATTEMPTS = 4


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


def resolve_pinned_ips(url: str, allowed_hostnames: set[str] | None = None) -> list[str]:
    """Validate ``url`` and return every validated IP to try, IPv4 first (or the
    hostname unchanged when it is explicitly allowlisted).

    Blocking (``socket.getaddrinfo``): async callers use ``apinned_request``.
    Raises ``UnsafeOutboundURLError`` for policy blocks and
    ``OutboundResolutionError`` when DNS itself failed.
    """
    target = _parse_target(url)
    try:
        return resolve_and_validate_hosts(target.host, allowed_hostnames)
    except HostResolutionError as exc:
        raise OutboundResolutionError(str(exc)) from exc
    except ValueError as exc:
        raise UnsafeOutboundURLError(str(exc)) from exc


def resolve_pinned_ip(url: str, allowed_hostnames: set[str] | None = None) -> str:
    """First (preferred) validated IP for ``url``; see ``resolve_pinned_ips``."""
    return resolve_pinned_ips(url, allowed_hostnames)[0]


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

    Pure (no DNS). ``safe_ip`` is a value returned by ``resolve_and_validate_host(s)``;
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
    if hop._origin[0] == "https" and _parse_target(next_url).scheme == "http":
        raise UnsafeOutboundURLError("Redirect downgrades https to http")

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


def _buffered(response: httpx.Response, body: bytes) -> httpx.Response:
    """Rebuild a streamed response around its (already decoded) capped body."""
    headers = [
        (k, v)
        for k, v in response.headers.multi_items()
        if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
    ]
    try:
        request: httpx.Request | None = response.request
    except RuntimeError:  # response produced without a request (test doubles)
        request = None
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=body,
        request=request,
        extensions=dict(response.extensions),
    )


def _declared_too_large(response: httpx.Response, max_bytes: int) -> bool:
    declared = response.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > max_bytes


async def _aread_capped(response: httpx.Response, max_bytes: int) -> httpx.Response:
    try:
        if _declared_too_large(response, max_bytes):
            raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
            chunks.append(chunk)
        return _buffered(response, b"".join(chunks))
    finally:
        await response.aclose()


def _read_capped(response: httpx.Response, max_bytes: int, deadline: float) -> httpx.Response:
    try:
        if _declared_too_large(response, max_bytes):
            raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError("Outbound request exceeded its total deadline")
            chunks.append(chunk)
        return _buffered(response, b"".join(chunks))
    finally:
        response.close()


_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


async def apinned_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float | httpx.Timeout = 10.0,
    total_timeout: float | None = DEFAULT_TOTAL_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = 0,
    allowed_hostnames: set[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Async pinned request.

    Raises ``UnsafeOutboundURLError`` (policy block), ``OutboundResolutionError``
    (DNS failure, retryable), ``ResponseTooLargeError`` (body over ``max_bytes``),
    ``TimeoutError`` (over ``total_timeout`` across DNS, hops and body); httpx
    transport errors propagate unchanged. On connect failure the next validated
    IP is tried. A fresh connection is used per hop, so a redirect to another
    hostname never reuses a pooled connection (and its TLS identity).
    ``transport`` is a test seam.
    """

    async def run() -> httpx.Response:
        hop: _Hop | None = _Hop(method.upper(), url, dict(headers or {}), params, json)
        while hop is not None:
            # getaddrinfo blocks: keep it off the event loop.
            ips = await asyncio.to_thread(resolve_pinned_ips, hop.url, allowed_hostnames)
            response: httpx.Response | None = None
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, transport=transport
            ) as client:
                for attempt, ip in enumerate(ips[:_MAX_CONNECT_ATTEMPTS]):
                    request = build_pinned_request(
                        hop.method,
                        hop.url,
                        ip,
                        headers=hop.headers,
                        params=hop.params,
                        json=hop.json,
                    )
                    try:
                        streamed = await client.send(request, stream=True)
                    except _CONNECT_ERRORS:
                        if attempt + 1 >= min(len(ips), _MAX_CONNECT_ATTEMPTS):
                            raise
                        continue
                    response = await _aread_capped(streamed, max_bytes)
                    break
            assert response is not None
            nxt = _next_hop(hop, response, max_redirects)
            if nxt is None:
                response.extensions[_ORIGINAL_URL_KEY] = hop.url
                return response
            hop = nxt
        raise AssertionError("unreachable")  # pragma: no cover

    if total_timeout is None:
        return await run()
    async with asyncio.timeout(total_timeout):
        return await run()


def pinned_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float | httpx.Timeout = 10.0,
    total_timeout: float | None = DEFAULT_TOTAL_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = 0,
    allowed_hostnames: set[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    """Sync twin of ``apinned_request`` for synchronous workers."""
    deadline = time.monotonic() + (total_timeout if total_timeout is not None else 1e9)
    hop: _Hop | None = _Hop(method.upper(), url, dict(headers or {}), params, json)
    while hop is not None:
        ips = resolve_pinned_ips(hop.url, allowed_hostnames)
        response: httpx.Response | None = None
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            for attempt, ip in enumerate(ips[:_MAX_CONNECT_ATTEMPTS]):
                request = build_pinned_request(
                    hop.method, hop.url, ip, headers=hop.headers, params=hop.params, json=hop.json
                )
                try:
                    streamed = client.send(request, stream=True)
                except _CONNECT_ERRORS:
                    if attempt + 1 >= min(len(ips), _MAX_CONNECT_ATTEMPTS):
                        raise
                    continue
                response = _read_capped(streamed, max_bytes, deadline)
                break
        assert response is not None
        nxt = _next_hop(hop, response, max_redirects)
        if nxt is None:
            response.extensions[_ORIGINAL_URL_KEY] = hop.url
            return response
        if time.monotonic() > deadline:
            raise TimeoutError("Outbound request exceeded its total deadline")
        hop = nxt
    raise AssertionError("unreachable")  # pragma: no cover
