from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


@pytest.mark.smoke
def test_professional_tenant_capabilities_are_exposed(pro_client):
    tenant = assert_ok(pro_client.get("/tenant"))
    capabilities = assert_ok(pro_client.get("/tenant/capabilities"))

    assert tenant["subscription_tier"] == "professional"
    assert capabilities["can_use_ai_chat_automation"] is True
    assert capabilities["can_use_lookup_chains"] is True
    assert capabilities["can_use_webhooks"] is True


def test_lite_tenant_capabilities_show_lookup_and_webhooks_blocked(lite_client):
    tenant = assert_ok(lite_client.get("/tenant"))
    capabilities = assert_ok(lite_client.get("/tenant/capabilities"))

    assert tenant["subscription_tier"] == "lite"
    assert capabilities["can_use_ai_chat_automation"] is True
    assert capabilities["can_use_lookup_chains"] is False
    assert capabilities["can_use_webhooks"] is False
