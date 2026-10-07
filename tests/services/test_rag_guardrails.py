from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.config import settings
from app.services.rag_service import RAGService, is_reasoning_model


def _ai_config(
    allowed_topics: list[str] | None = None,
    blocked_topics: list[str] | None = None,
):
    return SimpleNamespace(
        allowed_topics=allowed_topics,
        blocked_topics=blocked_topics,
    )


class TestScopePolicy:
    def test_allows_personal_creative_for_model_handling(self, db):
        service = RAGService(db)
        allowed, reason = service.enforce_scope_policy(
            user_query="Write a sweet romantic anniversary message for my boyfriend",
            ai_config=_ai_config(),
            retrieved_chunks_count=0,
            avg_similarity=0.0,
        )
        assert allowed is True
        assert reason is None

    def test_blocks_blocked_topic(self, db):
        service = RAGService(db)
        allowed, reason = service.enforce_scope_policy(
            user_query="Can you help me with legal compliance requirements?",
            ai_config=_ai_config(blocked_topics=["legal compliance"]),
            retrieved_chunks_count=3,
            avg_similarity=0.72,
        )
        assert allowed is False
        assert reason == "blocked_topic"

    def test_does_not_hard_block_allowed_topics_without_retrieval_hit(self, db):
        service = RAGService(db)
        allowed, reason = service.enforce_scope_policy(
            user_query="What's the weather in Lagos today?",
            ai_config=_ai_config(allowed_topics=["billing", "pricing", "account"]),
            retrieved_chunks_count=0,
            avg_similarity=0.0,
        )
        assert allowed is True
        assert reason is None

    def test_allows_when_retrieval_hit_even_if_topic_terms_do_not_match(self, db):
        service = RAGService(db)
        allowed, reason = service.enforce_scope_policy(
            user_query="How does your cancellation work for annual plans?",
            ai_config=_ai_config(allowed_topics=["billing", "pricing", "account"]),
            retrieved_chunks_count=2,
            avg_similarity=0.67,
        )
        assert allowed is True
        assert reason is None

    def test_allows_greeting(self, db):
        service = RAGService(db)
        allowed, reason = service.enforce_scope_policy(
            user_query="Hi",
            ai_config=_ai_config(allowed_topics=["billing"]),
            retrieved_chunks_count=0,
            avg_similarity=0.0,
        )
        assert allowed is True
        assert reason is None


class TestRuntimeSystemMessage:
    @pytest.mark.asyncio
    async def test_generate_ai_response_uses_runtime_system_message(self, db):
        service = RAGService(db)
        prompt = Mock()
        prompt.full_prompt = "Question: How do I reset my password?"

        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Use the reset link on login."))]
        mock_response.usage = Mock(total_tokens=42)

        with patch("openai.AsyncOpenAI") as mock_openai_class:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai_class.return_value = mock_client

            with patch(
                "app.services.rag_service.openai_llm_rate_limiter.acquire",
                new_callable=AsyncMock,
            ):
                text, token_count, is_api_error = await service.generate_ai_response(prompt)

        assert is_api_error is False
        assert token_count == 42
        assert text

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"][0]["role"] == "system"
        assert "multi-tenant platform" in call_kwargs["messages"][0]["content"]
        # Sampling params are shaped to the model family (see test_llm_kwargs_builder).
        # Reasoning models (e.g. gpt-6-luna) omit temperature/top_p; classic models keep them.
        if is_reasoning_model(settings.OPENAI_LLM_MODEL):
            assert "temperature" not in call_kwargs
            assert "top_p" not in call_kwargs
            assert "reasoning_effort" in call_kwargs
        else:
            assert call_kwargs["temperature"] == 0.2
            assert call_kwargs["top_p"] == 0.8
