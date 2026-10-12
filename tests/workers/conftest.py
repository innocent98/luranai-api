"""Hermetic DNS for the worker test suite.

Several worker tests seed a real, publicly-resolving webhook host
(``hooks.zapier.com``) so the payload passes the SSRF outbound guard exactly
as it would in production. The guard resolves that hostname through
``socket.getaddrinfo`` before allowing the request, which means those tests
silently depend on live DNS: in a sandbox with no resolver the guard raises
``HostResolutionError`` and the worker records the send as FAILED, surfacing
as a fake CRM bug (``assert FAILED == SENT``) rather than the network problem
it is.

This fixture wraps the real resolver and only synthesises a fixed public IP
*when real resolution fails*. Consequences:

- In CI (open DNS) it is a no-op — real names resolve as before.
- Numeric/literal hosts (e.g. ``169.254.169.254``) never hit DNS, so they flow
  through untouched and the guard's private/link-local range block still fires.
  ``test_crm_blocked_host_marks_failed_no_post`` therefore keeps working.
- Only genuinely-unresolvable hostnames get the synthetic public IP, keeping
  the suite hermetic without weakening the SSRF guard under test.
"""

import socket
from unittest.mock import patch

import pytest

# A stable, documentation-range public IP (RFC 5737 style host that is neither
# private nor link-local), so the guard's IP-range validation passes just as it
# would for any real public webhook host.
_PUBLIC_STANDIN_IP = "93.184.216.34"

_real_getaddrinfo = socket.getaddrinfo


def _offline_tolerant_getaddrinfo(host, *args, **kwargs):
    try:
        return _real_getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_STANDIN_IP, 0))
        ]


@pytest.fixture(autouse=True)
def _hermetic_outbound_dns():
    with patch(
        "app.utils.network_security.socket.getaddrinfo",
        side_effect=_offline_tolerant_getaddrinfo,
    ):
        yield
