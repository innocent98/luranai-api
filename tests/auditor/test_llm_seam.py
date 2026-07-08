"""Tests for the auditor LLM model seam (app/auditor/llm.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auditor import llm


def test_model_for_task_routes_to_configured_models():
    assert llm.model_for_task("profile_synthesis") == "gpt-4.1-mini"
    assert llm.model_for_task("page_classify") == "gpt-4o-mini"
    assert llm.model_for_task("gap_analysis") == "gpt-4.1-mini"
    assert llm.model_for_task("expected_map") == "gpt-4.1-mini"


def test_model_for_task_unknown_raises():
    with pytest.raises(ValueError):
        llm.model_for_task("nope")


@pytest.mark.asyncio
async def test_complete_json_parses_response_and_uses_task_model():
    fake_msg = MagicMock()
    fake_msg.content = '{"ok": true, "n": 3}'
    fake_choice = MagicMock(message=fake_msg)
    fake_resp = MagicMock(choices=[fake_choice])
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    with (
        patch("app.auditor.llm._client", fake_client),
        patch("app.auditor.llm.openai_llm_rate_limiter.acquire", AsyncMock()),
    ):
        out = await llm.complete_json("page_classify", "sys", "usr")
    assert out == {"ok": True, "n": 3}
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["response_format"] == {"type": "json_object"}
