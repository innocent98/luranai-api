"""OpenAI tool-calling router unit tests (mocked OpenAI client)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.rag_service import RAGService, ToolRoutingResult


def _tool_call_response(name: str, args: dict):
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _no_tool_response():
    message = SimpleNamespace(tool_calls=None, content="Sure, happy to help.")
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.asyncio
async def test_route_returns_tool_and_args(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=_tool_call_response("cancel_order", {"order_id": "42"}))
            )
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(
        "app.services.rag_service.openai_llm_rate_limiter.acquire", AsyncMock()
    )

    result = await service.route_action_via_tools(
        messages=[{"role": "user", "content": "please cancel order 42"}],
        tools=[{"type": "function", "function": {"name": "cancel_order", "parameters": {}}}],
    )
    assert isinstance(result, ToolRoutingResult)
    assert result.tool_name == "cancel_order"
    assert result.arguments == {"order_id": "42"}
    assert result.is_api_error is False


@pytest.mark.asyncio
async def test_route_returns_no_tool(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=_no_tool_response()))
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(
        "app.services.rag_service.openai_llm_rate_limiter.acquire", AsyncMock()
    )

    result = await service.route_action_via_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result.tool_name is None
    assert result.arguments == {}
    assert result.is_api_error is False


@pytest.mark.asyncio
async def test_route_api_error_flagged(monkeypatch):
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("boom")))
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(
        "app.services.rag_service.openai_llm_rate_limiter.acquire", AsyncMock()
    )

    result = await service.route_action_via_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result.is_api_error is True
    assert result.tool_name is None


def _raw_args_response(name: str, raw_arguments: str):
    tool_call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=raw_arguments))
    message = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("raw_arguments", ["not json at all", '["a", "b"]', "null", ""])
@pytest.mark.asyncio
async def test_route_malformed_or_non_dict_args_become_empty(monkeypatch, raw_arguments):
    # Defensive parsing: bad JSON, non-dict JSON, or empty args degrade to {}
    # without raising and without flagging an API error.
    service = RAGService(db=AsyncMock())
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=_raw_args_response("cancel_order", raw_arguments))
            )
        )
    )
    monkeypatch.setattr("app.services.rag_service.openai.AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(
        "app.services.rag_service.openai_llm_rate_limiter.acquire", AsyncMock()
    )

    result = await service.route_action_via_tools(
        messages=[{"role": "user", "content": "cancel it"}],
        tools=[{"type": "function", "function": {"name": "cancel_order", "parameters": {}}}],
    )
    assert result.tool_name == "cancel_order"
    assert result.arguments == {}
    assert result.is_api_error is False
