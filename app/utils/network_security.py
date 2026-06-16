"""Network security helpers for outbound HTTP requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _is_disallowed_ip(ip_str: str) -> bool:
    """Return True if IP is not safe for outbound tenant-controlled traffic."""
    ip_obj = ipaddress.ip_address(ip_str)
    return any(
        [
            ip_obj.is_private,
            ip_obj.is_loopback,
            ip_obj.is_link_local,
            ip_obj.is_multicast,
            ip_obj.is_reserved,
            ip_obj.is_unspecified,
        ]
    )


def _resolve_ips(hostname: str) -> set[str]:
    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname: {hostname}") from exc

    resolved_ips: set[str] = set()
    for entry in addrinfo:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip:
            resolved_ips.add(str(ip))

    if not resolved_ips:
        raise ValueError(f"No IP addresses resolved for hostname: {hostname}")
    return resolved_ips


def _reject_unsafe_hostname(hostname: str) -> None:
    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise ValueError("Localhost/internal hostnames are not allowed")


def resolve_and_validate_host(
    hostname: str, allowed_hostnames: set[str] | None = None
) -> str:
    """
    Resolve a hostname ONCE, validate every resolved IP, and return a single
    safe IP to connect to. Callers must connect to the returned value (not the
    hostname) to prevent DNS-rebinding between check and request.

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
    hostname = hostname.strip().lower()
    allowed_hostnames = allowed_hostnames or set()
    if hostname in allowed_hostnames:
        return hostname

    _reject_unsafe_hostname(hostname)

    resolved_ips = _resolve_ips(hostname)
    for ip_str in resolved_ips:
        if _is_disallowed_ip(ip_str):
            raise ValueError(f"URL resolves to disallowed IP: {ip_str}")

    # All resolved IPs are safe; pin a single deterministic address.
    return sorted(resolved_ips)[0]


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
