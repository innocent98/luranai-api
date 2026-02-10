"""Unit tests for outbound network safety checks."""

import pytest

from app.utils.network_security import ensure_safe_outbound_url


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
