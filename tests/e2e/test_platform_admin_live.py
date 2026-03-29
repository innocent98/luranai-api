from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


def test_platform_admin_can_reach_core_admin_endpoints(platform_client):
    profile = assert_ok(platform_client.get("/platform/auth/me"))
    assert profile["email"]
