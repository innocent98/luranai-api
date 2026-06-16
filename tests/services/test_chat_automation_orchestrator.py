"""ChatAutomationOrchestrator tool-building + routing tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models.automation import TenantActionStatus
from app.services.chat_automation_orchestrator import ChatAutomationOrchestrator
from app.services.rag_service import ToolRoutingResult


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


def _active_chat_action(**kwargs):
    action = _action(**kwargs)
    action.status = TenantActionStatus.ACTIVE
    action.available_in_ai_chat = True
    action.name = kwargs.get("name", "Action")
    return action


@pytest.mark.asyncio
async def test_no_actions_skips_llm(monkeypatch):
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [])
    router = AsyncMock(return_value=ToolRoutingResult())
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools", router
    )
    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="hi",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is False
    router.assert_not_awaited()  # cost guard: no LLM call when no actions


@pytest.mark.asyncio
async def test_no_tool_falls_through_to_rag(monkeypatch):
    action = _active_chat_action(
        slug="cancel_order", name="Cancel order", description="Cancel an order",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch, "_can_use_ai_chat_automation", lambda tid: True)
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [action])
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools",
        AsyncMock(return_value=ToolRoutingResult(tool_name=None)),
    )
    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="just browsing, thanks",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is False


@pytest.mark.asyncio
async def test_intent_hit_with_all_args_executes(monkeypatch):
    action = _active_chat_action(
        slug="cancel_order", name="Cancel order", description="Cancel an order",
        url_template="https://api.example.com/orders/{{order_id}}/cancel",
    )
    orch = ChatAutomationOrchestrator(db=AsyncMock())
    monkeypatch.setattr(orch, "_can_use_ai_chat_automation", lambda tid: True)
    monkeypatch.setattr(orch.automation_service, "list_actions", lambda tid: [action])
    monkeypatch.setattr(
        "app.services.chat_automation_orchestrator.RAGService.route_action_via_tools",
        AsyncMock(return_value=ToolRoutingResult(tool_name="cancel_order", arguments={"order_id": "42"})),
    )
    executed = {}

    async def fake_exec(**kwargs):
        executed.update(kwargs["input_payload"])
        return SimpleNamespace(
            status=SimpleNamespace(value="succeeded"), id="e1",
            normalized_output={"data": {"status": "cancelled"}}, response_body=None,
        )

    monkeypatch.setattr(orch.automation_service, "execute_action", AsyncMock(side_effect=fake_exec))
    monkeypatch.setattr(orch.conversation_service, "update_session_meta", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_format_execution_response", AsyncMock(return_value=("Done.", False, None)))

    result = await orch.handle_turn(
        tenant_id="t", session_id="s", user_message="please cancel order 42",
        session_meta={}, conversation_history=[],
    )
    assert result.handled is True
    assert executed.get("order_id") == "42"
