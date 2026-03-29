from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


def _webhook_flow_payload(tenant_id: str, *, name: str, webhook_count: int) -> dict:
    nodes = []
    for index in range(webhook_count):
        node_id = f"webhook_{index + 1}"
        next_node_id = "flow_end" if index == webhook_count - 1 else f"webhook_{index + 2}"
        nodes.append(
            {
                "id": node_id,
                "type": "webhook",
                "config": {
                    "url": f"https://example.test/{node_id}",
                    "method": "POST",
                    "next_node_id": next_node_id,
                },
            }
        )
    nodes.append({"id": "flow_end", "type": "end", "config": {"message": "Done"}})
    return {
        "tenant_id": tenant_id,
        "name": name,
        "description": "Live E2E webhook flow",
        "trigger_type": "manual",
        "trigger_config": {},
        "status": "draft",
        "is_active": True,
        "version": 1,
        "nodes": {"start_node_id": "webhook_1", "nodes": nodes, "edges": []},
    }


def _simple_flow_payload(tenant_id: str, *, name: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "name": name,
        "description": "Live E2E delete flow",
        "trigger_type": "manual",
        "trigger_config": {},
        "status": "draft",
        "is_active": True,
        "version": 1,
        "nodes": {
            "start_node_id": "end",
            "nodes": [{"id": "end", "type": "end", "config": {"message": "Done"}}],
            "edges": [],
        },
    }


def test_lite_tenant_cannot_create_webhook_flows(lite_client):
    tenant = assert_ok(lite_client.get("/tenant"))
    response = lite_client.post(
        "/flows",
        json=_webhook_flow_payload(tenant["id"], name="Lite Webhook Block", webhook_count=1),
    )
    assert response.status_code == 402, response.text
    assert "current plan" in response.json()["detail"].lower()


def test_professional_tenant_is_blocked_when_webhook_count_exceeds_limit(pro_client):
    tenant = assert_ok(pro_client.get("/tenant"))
    response = pro_client.post(
        "/flows",
        json=_webhook_flow_payload(tenant["id"], name="Too Many Webhooks", webhook_count=4),
    )
    assert response.status_code == 402, response.text
    assert "webhook" in response.json()["detail"].lower()


def test_flow_delete_is_hard_delete(pro_client):
    tenant = assert_ok(pro_client.get("/tenant"))
    created = assert_ok(
        pro_client.post(
            "/flows",
            json=_simple_flow_payload(tenant["id"], name="Live E2E Delete Flow"),
        ),
        status_code=201,
    )
    delete_response = pro_client.delete(f"/flows/{created['id']}")
    assert delete_response.status_code == 204, delete_response.text

    get_response = pro_client.get(f"/flows/{created['id']}")
    assert get_response.status_code == 404, get_response.text
