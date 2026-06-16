"""SSRF pinning: _build_pinned_request connects to the validated IP while
preserving the original Host header and TLS SNI (no DNS re-resolution)."""

from unittest.mock import AsyncMock

from app.services.automation_service import AutomationService


def _service():
    return AutomationService(db=AsyncMock())


def test_pinned_request_uses_ip_and_preserves_host_and_sni():
    request = _service()._build_pinned_request(
        method="GET",
        url="https://api.example.com/orders/42",
        headers={},
        query={},
        body={},
        safe_ip="93.184.216.34",
    )
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "api.example.com"
    assert request.extensions.get("sni_hostname") == "api.example.com"


def test_pinned_request_preserves_port_in_host_header():
    request = _service()._build_pinned_request(
        method="GET",
        url="https://api.example.com:8443/orders/42",
        headers={},
        query={},
        body={},
        safe_ip="93.184.216.34",
    )
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "api.example.com:8443"
    assert request.extensions.get("sni_hostname") == "api.example.com"


def test_pinned_request_host_header_excludes_userinfo():
    # Credentials in the URL must NOT leak into the Host header.
    request = _service()._build_pinned_request(
        method="GET",
        url="https://user:secret@api.example.com/orders/42",
        headers={},
        query={},
        body={},
        safe_ip="93.184.216.34",
    )
    assert "secret" not in request.headers["host"]
    assert request.headers["host"] == "api.example.com"


def test_allowlisted_host_is_not_pinned_and_has_no_sni_override():
    # When resolve_and_validate_host returns the hostname unchanged (allowlist),
    # safe_ip == original_host: connect by hostname, no SNI override needed.
    request = _service()._build_pinned_request(
        method="GET",
        url="https://internal-api/orders/42",
        headers={},
        query={},
        body={},
        safe_ip="internal-api",
    )
    assert request.url.host == "internal-api"
    assert request.extensions.get("sni_hostname") is None


def test_post_body_is_json_encoded():
    request = _service()._build_pinned_request(
        method="POST",
        url="https://api.example.com/tickets",
        headers={},
        query={},
        body={"subject": "hi"},
        safe_ip="93.184.216.34",
    )
    assert request.url.host == "93.184.216.34"
    assert b"subject" in request.content
