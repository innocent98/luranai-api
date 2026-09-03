from __future__ import annotations

import pytest
import requests

from tests.e2e.conftest import LiveApiClient
from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


@pytest.mark.smoke
def test_tenant_login_and_me_endpoint(e2e_config):
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    login_payload = client.login(e2e_config.pro.email, e2e_config.pro.password)
    assert login_payload["access_token"]

    me = assert_ok(client.get("/auth/me"))
    assert me["email"] == e2e_config.pro.email
    assert me["role"] == "tenant_admin"


def test_invalid_login_returns_auth_error(e2e_config):
    response = requests.post(
        f"{e2e_config.base_url}/auth/login",
        json={"email": e2e_config.pro.email, "password": "definitely-wrong-password"},
        timeout=e2e_config.timeout_seconds,
    )
    assert response.status_code in {400, 401}, response.text
