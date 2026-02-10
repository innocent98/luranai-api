"""Unit tests for intelligent conversational escalation logic."""

from unittest.mock import MagicMock

import pytest

from app.db.models.conversation import EscalationAction
from app.db.models.tenant import TenantAIConfig
from app.services.rag_service import RAGService


@pytest.fixture
def mock_db():
    """Mock database session."""
    return MagicMock()


@pytest.fixture
def rag_service(mock_db):
    """Create RAG service instance."""
    return RAGService(db=mock_db)


@pytest.fixture
def mock_ai_config():
    """Mock tenant AI configuration."""
    return TenantAIConfig(
        escalation_keywords=["urgent", "emergency", "critical"],
        grounding_requirement=True,
        brand_voice={"tone": "friendly", "formality_level": "casual"},
        allowed_topics=[],
        blocked_topics=[],
        custom_instructions=None,
        max_response_tokens=800,
    )


class TestConversationalMode:
    """Test conversational mode for no-knowledge scenarios."""

    def test_no_knowledge_allows_conversation(self, rag_service, mock_ai_config):
        """Test that zero chunks doesn't immediately escalate."""
        decision = rag_service.should_escalate(
            user_query="I need help",
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=[],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "conversational_mode"

    def test_greeting_allows_conversation(self, rag_service, mock_ai_config):
        """Test that greetings allow conversational mode."""
        decision = rag_service.should_escalate(
            user_query="Hello",
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=[],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "conversational_mode"

    def test_generic_help_request_allows_conversation(self, rag_service, mock_ai_config):
        """Test that generic help requests allow conversational mode."""
        decision = rag_service.should_escalate(
            user_query="Can you help me?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=[],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "conversational_mode"

    def test_vague_inquiry_allows_conversation(self, rag_service, mock_ai_config):
        """Test that vague inquiries allow conversational mode."""
        decision = rag_service.should_escalate(
            user_query="I have a question",
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=[],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "conversational_mode"


class TestPartialKnowledgeMode:
    """Test partial knowledge mode for low-similarity scenarios."""

    def test_low_similarity_allows_conversation(self, rag_service, mock_ai_config):
        """Test that low similarity allows partial knowledge mode."""
        decision = rag_service.should_escalate(
            user_query="Tell me about pricing",
            ai_config=mock_ai_config,
            retrieved_chunks_count=3,
            similarity_scores=[0.35, 0.38, 0.32],  # Avg 0.35 < 0.40
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "partial_knowledge_mode"

    def test_borderline_similarity_allows_conversation(self, rag_service, mock_ai_config):
        """Test that borderline similarity allows partial knowledge mode."""
        decision = rag_service.should_escalate(
            user_query="What are your features?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=2,
            similarity_scores=[0.39, 0.38],  # Avg 0.385 < 0.40
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "partial_knowledge_mode"


class TestExplicitEscalation:
    """Test explicit escalation requests."""

    def test_explicit_human_request_escalates(self, rag_service, mock_ai_config):
        """Test that explicit requests still auto-escalate."""
        test_cases = [
            "I want to speak to an agent",
            "Can I talk to a human agent?",
            "Get me a real person",
            "Transfer me to customer service",
            "I need to speak to someone",
        ]
        for query in test_cases:
            decision = rag_service.should_escalate(
                user_query=query,
                ai_config=mock_ai_config,
                retrieved_chunks_count=5,
                similarity_scores=[0.85, 0.90, 0.88, 0.92, 0.87],
            )
            assert decision.action == EscalationAction.AUTO_ESCALATE, f"Failed for query: {query}"
            assert "requested human" in decision.reason.lower(), f"Wrong reason for query: {query}"

    def test_escalation_keywords_still_work(self, rag_service, mock_ai_config):
        """Test that critical escalation keywords still trigger auto-escalation."""
        test_cases = [
            "This is urgent",
            "Emergency situation here",
            "Critical issue with my account",
        ]
        for query in test_cases:
            decision = rag_service.should_escalate(
                user_query=query,
                ai_config=mock_ai_config,
                retrieved_chunks_count=5,
                similarity_scores=[0.85, 0.90, 0.88, 0.92, 0.87],
            )
            assert decision.action == EscalationAction.AUTO_ESCALATE, f"Failed for query: {query}"
            assert "keyword detected" in decision.reason.lower(), f"Wrong reason for query: {query}"


class TestSufficientKnowledge:
    """Test scenarios with sufficient knowledge."""

    def test_good_knowledge_no_escalation(self, rag_service, mock_ai_config):
        """Test that good knowledge leads to no escalation."""
        decision = rag_service.should_escalate(
            user_query="How do I reset my password?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=4,
            similarity_scores=[0.85, 0.78, 0.82, 0.80],  # Avg 0.8125 > 0.40
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "sufficient_knowledge"

    def test_high_similarity_no_escalation(self, rag_service, mock_ai_config):
        """Test that high similarity leads to no escalation."""
        decision = rag_service.should_escalate(
            user_query="What are your pricing plans?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=3,
            similarity_scores=[0.92, 0.95, 0.91],  # Avg 0.926 > 0.40
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "sufficient_knowledge"


class TestSatisfactionDetection:
    """Test satisfaction/closing detection."""

    def test_thank_you_no_escalation(self, rag_service, mock_ai_config):
        """Test that thank you messages don't escalate."""
        test_cases = [
            "Thanks!",
            "Thank you so much",
            "I appreciate your help",
            "That's perfect, thanks",
        ]
        for query in test_cases:
            decision = rag_service.should_escalate(
                user_query=query,
                ai_config=mock_ai_config,
                retrieved_chunks_count=0,
                similarity_scores=[],
            )
            assert decision.action == EscalationAction.NO_ESCALATION, f"Failed for query: {query}"
            assert decision.reason == "satisfaction_detected", f"Wrong reason for query: {query}"
            assert decision.skip_quality_check is True, f"Should skip quality check for: {query}"

    def test_closing_statements_no_escalation(self, rag_service, mock_ai_config):
        """Test that closing statements don't escalate."""
        test_cases = [
            "Got it, thanks",
            "All good",
            "That's all I needed",
            "Okay, bye",
        ]
        for query in test_cases:
            decision = rag_service.should_escalate(
                user_query=query,
                ai_config=mock_ai_config,
                retrieved_chunks_count=0,
                similarity_scores=[],
            )
            assert decision.action == EscalationAction.NO_ESCALATION, f"Failed for query: {query}"
            assert decision.reason == "satisfaction_detected", f"Wrong reason for query: {query}"

    def test_long_satisfaction_message_still_detected(self, rag_service, mock_ai_config):
        """Test that short satisfaction messages are detected even with multiple words."""
        # 10 words or less should be detected
        decision = rag_service.should_escalate(
            user_query="Thank you so much for all your help today",  # 9 words
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=[],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "satisfaction_detected"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_chunks_with_no_scores(self, rag_service, mock_ai_config):
        """Test handling of empty chunks with no scores."""
        decision = rag_service.should_escalate(
            user_query="What is your refund policy?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=0,
            similarity_scores=None,
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "conversational_mode"

    def test_chunks_but_empty_scores_list(self, rag_service, mock_ai_config):
        """Test handling of chunks with empty scores list."""
        decision = rag_service.should_escalate(
            user_query="Tell me about your features",
            ai_config=mock_ai_config,
            retrieved_chunks_count=3,
            similarity_scores=[],
        )
        # Should not crash, should allow conversation
        assert decision.action == EscalationAction.NO_ESCALATION

    def test_exactly_threshold_similarity(self, rag_service, mock_ai_config):
        """Test similarity exactly at threshold (0.40)."""
        decision = rag_service.should_escalate(
            user_query="What integrations do you support?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=2,
            similarity_scores=[0.40, 0.40],  # Avg exactly 0.40
        )
        # At threshold, should be sufficient_knowledge (not partial)
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "sufficient_knowledge"

    def test_one_chunk_low_similarity(self, rag_service, mock_ai_config):
        """Test single chunk with low similarity."""
        decision = rag_service.should_escalate(
            user_query="How do I cancel my subscription?",
            ai_config=mock_ai_config,
            retrieved_chunks_count=1,
            similarity_scores=[0.30],
        )
        assert decision.action == EscalationAction.NO_ESCALATION
        assert decision.reason == "partial_knowledge_mode"
