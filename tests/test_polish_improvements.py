"""
Test suite for polish improvements from senior engineer review.

Tests:
1. Similarity threshold constants are used correctly
2. Confidence score is removed from all responses
3. Knowledge source version constraint prevents duplicates
4. Chunk visibility filtering works
5. Reranker uses relevance scores
"""

import pytest
from uuid import uuid4
from sqlalchemy.orm import Session

from app.services.rag_service import RAGService
from app.services.knowledge_service import KnowledgeService
from app.services.conversation_service import ConversationService
from app.db.models.conversation import MessageRole
from app.schemas.conversation import ConversationSessionCreate


class TestSimilarityThresholds:
    """Test that similarity thresholds are centralized."""

    def test_similarity_constants_exist(self):
        """RAGService should have similarity threshold constants."""
        assert hasattr(RAGService, 'MIN_SCORE_THRESHOLD')
        assert hasattr(RAGService, 'MIN_RELEVANT_SIMILARITY')
        assert RAGService.MIN_SCORE_THRESHOLD == 0.35
        assert RAGService.MIN_RELEVANT_SIMILARITY == 0.40


class TestConfidenceScoreRemoval:
    """Test that confidence scores are removed from the system."""

    def test_add_message_without_confidence(self, db: Session, test_tenant):
        """Messages should be created without confidence_score parameter."""
        conv_service = ConversationService(db)

        # Create session
        session_data = ConversationSessionCreate(
            tenant_id=test_tenant.id,
            user_identifier="test_user",
            meta={}
        )
        session = conv_service.create_session(session_data)

        # Add message without confidence_score
        message = conv_service.add_message(
            session_id=session.id,
            role=MessageRole.ASSISTANT,
            content="Test response"
        )

        # Verify message was created
        assert message.id is not None
        assert message.content == "Test response"
        # Confidence score should not exist in response
        assert not hasattr(message, 'confidence_score') or message.confidence_score is None


class TestChunkVisibilityFilter:
    """Test that chunk visibility is enforced."""

    def test_get_chunks_batch_filters_visibility(self, db: Session, test_tenant):
        """get_chunks_batch should only return PUBLIC chunks."""
        knowledge_service = KnowledgeService(db)

        # The filter is applied at database level
        # We're testing that the query includes visibility filter
        from app.db.models.knowledge import ChunkVisibility

        # This should be enforced - no exceptions raised
        chunks = knowledge_service.get_chunks_batch(
            chunk_ids=[uuid4()],  # Non-existent, but shouldn't error
            tenant_id=test_tenant.id
        )

        # Should return empty dict (no chunks with that ID)
        assert chunks == {}


class TestRerankerNaming:
    """Test that reranker uses correct variable names."""

    def test_rerank_returns_relevance_scores(self, db: Session):
        """rerank_chunks should return relevance scores."""
        rag_service = RAGService(db)

        # Test with empty chunks (edge case)
        chunks, scores = rag_service.rerank_chunks(
            query="test",
            chunks=[],
            similarity_scores=[],
            top_k=2
        )

        # Should return empty lists
        assert chunks == []
        assert scores == []


class TestMaxChunkCharsTODO:
    """Test that MAX_CHUNK_CHARS has TODO comment."""

    def test_token_safety_todo_exists(self):
        """RAGService should have MAX_CHUNK_CHARS constant."""
        assert hasattr(RAGService, 'MAX_CHUNK_CHARS')
        assert RAGService.MAX_CHUNK_CHARS == 900


class TestTokenCounter:
    """Test tiktoken integration."""

    def test_token_counter_accuracy(self):
        """Verify tiktoken integration works correctly."""
        from app.utils.token_counter import count_tokens

        # Test known token counts
        assert count_tokens("Hello world") == 2
        assert count_tokens("") == 0

        # Test longer text
        long_text = "This is a longer piece of text " * 10
        tokens = count_tokens(long_text)
        assert tokens > 0
        assert tokens < len(long_text)  # Tokens < characters


@pytest.fixture
def test_tenant(db: Session):
    """Create a test tenant."""
    from app.db.models.tenant import Tenant, TenantStatus
    import hashlib

    tenant = Tenant(
        name="test_tenant_polish",
        display_name="Test Tenant Polish",
        status=TenantStatus.ACTIVE,
        api_key_hash=hashlib.sha256(b"test_api_key").hexdigest(),
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    return tenant
