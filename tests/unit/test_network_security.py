"""Unit tests for outbound network safety checks."""

import socket
from unittest.mock import patch

import pytest

from app.utils.network_security import (
    HostResolutionError,
    ensure_safe_outbound_url,
    resolve_and_validate_host,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost:8000",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://172.16.0.1",
        "http://169.254.1.1",
        "http://[::1]",
    ],
)
def test_blocks_private_and_local_urls(url: str):
    with pytest.raises(ValueError):
        ensure_safe_outbound_url(url)


def test_blocks_non_http_schemes():
    with pytest.raises(ValueError):
        ensure_safe_outbound_url("ftp://example.com/resource")


def test_allows_public_ip_url():
    ensure_safe_outbound_url("https://8.8.8.8/path")


def _addrinfo(ip: str):
    # Mirrors socket.getaddrinfo return shape: (family, type, proto, canonname, sockaddr)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def test_resolve_and_validate_returns_public_ip():
    with patch(
        "app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")
    ):
        assert resolve_and_validate_host("example.com") == "93.184.216.34"


def test_resolve_and_validate_rejects_private_ip():
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")):
        with pytest.raises(ValueError):
            resolve_and_validate_host("internal.example.com")


def test_resolve_and_validate_rejects_when_any_ip_unsafe():
    infos = _addrinfo("93.184.216.34") + _addrinfo("127.0.0.1")
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=infos):
        with pytest.raises(ValueError):
            resolve_and_validate_host("rebind.example.com")


def test_resolve_and_validate_allowlisted_hostname_skips_resolution():
    # Allowlisted hostnames bypass IP checks and return the hostname itself.
    assert (
        resolve_and_validate_host("internal-api", allowed_hostnames={"internal-api"})
        == "internal-api"
    )


def test_resolve_and_validate_rejects_localhost():
    with pytest.raises(ValueError):
        resolve_and_validate_host("localhost")


# ---------------------------------------------------------------------------
# HostResolutionError — "we couldn't look it up" vs "it resolves to something
# unsafe". Callers gate outbound traffic with `except ValueError`, so this must
# stay a ValueError subclass or those gates would fail OPEN on a DNS error.
# ---------------------------------------------------------------------------


def test_resolution_failure_raises_host_resolution_error():
    with patch("app.utils.network_security.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(HostResolutionError):
            resolve_and_validate_host("no-such-domain.example")


def test_host_resolution_error_is_a_value_error():
    """Existing SSRF gates catch ValueError; they must keep failing closed."""
    assert issubclass(HostResolutionError, ValueError)
    with patch("app.utils.network_security.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(ValueError):
            resolve_and_validate_host("no-such-domain.example")


def test_empty_resolution_raises_host_resolution_error():
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=[]):
        with pytest.raises(HostResolutionError):
            resolve_and_validate_host("empty.example")


def test_disallowed_ip_is_not_a_resolution_error():
    """A host that resolves to a private IP is unsafe, not unresolvable — the
    two must stay distinguishable so the auditor can word its error honestly."""
    with patch(
        "app.utils.network_security.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("127.0.0.1", 0))],
    ):
        with pytest.raises(ValueError) as exc:
            resolve_and_validate_host("rebound.example")
        assert not isinstance(exc.value, HostResolutionError)


@pytest.mark.parametrize(
    "ip",
    [
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        "100.64.0.1",
        "fe80::1",
        "fd00::1",
        "0.0.0.0",
    ],
)
def test_resolve_rejects_embedded_and_non_global_addresses(ip: str):
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo(ip)):
        with pytest.raises(ValueError):
            resolve_and_validate_host("rebind.example.com")


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_resolve_accepts_public_addresses(ip: str):
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo(ip)):
        assert resolve_and_validate_host("ok.example.com") == ip


def test_resolve_prefers_ipv4_on_dual_stack():
    # "2600:..." string-sorts before "52.x"; the pinned IP must still be IPv4.
    answers = _addrinfo("2600:1f18::1") + _addrinfo("52.1.2.3")
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=answers):
        assert resolve_and_validate_host("dual.example.com") == "52.1.2.3"
