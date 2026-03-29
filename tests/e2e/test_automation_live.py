from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


def test_professional_tenant_can_list_actions_and_secrets(pro_client):
    actions = assert_ok(pro_client.get("/tenant/automation/actions"))
    secrets = assert_ok(pro_client.get("/tenant/automation/secrets"))

    assert any(action["slug"] == "track_order_status_chat" for action in actions)
    assert any(secret["key_name"] == "sandbox_bearer_token" for secret in secrets)


def test_lite_tenant_action_creation_is_blocked_at_limit(lite_client):
    response = lite_client.post(
        "/tenant/automation/actions",
        json={
            "name": "Limit Test Action",
            "slug": "limit_test_action",
            "description": "Should be rejected at plan limit",
            "status": "active",
            "type": "http",
            "method": "GET",
            "url_template": "http://host.docker.internal:8000/api/v1/automation-sandbox/orders/status",
            "timeout_seconds": 20,
            "auth_type": "none",
            "auth_config": {},
            "headers_template": {},
            "query_template": {"order_reference": "{{order_reference}}"},
            "body_template": {},
            "input_schema": {"required": ["order_reference"]},
            "output_schema": {},
            "error_mapping": {},
            "retry_policy": {"max_attempts": 1, "backoff_ms": 0},
            "is_sensitive": False,
            "requires_approval": False,
            "allow_in_flows": False,
            "available_in_ai_chat": False,
        },
    )
    assert response.status_code == 403, response.text
    assert "automation action limit" in response.json()["detail"].lower()


def test_quota_tenant_execution_limit_blocks_action_runs(quota_client):
    actions = assert_ok(quota_client.get("/tenant/automation/actions"))
    target = next(action for action in actions if action["slug"] == "track_order_status_chat")
    response = quota_client.post(
        f"/tenant/automation/actions/{target['id']}/test",
        json={"input_payload": {"order_reference": "ORD1234"}},
    )
    payload = assert_ok(response)
    assert payload["status"] == "blocked_policy"
    assert payload["error_code"] == "AUTOMATION_EXECUTION_LIMIT_REACHED"
