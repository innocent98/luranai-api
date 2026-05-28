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


def ensure_safe_outbound_url(url: str, allowed_hostnames: set[str] | None = None) -> None:
    """
    Validate outbound URL for SSRF protection.

    Raises:
        ValueError: If URL is malformed or points to disallowed internal/private network ranges.
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")

    if not parsed.hostname:
        raise ValueError("URL hostname is required")

    hostname = parsed.hostname.strip().lower()
    allowed_hostnames = allowed_hostnames or set()
    if hostname in allowed_hostnames:
        return

    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise ValueError("Localhost/internal hostnames are not allowed")

    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname: {hostname}") from exc

    resolved_ips: set[str] = set()
    for entry in addrinfo:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        # sockaddr[0] is the host (str) for both IPv4 (host, port) and IPv6
        # (host, port, flowinfo, scope_id), but stubs widen it to str | int.
        ip = sockaddr[0]
        if ip:
            resolved_ips.add(str(ip))

    if not resolved_ips:
        raise ValueError(f"No IP addresses resolved for hostname: {hostname}")

    for ip_str in resolved_ips:
        if _is_disallowed_ip(ip_str):
            raise ValueError(f"URL resolves to disallowed IP: {ip_str}")
