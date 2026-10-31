"""Sales tests never touch the network: outbound webhook hosts resolve to a fixed public address.

`test_lead_dashboard_link.py` drives the Slack job against `hooks.slack.com`, whose pinned HTTP
client resolves the host before the (stubbed) POST. With real DNS that test failed whenever the
lookup failed (offline run, resolver hiccup): the job raised "slack network error" and no POST
was made. `tests/workers/conftest.py` already fixes this for the worker tests; this is the same
guard for the sales tests, but without even trying real DNS first.
"""

import socket
from unittest.mock import patch

import pytest

# A documentation-range public IP: neither private nor link-local, so the SSRF guard's range
# validation passes just as it would for a real public webhook host.
_PUBLIC_STANDIN_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _hermetic_outbound_dns():
    def _resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_STANDIN_IP, 0))]

    with patch("app.utils.network_security.socket.getaddrinfo", side_effect=_resolve):
        yield
