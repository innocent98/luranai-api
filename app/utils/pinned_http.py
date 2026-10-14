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
import zlib
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


class ResponseRejectedError(ValueError):
    """The response body was refused (too large, or undecodable within bounds).
    A property of the remote response, so retrying will not help."""


class ResponseTooLargeError(ResponseRejectedError):
    """The response body exceeded the caller's byte cap (wire or decoded)."""


class UnsupportedContentEncodingError(ResponseRejectedError):
    """Compressed with an encoding we cannot decode within the byte cap."""


DEFAULT_MAX_BYTES = 1024 * 1024
_ACCEPT_ENCODING = "gzip, deflate"  # what _BodyBuilder can decode under a cap
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
    for name in [k for k in request_headers if k.lower() in {"host", "accept-encoding"}]:
        del request_headers[name]
    request_headers["Host"] = host_header
    # Only advertise encodings the capped body reader can decode within bounds.
    request_headers["Accept-Encoding"] = _ACCEPT_ENCODING

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


class _BodyBuilder:
    """Accumulates a response body under a byte cap.

    The cap is applied to BOTH the wire bytes and the decoded bytes. Decoding is
    done here (httpx's own decoder would expand a whole network chunk at once, so
    a gzip bomb could allocate far past the cap before we noticed): zlib is driven
    with ``max_length`` so at most ``remaining + 1`` decoded bytes ever exist.
    """

    def __init__(self, response: httpx.Response, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.wire = 0
        self.decoded = 0
        self.chunks: list[bytes] = []
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding in {"", "identity"}:
            self._mode = "identity"
        elif encoding in {"gzip", "x-gzip", "deflate"}:
            self._mode = encoding
        else:
            raise UnsupportedContentEncodingError(f"Unsupported content-encoding: {encoding[:32]}")
        self._d = zlib.decompressobj(zlib.MAX_WBITS | 32) if self._mode != "identity" else None
        self._first = True
        _check_declared(response, max_bytes)

    def _too_large(self) -> ResponseTooLargeError:
        return ResponseTooLargeError(f"Response larger than {self.max_bytes} bytes")

    def _inflate(self, data: bytes) -> bytes:
        assert self._d is not None
        room = self.max_bytes - self.decoded
        try:
            out = self._d.decompress(data, room + 1)
            while self._d.unconsumed_tail and len(out) <= room:
                out += self._d.decompress(self._d.unconsumed_tail, room + 1 - len(out))
        except zlib.error as exc:
            if self._mode == "deflate" and self._first:
                # Some servers send raw deflate (no zlib header).
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
                self._first = False
                return self._inflate(data)
            raise UnsupportedContentEncodingError("Invalid compressed response body") from exc
        self._first = False
        return out

    def feed(self, wire_chunk: bytes) -> None:
        self.wire += len(wire_chunk)
        if self.wire > self.max_bytes:
            raise self._too_large()
        data = wire_chunk if self._d is None else self._inflate(wire_chunk)
        self.decoded += len(data)
        if self.decoded > self.max_bytes:
            raise self._too_large()
        self.chunks.append(data)

    def finish(self, response: httpx.Response) -> httpx.Response:
        if self._d is not None and not self._d.eof and self.wire:
            raise UnsupportedContentEncodingError("Truncated compressed response body")
        return _buffered(response, b"".join(self.chunks))


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


def _check_declared(response: httpx.Response, max_bytes: int) -> None:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")


def _already_buffered(response: httpx.Response) -> bytes | None:
    """Body of a response that was fully read before we got it (test doubles)."""
    try:
        return response.content
    except httpx.ResponseNotRead:
        return None


async def aread_capped(response: httpx.Response, max_bytes: int) -> httpx.Response:
    """Read a streamed response under the byte cap (wire and decoded), closing it."""
    try:
        _check_declared(response, max_bytes)
        buffered = _already_buffered(response)
        if buffered is not None:
            if len(buffered) > max_bytes:
                raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
            return _buffered(response, buffered)
        builder = _BodyBuilder(response, max_bytes)
        async for chunk in response.aiter_raw():
            builder.feed(chunk)
        return builder.finish(response)
    finally:
        await response.aclose()


def _read_capped(response: httpx.Response, max_bytes: int, deadline: float) -> httpx.Response:
    try:
        _check_declared(response, max_bytes)
        buffered = _already_buffered(response)
        if buffered is not None:
            if len(buffered) > max_bytes:
                raise ResponseTooLargeError(f"Response larger than {max_bytes} bytes")
            return _buffered(response, buffered)
        builder = _BodyBuilder(response, max_bytes)
        for chunk in response.iter_raw():
            builder.feed(chunk)
            if time.monotonic() > deadline:
                raise TimeoutError("Outbound request exceeded its total deadline")
        return builder.finish(response)
    finally:
        response.close()


_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


def _remaining_timeout(timeout: float | httpx.Timeout, deadline: float) -> float | httpx.Timeout:
    """Per-operation timeout clipped to what is left of the total deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Outbound request exceeded its total deadline")
    if isinstance(timeout, httpx.Timeout):
        return httpx.Timeout(
            connect=None if timeout.connect is None else min(timeout.connect, remaining),
            read=None if timeout.read is None else min(timeout.read, remaining),
            write=None if timeout.write is None else min(timeout.write, remaining),
            pool=None if timeout.pool is None else min(timeout.pool, remaining),
        )
    return min(float(timeout), remaining)


def _request_for(hop: _Hop, ip: str) -> httpx.Request:
    return build_pinned_request(
        hop.method,
        hop.url,
        ip,
        headers=hop.headers,
        params=hop.params,
        json=hop.json,
    )


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
    (DNS failure, retryable), ``ResponseRejectedError`` (body over ``max_bytes`` on
    the wire or decoded, or undecodable), ``TimeoutError`` (over ``total_timeout``,
    which covers DNS, every fallback IP, every redirect hop and the body); httpx
    transport errors propagate unchanged. On connect failure the next validated
    IP is tried. A fresh connection is used per hop, so a redirect to another
    hostname never reuses a pooled connection (and its TLS identity). The body of
    a redirect that is followed is never read. ``transport`` is a test seam.
    """

    async def run() -> httpx.Response:
        hop = _Hop(method.upper(), url, dict(headers or {}), params, json)
        while True:
            # getaddrinfo blocks: keep it off the event loop.
            ips = (await asyncio.to_thread(resolve_pinned_ips, hop.url, allowed_hostnames))[
                :_MAX_CONNECT_ATTEMPTS
            ]
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, transport=transport
            ) as client:
                streamed: httpx.Response | None = None
                for attempt, ip in enumerate(ips):
                    try:
                        streamed = await client.send(_request_for(hop, ip), stream=True)
                        break
                    except _CONNECT_ERRORS:
                        if attempt + 1 >= len(ips):
                            raise
                assert streamed is not None
                try:
                    nxt = _next_hop(hop, streamed, max_redirects)
                except BaseException:
                    await streamed.aclose()
                    raise
                if nxt is not None:
                    await streamed.aclose()  # redirect body is discarded unread
                else:
                    response = await aread_capped(streamed, max_bytes)
            if nxt is None:
                response.extensions[_ORIGINAL_URL_KEY] = hop.url
                return response
            hop = nxt

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
    """Sync twin of ``apinned_request``. The total deadline is checked before every
    DNS lookup, IP attempt and hop, per-operation timeouts are clipped to what is
    left of it, and body reads check it between chunks."""
    deadline = time.monotonic() + (total_timeout if total_timeout is not None else 1e9)
    hop = _Hop(method.upper(), url, dict(headers or {}), params, json)
    while True:
        _remaining_timeout(timeout, deadline)  # raises if already out of time
        ips = resolve_pinned_ips(hop.url, allowed_hostnames)[:_MAX_CONNECT_ATTEMPTS]
        with httpx.Client(
            timeout=_remaining_timeout(timeout, deadline),
            follow_redirects=False,
            transport=transport,
        ) as client:
            streamed: httpx.Response | None = None
            for attempt, ip in enumerate(ips):
                try:
                    streamed = client.send(_request_for(hop, ip), stream=True)
                    break
                except _CONNECT_ERRORS:
                    if attempt + 1 >= len(ips):
                        raise
                    _remaining_timeout(timeout, deadline)
            assert streamed is not None
            try:
                nxt = _next_hop(hop, streamed, max_redirects)
            except BaseException:
                streamed.close()
                raise
            if nxt is not None:
                streamed.close()  # redirect body is discarded unread
            else:
                response = _read_capped(streamed, max_bytes, deadline)
        if nxt is None:
            response.extensions[_ORIGINAL_URL_KEY] = hop.url
            return response
        hop = nxt
