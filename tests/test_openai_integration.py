"""
Integration tests for OpenAI migration.

Tests embedding generation, LLM response generation, rate limiting,
and overall system integration with OpenAI APIs.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock

from app.utils.embedding import embedding_service
from app.services.rag_service import RAGService
from app.utils.rate_limiter import openai_embedding_rate_limiter, openai_llm_rate_limiter
from app.core.config import settings


class TestOpenAIEmbeddings:
    """Test OpenAI embedding generation."""

    @pytest.mark.asyncio
    async def test_embedding_dimension(self):
        """Verify OpenAI embeddings are 1536 dimensions."""
        # Mock OpenAI response
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]

        with patch.object(embedding_service.client.embeddings, "create", return_value=mock_response):
            embedding = await embedding_service.generate_embedding("test query", use_rate_limit=False)

            assert embedding is not None, "Embedding should not be None"
            assert len(embedding) == 1536, f"Expected 1536 dimensions, got {len(embedding)}"
            assert all(isinstance(x, (int, float)) for x in embedding), "All values should be numeric"

    @pytest.mark.asyncio
    async def test_embedding_generation(self):
        """Test basic embedding generation."""
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]

        with patch.object(embedding_service.client.embeddings, "create", return_value=mock_response):
            embedding = await embedding_service.generate_embedding(
                "What are your pricing tiers?",
                use_rate_limit=False
            )

            assert embedding is not None
            assert len(embedding) == 1536

    @pytest.mark.asyncio
    async def test_query_embedding_generation(self):
        """Test query embedding generation."""
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.2] * 1536)]

        with patch.object(embedding_service.client.embeddings, "create", return_value=mock_response):
            embedding = await embedding_service.generate_query_embedding(
                "How do I reset my password?",
                use_rate_limit=False
            )

            assert embedding is not None
            assert len(embedding) == 1536

    @pytest.mark.asyncio
    async def test_batch_embeddings(self):
        """Test batch embedding generation."""
        texts = [
            "What are your features?",
            "How much does it cost?",
            "How do I sign up?"
        ]

        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]

        with patch.object(embedding_service.client.embeddings, "create", return_value=mock_response):
            embeddings = await embedding_service.generate_batch_embeddings(
                texts,
                use_rate_limit=False
            )

            assert len(embeddings) == 3
            assert all(e is not None for e in embeddings)
            assert all(len(e) == 1536 for e in embeddings)

    @pytest.mark.asyncio
    async def test_embedding_error_handling(self):
        """Test embedding generation error handling."""
        with patch.object(
            embedding_service.client.embeddings,
            "create",
            side_effect=Exception("API Error")
        ):
            embedding = await embedding_service.generate_embedding(
                "test",
                use_rate_limit=False
            )

            assert embedding is None, "Should return None on error"


class TestOpenAILLM:
    """Test OpenAI LLM response generation."""

    @pytest.mark.asyncio
    async def test_llm_response_generation(self):
        """Test GPT-4o-mini generates valid responses."""
        rag_service = RAGService()

        # Mock prompt
        mock_prompt = Mock()
        mock_prompt.full_prompt = "Answer this question: What are your pricing tiers?"

        # Mock OpenAI response
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="We have three pricing tiers: Basic, Pro, and Enterprise."))]
        mock_response.usage = Mock(total_tokens=50)

        with patch("app.services.rag_service.AsyncOpenAI") as mock_openai_class:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai_class.return_value = mock_client

            with patch("app.services.rag_service.openai_llm_rate_limiter.acquire", new_callable=AsyncMock):
                response_text, token_count, is_api_error = await rag_service.generate_ai_response(
                    mock_prompt,
                    max_tokens=800
                )

                assert response_text is not None
                assert len(response_text) > 0
                assert token_count == 50
                assert is_api_error is False

    @pytest.mark.asyncio
    async def test_llm_token_counting(self):
        """Test token counting in LLM responses."""
        rag_service = RAGService()

        mock_prompt = Mock()
        mock_prompt.full_prompt = "Test prompt"

        # Mock response with specific token counts
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Test response"))]
        mock_response.usage = Mock(
            total_tokens=100,
            prompt_tokens=50,
            completion_tokens=50
        )

        with patch("app.services.rag_service.AsyncOpenAI") as mock_openai_class:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai_class.return_value = mock_client

            with patch("app.services.rag_service.openai_llm_rate_limiter.acquire", new_callable=AsyncMock):
                _, token_count, _ = await rag_service.generate_ai_response(mock_prompt)

                assert token_count == 100

    @pytest.mark.asyncio
    async def test_llm_error_handling(self):
        """Test LLM error handling."""
        rag_service = RAGService()

        mock_prompt = Mock()
        mock_prompt.full_prompt = "Test prompt"

        with patch("app.services.rag_service.AsyncOpenAI") as mock_openai_class:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Service unavailable"))
            mock_openai_class.return_value = mock_client

            with patch("app.services.rag_service.openai_llm_rate_limiter.acquire", new_callable=AsyncMock):
                response_text, token_count, is_api_error = await rag_service.generate_ai_response(mock_prompt)

                assert is_api_error is True
                assert "trouble processing" in response_text.lower() or "unable" in response_text.lower()

    @pytest.mark.asyncio
    async def test_llm_rate_limit_error(self):
        """Test handling of rate limit errors."""
        rag_service = RAGService()

        mock_prompt = Mock()
        mock_prompt.full_prompt = "Test prompt"

        with patch("app.services.rag_service.AsyncOpenAI") as mock_openai_class:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                side_effect=Exception("Rate limit exceeded")
            )
            mock_openai_class.return_value = mock_client

            with patch("app.services.rag_service.openai_llm_rate_limiter.acquire", new_callable=AsyncMock):
                response_text, token_count, is_api_error = await rag_service.generate_ai_response(mock_prompt)

                assert is_api_error is True
                assert "high demand" in response_text.lower() or "rate limit" in response_text.lower()


class TestRateLimiters:
    """Test rate limiter configuration."""

    def test_embedding_rate_limiter_config(self):
        """Verify embedding rate limiter is configured correctly."""
        assert openai_embedding_rate_limiter.key_prefix == "openai_embedding_api"
        assert openai_embedding_rate_limiter.max_calls == 2700  # 90% of 3K
        assert openai_embedding_rate_limiter.window_seconds == 60

    def test_llm_rate_limiter_config(self):
        """Verify LLM rate limiter is configured correctly."""
        assert openai_llm_rate_limiter.key_prefix == "openai_llm_api"
        assert openai_llm_rate_limiter.max_calls == 450  # 90% of 500
        assert openai_llm_rate_limiter.window_seconds == 60

    @pytest.mark.asyncio
    async def test_rate_limiter_usage_tracking(self):
        """Test rate limiter tracks usage correctly."""
        # Reset rate limiter
        await openai_embedding_rate_limiter.reset("test")

        # Get initial usage
        usage = await openai_embedding_rate_limiter.get_current_usage("test")
        assert usage["calls"] == 0
        assert usage["max_calls"] == 2700

        # Simulate acquiring
        await openai_embedding_rate_limiter.acquire("test")

        # Check usage increased
        usage = await openai_embedding_rate_limiter.get_current_usage("test")
        assert usage["calls"] == 1


class TestConfiguration:
    """Test OpenAI configuration."""

    def test_openai_api_key_configured(self):
        """Verify OpenAI API key is configured."""
        assert hasattr(settings, "OPENAI_API_KEY")
        assert settings.OPENAI_API_KEY is not None

    def test_openai_models_configured(self):
        """Verify OpenAI models are configured."""
        assert hasattr(settings, "OPENAI_EMBEDDING_MODEL")
        assert settings.OPENAI_EMBEDDING_MODEL == "text-embedding-3-small"

        assert hasattr(settings, "OPENAI_LLM_MODEL")
        assert settings.OPENAI_LLM_MODEL == "gpt-4o-mini"

    def test_embedding_dimension_updated(self):
        """Verify embedding dimension is updated to 1536."""
        assert settings.EMBEDDING_DIMENSION == 1536


class TestEndToEnd:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_rag_pipeline(self):
        """Test complete RAG pipeline with OpenAI."""
        # This would be a full integration test with actual API calls
        # Skip in CI/CD unless OpenAI API key is available
        if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.startswith("sk-test"):
            pytest.skip("Requires valid OpenAI API key")

        # Test embedding generation
        query = "What are your pricing tiers?"
        embedding = await embedding_service.generate_query_embedding(query, use_rate_limit=False)

        assert embedding is not None
        assert len(embedding) == 1536

        # Test LLM generation
        rag_service = RAGService()
        mock_prompt = Mock()
        mock_prompt.full_prompt = f"Answer this question: {query}"

        response_text, token_count, is_api_error = await rag_service.generate_ai_response(
            mock_prompt,
            max_tokens=200
        )

        assert response_text is not None
        assert len(response_text) > 0
        assert is_api_error is False
        assert token_count is not None
        assert token_count > 0
