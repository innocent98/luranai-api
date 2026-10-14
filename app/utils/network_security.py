"""Network security helpers for outbound HTTP requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class HostResolutionError(ValueError):
    """A hostname could not be resolved to any IP address.

    Deliberately a ValueError subclass: outbound-request gates across the spine
    are written as ``except ValueError`` and MUST keep failing closed when DNS
    fails. Widening the type would make every one of them fail open.

    The subclass exists so callers that need to explain themselves can tell
    "we could not look this host up" apart from "it resolves somewhere unsafe".
    The auditor needs that distinction: a domain that does not resolve is the
    user's typo, not a fault of our renderer, and must not be reported as one.
    """


def _embedded_ipv4(ip_obj: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address can tunnel to (mapped, NAT64, 6to4, Teredo)."""
    embedded: list[ipaddress.IPv4Address] = []
    if ip_obj.ipv4_mapped is not None:
        embedded.append(ip_obj.ipv4_mapped)
    if ip_obj.sixtofour is not None:
        embedded.append(ip_obj.sixtofour)
    if ip_obj.teredo is not None:
        embedded.extend(ip_obj.teredo)
    if ip_obj in _NAT64_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF))
    return embedded


_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _is_disallowed_ip(ip_str: str) -> bool:
    """Return True if IP is not safe for outbound tenant-controlled traffic.

    Anything that is not globally routable is rejected (private, loopback,
    link-local, CGNAT, reserved, ...), and IPv6 forms that embed an IPv4
    address (IPv4-mapped, NAT64, 6to4, Teredo) are judged by the IPv4 they
    carry, so ``::ffff:169.254.169.254`` cannot slip through.
    """
    ip_obj = ipaddress.ip_address(ip_str.split("%", 1)[0])
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if any(_is_disallowed_ip(str(v4)) for v4 in _embedded_ipv4(ip_obj)):
            return True
    return (
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _resolve_ips(hostname: str) -> set[str]:
    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HostResolutionError(f"Could not resolve hostname: {hostname}") from exc

    resolved_ips: set[str] = set()
    for entry in addrinfo:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip:
            resolved_ips.add(str(ip))

    if not resolved_ips:
        raise HostResolutionError(f"No IP addresses resolved for hostname: {hostname}")
    return resolved_ips


def _reject_unsafe_hostname(hostname: str) -> None:
    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise ValueError("Localhost/internal hostnames are not allowed")


def resolve_and_validate_hosts(
    hostname: str, allowed_hostnames: set[str] | None = None
) -> list[str]:
    """Resolve ``hostname`` ONCE, validate every answer, and return ALL of them,
    IPv4 first (then IPv6), each group sorted for determinism.

    IPv4 is preferred because most deployments have no IPv6 route; callers that
    can retry (``app.utils.pinned_http``) walk the list on connect failure, which
    restores the happy-eyeballs behaviour connect-by-name used to give.
    Allowlisted hostnames are returned unchanged as a single-item list.

    Raises:
        HostResolutionError: the name did not resolve (transient/typo; a ValueError).
        ValueError: internal hostname, or ANY answer is a disallowed IP.
    """
    hostname = hostname.strip().lower()
    allowed_hostnames = allowed_hostnames or set()
    if hostname in allowed_hostnames:
        return [hostname]

    _reject_unsafe_hostname(hostname)

    resolved_ips = _resolve_ips(hostname)
    for ip_str in resolved_ips:
        if _is_disallowed_ip(ip_str):
            raise ValueError(f"URL resolves to disallowed IP: {ip_str}")

    return sorted(resolved_ips, key=lambda ip: (ipaddress.ip_address(ip).version, ip))


def resolve_and_validate_host(hostname: str, allowed_hostnames: set[str] | None = None) -> str:
    """
    Resolve a hostname ONCE, validate every resolved IP, and return a single
    safe IP to connect to (IPv4 preferred). Callers must connect to the returned
    value (not the hostname) to prevent DNS-rebinding between check and request.

    Connection contract: connect to the returned IP while still sending the
    ORIGINAL hostname as the Host header and TLS SNI / cert-verification name
    (do not splice the IP into the URL host, which would break TLS and drop the
    Host header). For IPv6 results, bracket the address if placed in a URL.

    Allowlisted hostnames bypass IP checks and are returned unchanged so the
    caller connects by hostname (used for internal allowlisted endpoints).

    Raises:
        ValueError: if the hostname is internal, unresolvable, or resolves to
        any disallowed IP range.
    """
    return resolve_and_validate_hosts(hostname, allowed_hostnames)[0]


def ensure_safe_outbound_url(url: str, allowed_hostnames: set[str] | None = None) -> None:
    """
    Validate outbound URL for SSRF protection. Kept for callers that only need
    a yes/no gate. New code should prefer resolve_and_validate_host + pinning.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL hostname is required")
    resolve_and_validate_host(parsed.hostname, allowed_hostnames)
