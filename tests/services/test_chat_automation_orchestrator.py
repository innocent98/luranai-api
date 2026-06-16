"""ChatAutomationOrchestrator tool-building + routing tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator


def _action(*, slug, name, description, url_template, body_template=None):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        slug=slug,
        name=name,
        description=description,
        url_template=url_template,
        query_template={},
        body_template=body_template or {},
        input_schema={},
    )


def test_build_action_tools_shapes_openai_tool():
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    action = _action(
        slug="cancel_order",
        name="Cancel order",
        description="Cancel a customer order by order number",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    tools = orch._build_action_tools([action])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert tools[0]["type"] == "function"
    assert fn["name"] == "cancel_order"
    assert "Cancel" in fn["description"]
    assert "order_id" in fn["parameters"]["properties"]


def test_build_action_tools_excludes_secret_fields():
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    action = _action(
        slug="lookup_account",
        name="Lookup account",
        description="Find an account",
        url_template="https://api.example.com/accounts/{{account_id}}",
        body_template={"api_key": "{{secret_api_key}}"},
    )
    tools = orch._build_action_tools([action])
    props = tools[0]["function"]["parameters"]["properties"]
    assert "account_id" in props
    assert "secret_api_key" not in props


def test_build_action_tools_uses_input_schema_and_filters_secrets():
    # When input_schema.properties is present, it drives the tool params, and
    # secret-like keys are filtered out of both properties and required.
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    action = _action(
        slug="cancel_order",
        name="Cancel order",
        description="Cancel an order",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    action.input_schema = {
        "properties": {
            "order_id": {"type": "string", "description": "Order number"},
            "api_token": {"type": "string"},
        },
        "required": ["order_id", "api_token"],
    }
    params = orch._build_action_tools([action])[0]["function"]["parameters"]
    assert "order_id" in params["properties"]
    assert params["properties"]["order_id"]["description"] == "Order number"
    assert "api_token" not in params["properties"]
    assert params["required"] == ["order_id"]
