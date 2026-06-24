"""TDD tests for generate_sales_response in RAGService (Task 3)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.rag_service import RAGService


def _fake_prompt():
    """Minimal PromptComponents stand-in with a full_prompt attribute."""
    return SimpleNamespace(full_prompt="What are your pricing options?")


def _fake_config():
    """Minimal TenantSalesAgentConfig-shaped object with qualification_questions."""
    return SimpleNamespace(qualification_questions=[{"question": "How large is your team?"}])


@pytest.mark.asyncio
async def test_sales_generation_parses_structured_output():
    svc = RAGService(db=AsyncMock())
    raw = (
        '{"reply":"Our Business plan scales with team size. How large is your team?",'
        '"intent":"PRICING_INQUIRY",'
        '"signals":{"asked_features":false,"requested_demo":false,'
        '"mentioned_competitor":false,"ready_to_buy":false},'
        '"captured":{"name":null,"email":null,"company":null,'
        '"team_size":null,"industry":null}}'
    )
    with patch.object(svc, "_call_openai_raw", AsyncMock(return_value=(raw, 42, False))):
        reply, meta, tokens, err = await svc.generate_sales_response(
            _fake_prompt(), _fake_config()
        )
    assert "team" in reply.lower()
    assert meta is not None
    assert meta["intent"] == "PRICING_INQUIRY"
    assert tokens == 42
    assert err is False


@pytest.mark.asyncio
async def test_sales_generation_falls_back_on_malformed():
    svc = RAGService(db=AsyncMock())
    with patch.object(svc, "_call_openai_raw", AsyncMock(return_value=("not json", 5, False))):
        reply, meta, tokens, err = await svc.generate_sales_response(
            _fake_prompt(), _fake_config()
        )
    assert reply == "not json"
    assert meta is None
    assert tokens == 5
    assert err is False


@pytest.mark.asyncio
async def test_sales_generation_api_error_returns_fallback_text():
    svc = RAGService(db=AsyncMock())
    fallback = "I'm having trouble processing your request."
    with patch.object(
        svc, "_call_openai_raw", AsyncMock(return_value=(fallback, None, True))
    ):
        reply, meta, tokens, err = await svc.generate_sales_response(
            _fake_prompt(), _fake_config()
        )
    assert reply == fallback
    assert meta is None
    assert tokens is None
    assert err is True


@pytest.mark.asyncio
async def test_sales_generation_meta_has_signals_and_captured():
    svc = RAGService(db=AsyncMock())
    raw = (
        '{"reply":"Great, let me help you with that.",'
        '"intent":"DEMO_REQUEST",'
        '"signals":{"asked_features":true,"requested_demo":true,'
        '"mentioned_competitor":false,"ready_to_buy":false},'
        '"captured":{"name":"Alice","email":"alice@example.com","company":"Acme",'
        '"team_size":"50","industry":"SaaS"}}'
    )
    with patch.object(svc, "_call_openai_raw", AsyncMock(return_value=(raw, 10, False))):
        reply, meta, tokens, err = await svc.generate_sales_response(
            _fake_prompt(), _fake_config()
        )
    assert meta is not None
    assert meta["signals"]["asked_features"] is True
    assert meta["captured"]["name"] == "Alice"
    assert meta["intent"] == "DEMO_REQUEST"
