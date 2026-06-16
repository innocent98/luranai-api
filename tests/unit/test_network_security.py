"""Unit tests for outbound network safety checks."""

import socket
from unittest.mock import patch

import pytest

from app.utils.network_security import ensure_safe_outbound_url, resolve_and_validate_host


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
    with patch("app.utils.network_security.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
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
    assert resolve_and_validate_host("internal-api", allowed_hostnames={"internal-api"}) == "internal-api"


def test_resolve_and_validate_rejects_localhost():
    with pytest.raises(ValueError):
        resolve_and_validate_host("localhost")
