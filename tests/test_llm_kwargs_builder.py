"""Unit tests for the capability-aware OpenAI chat-completion kwargs builder.

Covers the model-family branching introduced for the gpt-6-luna adoption:
reasoning models (o-series, gpt-5/6) reject temperature/top_p/max_tokens and
take max_completion_tokens + reasoning_effort, and function tools additionally
require reasoning_effort="none" on those models in chat.completions. Classic
models (gpt-4o-mini, etc.) keep the old sampling params so an env-only rollback
needs no code change.
"""

from app.services.rag_service import (
    build_chat_completion_kwargs,
    is_reasoning_model,
)

MSGS = [{"role": "user", "content": "hi"}]


class TestIsReasoningModel:
    def test_luna_is_reasoning(self):
        assert is_reasoning_model("gpt-6-luna") is True

    def test_gpt5_is_reasoning(self):
        assert is_reasoning_model("gpt-5.1-sol") is True

    def test_o_series_is_reasoning(self):
        assert is_reasoning_model("o3-mini") is True
        assert is_reasoning_model("o1") is True

    def test_gpt4o_mini_is_not_reasoning(self):
        assert is_reasoning_model("gpt-4o-mini") is False

    def test_gpt41_is_not_reasoning(self):
        assert is_reasoning_model("gpt-4.1-mini") is False

    def test_empty_is_not_reasoning(self):
        assert is_reasoning_model("") is False

    def test_case_insensitive(self):
        assert is_reasoning_model("GPT-6-Luna") is True


class TestClassicModelKwargs:
    def test_keeps_classic_sampling_params(self):
        kw = build_chat_completion_kwargs(
            model="gpt-4o-mini",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            top_p=0.8,
        )
        assert kw["model"] == "gpt-4o-mini"
        assert kw["messages"] is MSGS
        assert kw["max_tokens"] == 800
        assert kw["temperature"] == 0.2
        assert kw["top_p"] == 0.8
        assert "max_completion_tokens" not in kw
        assert "reasoning_effort" not in kw

    def test_response_format_passthrough(self):
        rf = {"type": "json_object"}
        kw = build_chat_completion_kwargs(
            model="gpt-4o-mini",
            messages=MSGS,
            max_tokens=300,
            temperature=0.1,
            response_format=rf,
        )
        assert kw["response_format"] is rf


class TestReasoningModelKwargs:
    def test_drops_classic_params_uses_completion_tokens_and_effort(self):
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            top_p=0.8,
            reasoning_effort="low",
        )
        assert kw["max_completion_tokens"] == 800
        assert kw["reasoning_effort"] == "low"
        assert "max_tokens" not in kw
        assert "temperature" not in kw
        assert "top_p" not in kw

    def test_respects_configured_effort(self):
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            reasoning_effort="medium",
        )
        assert kw["reasoning_effort"] == "medium"

    def test_invalid_effort_falls_back_to_low(self):
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            reasoning_effort="minimal",  # not supported by luna
        )
        assert kw["reasoning_effort"] == "low"

    def test_tools_force_effort_none(self):
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=300,
            temperature=0.1,
            tools=tools,
            tool_choice="auto",
            reasoning_effort="low",  # must be overridden to "none" because tools present
        )
        assert kw["reasoning_effort"] == "none"
        assert kw["tools"] is tools
        assert kw["tool_choice"] == "auto"

    def test_response_format_passthrough(self):
        rf = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            response_format=rf,
            reasoning_effort="low",
        )
        assert kw["response_format"] is rf
        assert kw["reasoning_effort"] == "low"

    def test_no_tools_means_no_tool_keys(self):
        kw = build_chat_completion_kwargs(
            model="gpt-6-luna",
            messages=MSGS,
            max_tokens=800,
            temperature=0.2,
            tools=None,
            tool_choice="none",
        )
        assert "tools" not in kw
        assert "tool_choice" not in kw
