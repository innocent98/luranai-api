"""RAG service for vector retrieval and prompt assembly."""

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.tenant import TenantAIConfig
from app.schemas.knowledge import KnowledgeChunkResponse
from app.schemas.conversation import MessageResponse


class RAGService:
    """
    Service for Retrieval-Augmented Generation.

    Handles vector search, knowledge retrieval, prompt assembly,
    and AI response generation orchestration.
    """

    def __init__(self, db: Session):
        """Initialize RAG service with database session."""
        self.db = db

    # System-level prompt (immutable)
    SYSTEM_PROMPT = """You are a professional customer support representative helping customers with their questions.

CRITICAL RULES:
1. ONLY use the information provided below to answer questions - never invent, guess, or make assumptions
2. Speak exactly like a real human support agent would - be warm, conversational, and natural
3. NEVER use phrases like: "knowledge base", "records", "database", "the information I have", "according to", "provided to me", "in my system", or any other technical/robotic language
4. Do NOT start every message with "Hi there!", "Hello!", "Thanks for reaching out" or similar greetings - go straight to answering the question. Only greet on the very first message of a new conversation if appropriate.
5. When you don't know something, say it naturally like a human would: "I'm not sure about that", "That's not something I can help with right now", "I don't have those details"
6. Always offer helpful alternatives: connect them with a specialist, suggest checking with the team, provide related information you DO know
7. Be concise, direct, friendly, and genuinely helpful - get to the point while maintaining a warm tone"""

    def assemble_prompt(
        self,
        ai_config: TenantAIConfig,
        user_query: str,
        retrieved_chunks: list[KnowledgeChunkResponse],
        conversation_history: list[MessageResponse],
        max_history_turns: int = 10,
    ) -> dict:
        """
        Assemble a complete prompt for the AI model using 3-layer architecture.

        Layer 1: Static system prompt (immutable)
        Layer 2: Dynamic tenant configuration
        Layer 3: User context (history + retrieved knowledge)

        Args:
            ai_config: Tenant AI configuration
            user_query: Current user query
            retrieved_chunks: Retrieved knowledge chunks
            conversation_history: Recent conversation messages
            max_history_turns: Maximum conversation turns to include

        Returns:
            Dict with assembled prompt components
        """
        # Layer 1: System prompt (immutable)
        system_prompt = self.SYSTEM_PROMPT

        # Layer 2: Tenant-specific configuration
        tenant_instructions = self._build_tenant_instructions(ai_config)

        # Layer 3: User context
        knowledge_context = self._build_knowledge_context(retrieved_chunks)
        history_context = self._build_history_context(conversation_history, max_history_turns)

        # Assemble full prompt
        full_prompt = f"""{system_prompt}

{tenant_instructions}

AVAILABLE INFORMATION:
{knowledge_context}

CONVERSATION HISTORY:
{history_context}

CUSTOMER QUESTION:
{user_query}

Answer the customer's question naturally and conversationally, just like a real support agent would. If you don't know the answer, admit it naturally and offer to connect them with someone who can help. Sound human - be warm, helpful, and genuine."""

        return {
            "system_prompt": system_prompt,
            "tenant_instructions": tenant_instructions,
            "knowledge_context": knowledge_context,
            "history_context": history_context,
            "user_query": user_query,
            "full_prompt": full_prompt,
        }

    def _build_tenant_instructions(self, ai_config: TenantAIConfig) -> str:
        """
        Build tenant-specific instructions from AI config.

        Args:
            ai_config: Tenant AI configuration

        Returns:
            Formatted tenant instructions
        """
        instructions = ["TENANT-SPECIFIC INSTRUCTIONS:"]

        # Brand voice
        if ai_config.brand_voice:
            voice = ai_config.brand_voice
            if voice.get("tone"):
                instructions.append(f"- Tone: {voice['tone']}")
            if voice.get("formality_level"):
                instructions.append(f"- Formality: {voice['formality_level']}")
            if voice.get("greeting_style"):
                instructions.append(f"- Greeting style: {voice['greeting_style']}")

        # Topic restrictions
        if ai_config.allowed_topics:
            topics = ", ".join(ai_config.allowed_topics)
            instructions.append(f"- ONLY discuss these topics: {topics}")

        if ai_config.blocked_topics:
            topics = ", ".join(ai_config.blocked_topics)
            instructions.append(f"- NEVER discuss these topics: {topics}")

        # Grounding requirement
        if ai_config.grounding_requirement:
            instructions.append("- Only answer based on the information provided - never guess or make assumptions")

        # Custom instructions
        if ai_config.custom_instructions:
            instructions.append(f"\nAdditional instructions:\n{ai_config.custom_instructions}")

        return "\n".join(instructions)

    def _build_knowledge_context(self, chunks: list[KnowledgeChunkResponse]) -> str:
        """
        Build knowledge context from retrieved chunks.

        Args:
            chunks: Retrieved knowledge chunks

        Returns:
            Formatted knowledge context
        """
        if not chunks:
            return "No relevant knowledge found in the knowledge base."

        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            context_parts.append(f"[Source {i}]\n{chunk.content}")

        return "\n\n".join(context_parts)

    def _build_history_context(
        self, messages: list[MessageResponse], max_turns: int = 10
    ) -> str:
        """
        Build conversation history context.

        Args:
            messages: Conversation messages
            max_turns: Maximum number of turns to include

        Returns:
            Formatted conversation history
        """
        if not messages:
            return "No prior conversation history."

        # Take last N messages (up to max_turns * 2 for user-assistant pairs)
        recent_messages = messages[-(max_turns * 2) :]

        history_parts = []
        for msg in recent_messages:
            role = msg.role.value.upper()
            history_parts.append(f"{role}: {msg.content}")

        return "\n".join(history_parts)

    def calculate_confidence(
        self,
        retrieved_chunks: list[KnowledgeChunkResponse],
        similarity_scores: list[float],
    ) -> float:
        """
        Calculate confidence score based on retrieval quality.

        Args:
            retrieved_chunks: Retrieved chunks
            similarity_scores: Similarity scores from vector search

        Returns:
            Confidence score between 0.0 and 1.0
        """
        if not retrieved_chunks or not similarity_scores:
            return 0.0

        # Simple approach: use average of top scores
        # More sophisticated approaches can be added later
        top_scores = similarity_scores[: min(3, len(similarity_scores))]
        avg_score = sum(top_scores) / len(top_scores)

        return min(1.0, max(0.0, avg_score))

    def should_escalate(
        self,
        user_query: str,
        confidence_score: float,
        ai_config: TenantAIConfig,
    ) -> tuple[bool, Optional[str]]:
        """
        Determine if query should be escalated to humans.

        Args:
            user_query: User query text
            confidence_score: Calculated confidence score
            ai_config: Tenant AI configuration

        Returns:
            Tuple of (should_escalate, escalation_reason)
        """
        # Check confidence threshold
        if confidence_score < ai_config.confidence_threshold:
            return True, f"Low confidence score: {confidence_score:.2f}"

        # Check for escalation keywords
        query_lower = user_query.lower()
        for keyword in ai_config.escalation_keywords:
            if keyword.lower() in query_lower:
                return True, f"Escalation keyword detected: '{keyword}'"

        return False, None

    def format_response(
        self,
        ai_response: str,
        confidence_score: float,
        retrieved_chunk_ids: list[UUID],
        token_count: Optional[int] = None,
    ) -> dict:
        """
        Format AI response with metadata.

        Args:
            ai_response: Generated AI response
            confidence_score: Confidence score
            retrieved_chunk_ids: IDs of chunks used
            token_count: Optional token count

        Returns:
            Formatted response dict
        """
        return {
            "content": ai_response,
            "confidence_score": confidence_score,
            "retrieved_chunk_ids": retrieved_chunk_ids,
            "token_count": token_count,
            "meta": {
                "model_version": "gemini-1.5-pro",  # Will be dynamic later
                "chunks_used": len(retrieved_chunk_ids),
            },
        }

    # Vector search and AI generation methods

    async def retrieve_relevant_chunks(
        self,
        tenant_id: UUID,
        query: str,
        max_chunks: int = 5,
        score_threshold: float = 0.3,
    ) -> tuple[list[KnowledgeChunkResponse], list[float]]:
        """
        Retrieve relevant knowledge chunks using vector similarity.

        Args:
            tenant_id: Tenant UUID for filtering
            query: User query
            max_chunks: Maximum chunks to retrieve
            score_threshold: Minimum similarity score

        Returns:
            Tuple of (retrieved chunks, similarity scores)
        """
        from app.utils.embedding import embedding_service
        from app.utils.qdrant_client import qdrant_manager
        from app.services.knowledge_service import KnowledgeService

        try:
            # Generate query embedding
            query_embedding = await embedding_service.generate_query_embedding(query)

            if not query_embedding:
                return [], []

            # Search Qdrant
            search_results = qdrant_manager.search(
                query_vector=query_embedding,
                tenant_id=tenant_id,
                limit=max_chunks,
                score_threshold=score_threshold,
            )

            if not search_results:
                return [], []

            # Fetch chunk details from database
            knowledge_service = KnowledgeService(self.db)
            chunks = []
            scores = []

            for result in search_results:
                chunk_id = UUID(result["chunk_id"])
                chunk = knowledge_service.get_chunk(chunk_id)

                if chunk and chunk.is_active:
                    chunks.append(chunk)
                    scores.append(result["score"])

            return chunks, scores

        except Exception as e:
            from loguru import logger

            logger.error(f"Error retrieving chunks: {e}")
            return [], []

    async def embed_query(self, query: str) -> list[float]:
        """
        Generate embedding vector for a query.

        Args:
            query: Query text to embed

        Returns:
            Embedding vector
        """
        from app.utils.embedding import embedding_service

        embedding = await embedding_service.generate_query_embedding(query)
        return embedding or []

    async def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding vector for document text.

        Args:
            text: Document text to embed

        Returns:
            Embedding vector
        """
        from app.utils.embedding import embedding_service

        embedding = await embedding_service.generate_embedding(text)
        return embedding or []

    async def generate_ai_response(
        self,
        prompt: dict,
        max_tokens: int = 400,
    ) -> tuple[str, Optional[int]]:
        """
        Generate AI response using Gemini API.

        NOTE: Gemini integration pending. Returns placeholder for now.

        Args:
            prompt: Assembled prompt dictionary
            max_tokens: Maximum tokens for response

        Returns:
            Tuple of (AI response text, token count)
        """
        import google.generativeai as genai
        from app.core.config import settings
        from loguru import logger
        from app.utils.rate_limiter import (
            gemma_rate_limiter,
            gemma_circuit_breaker,
        )

        try:
            # Apply rate limiting for gemma-3-12b (30 RPM, 14.4K RPD)
            await gemma_rate_limiter.acquire()
            await gemma_circuit_breaker.check_and_acquire()

            # Configure Gemini
            genai.configure(api_key=settings.GOOGLE_API_KEY)

            # Create model
            model = genai.GenerativeModel(settings.GEMINI_MODEL)

            # Generate response
            response = model.generate_content(
                prompt["full_prompt"],
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.7,
                ),
            )

            # Extract text and token count
            response_text = response.text
            token_count = response.usage_metadata.total_token_count if hasattr(response, 'usage_metadata') else None

            logger.debug(f"Generated AI response with {token_count} tokens")
            return response_text, token_count

        except Exception as e:
            logger.error(f"Error generating AI response: {e}")
            return "I apologize, but I'm having trouble generating a response. Please try again or contact support.", None
