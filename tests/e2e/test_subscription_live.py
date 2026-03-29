from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


def test_subscription_details_are_available(pro_client):
    payload = assert_ok(pro_client.get("/subscription/my-subscription"))
    assert payload["subscription_tier"] == "professional"
    assert payload["subscription_status"] in {"active", "trial", "past_due"}


def test_downgrade_is_blocked_when_usage_exceeds_lite_limits(pro_client):
    response = pro_client.post(
        "/subscription/downgrade",
        json={"tier": "lite", "reason": "staging-e2e"},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "data_exceeds_limits"
    violation_fields = {item["field"] for item in detail["violations"]}
    assert "flows" in violation_fields
    assert "automation_actions" in violation_fields
