"""RAG service for vector retrieval and prompt assembly."""

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from loguru import logger

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
    SYSTEM_PROMPT = """You are Luran, a calm and reliable customer support assistant.

CORE BEHAVIOR:
- Answer ONLY with information provided to you - never guess or make assumptions
- Be warm and conversational, but STOP after answering the question
- If you don't have the information, say so naturally and offer to connect them with someone who can help

CRITICAL: Answer the question directly, then STOP. Do not add:
❌ "I'm sorry to hear about..." (no emotional reactions)
❌ "That must be stressful..." (no assumptions about feelings)
❌ "I hope this helps!" (no filler phrases)
❌ "According to my knowledge base..." (no robotic language)
❌ "Thanks for reaching out!" (skip greetings unless it's the first message)

SPECIAL CASE - When user says "thank you" or shows satisfaction:
✅ Acknowledge warmly, then add: "If you need anything else, I'm here to help!"

EXAMPLES:

Regular Question:
User: "If my car catches fire, am I covered?"
You: "Yes, comprehensive coverage includes fire damage. You can reach Claims at 1-800-123-4567 or claims@company.com."

Thank You Message:
User: "Thank you for the assistance"
You: "You're very welcome! If you need anything else, I'm here to help."

Answer the question. Then stop. That's it."""

    def assemble_prompt(
        self,
        ai_config: TenantAIConfig,
        user_query: str,
        retrieved_chunks: list[KnowledgeChunkResponse],
        conversation_history: list[MessageResponse],
        company_name: str,
        company_description: Optional[str] = None,
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
            company_name: Company display name
            company_description: Company description (optional)
            max_history_turns: Maximum conversation turns to include

        Returns:
            Dict with assembled prompt components
        """
        # Layer 1: System prompt (immutable)
        system_prompt = self.SYSTEM_PROMPT

        # Layer 2: Tenant-specific configuration
        tenant_instructions = self._build_tenant_instructions(
            ai_config, company_name, company_description
        )

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

    def _build_tenant_instructions(
        self,
        ai_config: TenantAIConfig,
        company_name: str,
        company_description: Optional[str] = None,
    ) -> str:
        """
        Build tenant-specific instructions from AI config.

        Args:
            ai_config: Tenant AI configuration
            company_name: Company display name
            company_description: Company description (optional)

        Returns:
            Formatted tenant instructions
        """
        instructions = []

        # Company context
        instructions.append(f"You are currently assisting users for: {company_name}")
        if company_description:
            instructions.append(f"\nCompany description:\n{company_description}")

        instructions.append("\nSUPPORT CONFIGURATION:")

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
        ai_config: TenantAIConfig,
        retrieved_chunks_count: int,
    ) -> tuple[str, Optional[str]]:
        """
        Determine escalation strategy based on query patterns and knowledge availability.

        Args:
            user_query: User query text
            ai_config: Tenant AI configuration
            retrieved_chunks_count: Number of knowledge chunks retrieved

        Returns:
            Tuple of (escalation_action, escalation_reason)

            escalation_action can be:
            - "auto_escalate": Immediately escalate (user requested human)
            - "offer_escalation": Offer to escalate, wait for consent
            - "no_escalation": Don't escalate, AI will attempt to answer

            escalation_reason can be:
            - "satisfaction_detected": User is closing/thanking (don't check response quality)
            - None: Normal flow (check response quality after generation)
            - Other strings: Specific escalation reasons
        """
        query_lower = user_query.lower().strip()

        # Patterns that indicate user satisfaction/closing (should NOT escalate)
        satisfaction_patterns = [
            "thank", "thanks", "appreciate", "grateful",
            "that's all", "all set", "all good", "perfect", "great",
            "got it", "understood", "makes sense",
            "okay", "ok", "alright", "cool", "awesome", "nice",
            "bye", "goodbye", "see you", "talk later", "have a good",
        ]

        # Check if this is a satisfaction/closing statement
        is_satisfaction = any(pattern in query_lower for pattern in satisfaction_patterns)
        if is_satisfaction and len(query_lower.split()) <= 10:
            logger.info(f"Detected satisfaction/closing statement, no escalation")
            return "no_escalation", "satisfaction_detected"

        # Scenario 1: User explicitly requests human agent
        human_request_patterns = [
            "speak to agent", "speak with agent", "talk to agent", "talk with agent",
            "human agent", "real person", "actual person",
            "speak to someone", "talk to someone",
            "escalate", "transfer me", "get me someone",
            "customer service", "support team",
        ]

        if any(pattern in query_lower for pattern in human_request_patterns):
            logger.info(f"User explicitly requested human agent")
            return "auto_escalate", "User requested human assistance"

        # Scenario 2: Check for critical escalation keywords (auto-escalate)
        for keyword in ai_config.escalation_keywords:
            if keyword.lower() in query_lower:
                logger.warning(f"Critical escalation keyword detected: {keyword}")
                return "auto_escalate", f"Escalation keyword detected: '{keyword}'"

        # Scenario 3: AI has NO knowledge at all - offer escalation
        if retrieved_chunks_count == 0:
            logger.info(f"No knowledge chunks found, will offer escalation")
            return "offer_escalation", "No relevant knowledge available"

        # Scenario 4: AI has knowledge - let it try to answer
        # Response quality will be evaluated AFTER generation
        logger.info(f"Knowledge available ({retrieved_chunks_count} chunks), AI will attempt to answer")
        return "no_escalation", None

    def evaluate_response_quality(self, ai_response: str) -> dict:
        """
        Evaluate if AI response is substantial and helpful.

        Args:
            ai_response: The AI-generated response text

        Returns:
            Dict with:
            - is_substantial: bool (response is good enough)
            - has_uncertainty: bool (AI expressed uncertainty)
            - word_count: int
            - char_count: int
        """
        word_count = len(ai_response.split())
        char_count = len(ai_response)

        # Check for uncertainty markers
        uncertainty_patterns = [
            "i'm not sure", "i don't know", "not certain", "unclear",
            "i don't have", "i'm not aware", "i can't find",
            "i'm unable to", "no information about", "don't have information",
            "i apologize", "unfortunately, i don't",
        ]
        has_uncertainty = any(pattern in ai_response.lower() for pattern in uncertainty_patterns)

        # Substantial = more than 100 chars OR more than 15 words
        # AND no strong uncertainty markers
        is_substantial = (char_count > 100 or word_count > 15) and not has_uncertainty

        logger.debug(
            f"Response quality: {word_count} words, {char_count} chars, "
            f"substantial={is_substantial}, uncertain={has_uncertainty}"
        )

        return {
            "is_substantial": is_substantial,
            "has_uncertainty": has_uncertainty,
            "word_count": word_count,
            "char_count": char_count,
        }

    def detect_escalation_consent(self, user_message: str) -> Optional[str]:
        """
        Detect if user is giving consent or rejecting escalation offer.

        Args:
            user_message: User's message text

        Returns:
            "consent" if user agrees to escalation
            "reject" if user declines escalation
            None if unclear/unrelated
        """
        message_lower = user_message.lower().strip()

        # Consent patterns
        consent_patterns = [
            "yes", "yeah", "yep", "sure", "ok", "okay", "please",
            "go ahead", "connect me", "transfer", "escalate",
            "i would", "i'd like", "that would be great",
            "yes please", "sure thing", "sounds good",
        ]

        # Rejection patterns
        rejection_patterns = [
            "no", "nope", "nah", "never mind", "nevermind",
            "i'm good", "im good", "i'm ok", "im ok",
            "no thanks", "no thank", "that's fine", "thats fine",
            "all good", "all set", "don't need", "dont need",
        ]

        # Check for consent (must be short message, < 15 words)
        if len(message_lower.split()) <= 15:
            if any(pattern in message_lower for pattern in consent_patterns):
                logger.info(f"Detected escalation consent")
                return "consent"

            if any(pattern in message_lower for pattern in rejection_patterns):
                logger.info(f"Detected escalation rejection")
                return "reject"

        # Unclear or unrelated message
        return None

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
        from google import genai
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

            # Create client
            client = genai.Client(api_key=settings.GOOGLE_API_KEY)

            # Generate response
            response = client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt["full_prompt"],
                config=genai.types.GenerateContentConfig(
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
