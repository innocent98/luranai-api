from __future__ import annotations

import pytest
import requests


pytestmark = pytest.mark.e2e


@pytest.mark.smoke
def test_health_endpoint_is_reachable(e2e_config):
    response = requests.get(f"{e2e_config.base_url}/health", timeout=e2e_config.timeout_seconds)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("status") in {"healthy", "ok", "success"}
