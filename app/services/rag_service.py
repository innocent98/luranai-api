"""RAG service for vector retrieval and prompt assembly."""

import re
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

    # Similarity thresholds
    MIN_SCORE_THRESHOLD = 0.45  # Vector search cutoff (Qdrant filter)
    MIN_RELEVANT_SIMILARITY = 0.5  # Quality gate for generation

    # Token optimization settings
    MAX_CHUNK_CHARS = 600  # Max characters per knowledge chunk (~150 tokens)
    # TODO: Character-based approximation is NOT token-safe
    #   - Gemini tokens ≠ characters (can vary by 2-3x)
    #   - Multilingual content breaks this assumption (Chinese/Japanese use fewer chars per token)
    #   - For production, use tiktoken or model-specific tokenizer for accurate counts
    #   - Current approach works for English MVP, but log actual token usage for validation

    def normalize_query(self, query: str) -> str:
        """
        Normalize user query before embedding.

        Reduces embedding noise and improves recall.

        Args:
            query: Raw user query

        Returns:
            Normalized query string
        """
        # Basic normalization (Phase 1)
        normalized = query.strip().lower()

        # Optional: Remove excessive whitespace
        normalized = " ".join(normalized.split())

        # Future enhancements (Phase 2):
        # - Remove stopwords (if, and, the, etc.)
        # - Synonym expansion
        # - Spell correction

        return normalized

    def rerank_chunks(
        self,
        query: str,
        chunks: list[KnowledgeChunkResponse],
        similarity_scores: list[float],
        top_k: int = 2,
    ) -> tuple[list[KnowledgeChunkResponse], list[float]]:
        """
        Re-rank retrieved chunks using keyword overlap + similarity.

        Vector search ranks by cosine similarity alone, but semantic
        relevance also depends on keyword presence.

        Args:
            query: Normalized user query
            chunks: Retrieved knowledge chunks
            similarity_scores: Vector similarity scores
            top_k: Number of top chunks to keep (default: 2)

        Returns:
            Tuple of (reranked_chunks, relevance_scores)
        """
        if not chunks:
            return chunks, similarity_scores

        query_words = set(query.lower().split())
        reranked = []

        for chunk, similarity in zip(chunks, similarity_scores):
            # Calculate keyword overlap
            chunk_words = set(chunk.content.lower().split())
            overlap = len(query_words & chunk_words) / len(query_words) if query_words else 0

            # Combined score: 70% similarity + 30% keyword overlap
            combined_score = (0.7 * similarity) + (0.3 * overlap)

            reranked.append({
                "chunk": chunk,
                "similarity": similarity,
                "overlap": overlap,
                "combined_score": combined_score,
            })

        # Sort by combined score
        reranked.sort(key=lambda x: x["combined_score"], reverse=True)

        # Take top K
        top_results = reranked[:top_k]

        # Extract chunks and scores
        final_chunks = [r["chunk"] for r in top_results]
        relevance_scores = [r["combined_score"] for r in top_results]

        logger.debug(
            f"Re-ranked {len(chunks)} chunks → {len(final_chunks)} chunks. "
            f"Top relevance score: {relevance_scores[0]:.3f}" if relevance_scores else "No chunks to re-rank"
        )

        return final_chunks, relevance_scores

    def compress_chunk_context(
        self,
        query: str,
        chunk_content: str,
        max_sentences: int = 5,
    ) -> str:
        """
        Extract most relevant sentences from chunk based on query keywords.

        Uses regex-based sentence splitting to handle edge cases like:
        - Abbreviations (U.S.A., Dr., etc.)
        - Bullet points
        - Numbered lists

        Args:
            query: User query
            chunk_content: Full chunk content
            max_sentences: Maximum sentences to include

        Returns:
            Compressed content with most relevant sentences
        """
        # SAFETY GUARD: Don't compress short chunks (< 3 sentences)
        # Avoids breaking bullet lists, tables, contact info, policies with qualifiers
        sentence_count = len(chunk_content.split(". "))
        if sentence_count < 3:
            # Just use character truncation for short chunks
            if len(chunk_content) > self.MAX_CHUNK_CHARS:
                return chunk_content[:self.MAX_CHUNK_CHARS] + "..."
            return chunk_content

        # Improved sentence splitting using regex
        # Handles: periods, question marks, exclamation marks
        # Avoids splitting on: abbreviations, decimals, initials
        sentence_pattern = r'(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?|\!)\s+'
        sentences = re.split(sentence_pattern, chunk_content.strip())

        # Filter out empty sentences
        sentences = [s.strip() for s in sentences if s.strip()]

        # If regex failed to split properly, fall back to simple split
        if len(sentences) <= 1:
            sentences = chunk_content.split(". ")

        query_words = set(query.lower().split())

        # Score each sentence by keyword overlap
        scored_sentences = []
        for sentence in sentences:
            sentence_words = set(sentence.lower().split())
            overlap = len(query_words & sentence_words)
            scored_sentences.append((sentence, overlap))

        # Sort by overlap (descending)
        scored_sentences.sort(key=lambda x: x[1], reverse=True)

        # Take top sentences
        top_sentences = [s[0] for s in scored_sentences[:max_sentences]]

        # Join back (preserve original sentence endings)
        compressed = ". ".join(top_sentences)
        if not compressed.endswith(('.', '!', '?')):
            compressed += "."

        # Ensure not too long (fallback to character truncation)
        if len(compressed) > self.MAX_CHUNK_CHARS:
            compressed = compressed[:self.MAX_CHUNK_CHARS] + "..."

        return compressed

    # System-level prompt (immutable)
    SYSTEM_PROMPT = """You are Luran — a calm, reliable, and professional customer support assistant.

Your role is to answer customer questions using ONLY the information explicitly provided to you.
You must NEVER guess, infer, assume, or invent details.

━━━━━━━━━━━━━━━━━━━━━━
CORE RULES (NON-NEGOTIABLE)
━━━━━━━━━━━━━━━━━━━━━━
1. Use ONLY the provided information.
2. If the answer is not available, clearly say so.
3. Do NOT hallucinate, speculate, or fill gaps.
4. Do NOT mention sources, documents, or knowledge bases.
5. Do NOT provide information outside allowed topics.
6. Stop immediately after answering. No sign-offs.

━━━━━━━━━━━━━━━━━━━━━━
RESPONSE STYLE
━━━━━━━━━━━━━━━━━━━━━━
• Warm, natural, and human — not robotic
• Clear, concise, and direct
• Professional and calm
• No emotional assumptions

━━━━━━━━━━━━━━━━━━━━━━
WHAT TO INCLUDE (ONLY IF RELEVANT)
━━━━━━━━━━━━━━━━━━━━━━
✓ Direct answer to the question
✓ Clear next steps (if applicable)
✓ Contact details (only when needed)
✓ Offer to connect to a human ONLY when you do not know the answer

━━━━━━━━━━━━━━━━━━━━━━
STRICTLY AVOID
━━━━━━━━━━━━━━━━━━━━━━
✗ Emotional assumptions ("I'm sorry this happened", "That must be stressful")
✗ Filler phrases ("I hope this helps", "Let me know if…")
✗ Closing statements or sign-offs
✗ Repeating information the user didn't ask for
✗ "According to my knowledge base…"
✗ Unnecessary greetings or pleasantries

━━━━━━━━━━━━━━━━━━━━━━
ESCALATION BEHAVIOR
━━━━━━━━━━━━━━━━━━━━━━
• If information is missing or unclear:
  → Say you don't have the information
  → Offer to connect them to a human

• If the user explicitly asks for a human:
  → Acknowledge and comply

━━━━━━━━━━━━━━━━━━━━━━
SPECIAL CASE: THANK YOU
━━━━━━━━━━━━━━━━━━━━━━
ONLY when the user says "thank you" or expresses appreciation,
you may reply with a brief polite response and ONE closing sentence.

Example:
"You're welcome! If you need anything else, I'm here to help."

━━━━━━━━━━━━━━━━━━━━━━
OUTPUT RULE
━━━━━━━━━━━━━━━━━━━━━━
Answer the customer's question.
Stop immediately.
Do not add anything extra."""

    def assemble_prompt(
        self,
        ai_config: TenantAIConfig,
        user_query: str,
        retrieved_chunks: list[KnowledgeChunkResponse],
        conversation_history: list[MessageResponse],
        company_name: str,
        company_description: Optional[str] = None,
        max_history_turns: int = 10,
    ) -> "PromptComponents":
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
            PromptComponents with assembled prompt and metadata
        """
        from app.schemas.rag import PromptComponents

        # Layer 1: System prompt (immutable)
        system_prompt = self.SYSTEM_PROMPT

        # Layer 2: Tenant-specific configuration (inline)
        instructions = []
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
            instructions.append("- Only answer based on the information provided, never guess, hallucinate or make assumptions")

        # Custom instructions
        if ai_config.custom_instructions:
            instructions.append(f"\nAdditional instructions:\n{ai_config.custom_instructions}")

        tenant_instructions = "\n".join(instructions)

        # Layer 3: Knowledge context (inline with truncation for token optimization)
        if not retrieved_chunks:
            knowledge_context = "(No information available)"
        else:
            context_parts = []
            for i, chunk in enumerate(retrieved_chunks, 1):
                # Compress chunk by extracting relevant sentences
                chunk_content = self.compress_chunk_context(
                    query=user_query,
                    chunk_content=chunk.content,
                    max_sentences=5,
                )
                context_parts.append(f"[Source {i}]\n{chunk_content}")
            knowledge_context = "\n\n".join(context_parts)

        # Layer 3: History context (inline)
        if not conversation_history:
            history_context = "No prior conversation history."
        else:
            recent_messages = conversation_history[-(max_history_turns * 2):]
            history_parts = []
            for msg in recent_messages:
                role = msg.role.value.upper()
                history_parts.append(f"{role}: {msg.content}")
            history_context = "\n".join(history_parts)

        # Assemble full prompt
        full_prompt = f"""{system_prompt}

{tenant_instructions}

AVAILABLE INFORMATION:
{knowledge_context}

CONVERSATION HISTORY (may contain incomplete or previous assistant errors - verify against knowledge base):
{history_context}

IMPORTANT: Previous assistant messages may have been incomplete or incorrect. Always prioritize the AVAILABLE INFORMATION section above when answering.

CUSTOMER QUESTION:
{user_query}

Answer the customer's question using ONLY the information provided above. Be natural and professional. Do not add information beyond what is explicitly available."""

        return PromptComponents(
            full_prompt=full_prompt,
            knowledge_chunks_used=len(retrieved_chunks),
            history_turns_used=len(recent_messages) if conversation_history else 0,
        )

    def should_escalate(
        self,
        user_query: str,
        ai_config: TenantAIConfig,
        retrieved_chunks_count: int,
        similarity_scores: Optional[list[float]] = None,
    ) -> "EscalationDecision":
        """
        Determine escalation strategy based on query patterns and knowledge availability.

        Args:
            user_query: User query text
            ai_config: Tenant AI configuration
            retrieved_chunks_count: Number of knowledge chunks retrieved

        Returns:
            EscalationDecision with action, reason, and skip_quality_check flag
        """
        from app.schemas.rag import EscalationDecision
        from app.db.models.conversation import EscalationAction

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
            return EscalationDecision(
                action=EscalationAction.NO_ESCALATION,
                reason="satisfaction_detected",
                skip_quality_check=True,
            )

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
            return EscalationDecision(
                action=EscalationAction.AUTO_ESCALATE,
                reason="User requested human assistance",
            )

        # Scenario 2: Check for critical escalation keywords (auto-escalate)
        for keyword in ai_config.escalation_keywords:
            if keyword.lower() in query_lower:
                logger.warning(f"Critical escalation keyword detected: {keyword}")
                return EscalationDecision(
                    action=EscalationAction.AUTO_ESCALATE,
                    reason=f"Escalation keyword detected: '{keyword}'",
                )

        # Scenario 3: AI has NO knowledge at all - offer escalation
        if retrieved_chunks_count == 0:
            logger.info(f"No knowledge chunks found, will offer escalation")
            return EscalationDecision(
                action=EscalationAction.OFFER_ESCALATION,
                reason="No relevant knowledge available",
            )

        # Scenario 3b: Chunks exist but similarity scores are too low (not actually relevant)
        if similarity_scores and len(similarity_scores) > 0:
            avg_similarity = sum(similarity_scores) / len(similarity_scores)
            # If average similarity is below threshold, the chunks probably aren't relevant
            if avg_similarity < self.MIN_RELEVANT_SIMILARITY:
                logger.warning(f"Low similarity score: {avg_similarity:.2f}, offering escalation")
                return EscalationDecision(
                    action=EscalationAction.OFFER_ESCALATION,
                    reason="Knowledge base has limited information on this topic",
                )

        # Scenario 4: AI has knowledge - let it try to answer
        # Response quality will be evaluated AFTER generation
        logger.info(f"Knowledge available ({retrieved_chunks_count} chunks), AI will attempt to answer")
        return EscalationDecision(
            action=EscalationAction.NO_ESCALATION,
            reason=None,
        )

    def compute_can_answer(
        self,
        retrieved_chunks_count: int,
        avg_similarity: float,
        escalation_action: "EscalationAction",
    ) -> tuple[bool, Optional[str]]:
        """
        Deterministically compute whether AI should attempt to answer.

        Consolidates decision logic from:
        - Chunk availability
        - Similarity quality
        - Escalation intent

        Args:
            retrieved_chunks_count: Number of chunks retrieved
            avg_similarity: Average similarity score
            escalation_action: Escalation decision from should_escalate()

        Returns:
            Tuple of (can_answer: bool, reason: Optional[str])
        """
        from app.db.models.conversation import EscalationAction

        # Case 1: No chunks at all
        if retrieved_chunks_count == 0:
            return False, "no_chunks_retrieved"

        # Case 2: Chunks exist but similarity too low
        if avg_similarity < self.MIN_RELEVANT_SIMILARITY:
            return False, f"low_similarity_{avg_similarity:.2f}"

        # Case 3: User explicitly requested human
        if escalation_action == EscalationAction.AUTO_ESCALATE:
            return False, "user_requested_human"

        # Case 4: All checks passed - safe to answer
        return True, None

    def evaluate_response_quality(self, ai_response: str) -> "ResponseQuality":
        """
        Evaluate if AI response is substantial and helpful.

        Args:
            ai_response: The AI-generated response text

        Returns:
            ResponseQuality with evaluation metrics
        """
        from app.schemas.rag import ResponseQuality

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

        return ResponseQuality(
            is_substantial=is_substantial,
            has_uncertainty=has_uncertainty,
            word_count=word_count,
            char_count=char_count,
        )

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

    # Vector search and AI generation methods

    async def retrieve_relevant_chunks(
        self,
        tenant_id: UUID,
        query: str,
        max_chunks: int = 3,
        score_threshold: float = None,
    ) -> tuple[list[KnowledgeChunkResponse], list[float]]:
        """
        Retrieve relevant knowledge chunks using vector similarity.

        Args:
            tenant_id: Tenant UUID for filtering
            query: User query
            max_chunks: Maximum chunks to retrieve (default: 3, re-ranked to top 2)
            score_threshold: Minimum similarity score (defaults to MIN_SCORE_THRESHOLD)

        Returns:
            Tuple of (retrieved chunks, similarity scores after re-ranking)
        """
        from app.utils.embedding import embedding_service
        from app.utils.qdrant_client import qdrant_manager
        from app.services.knowledge_service import KnowledgeService

        # Use class constant if not provided
        if score_threshold is None:
            score_threshold = self.MIN_SCORE_THRESHOLD

        try:
            # Normalize query before embedding
            normalized_query = self.normalize_query(query)
            logger.debug(f"Normalized query: {query} → {normalized_query}")

            # Generate query embedding
            query_embedding = await embedding_service.generate_query_embedding(normalized_query)

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

            # Batch fetch all chunks in single query (fix N+1 pattern)
            chunk_ids = [UUID(result["chunk_id"]) for result in search_results]
            chunks_dict = knowledge_service.get_chunks_batch(chunk_ids, tenant_id=tenant_id)

            # Preserve order from vector search results
            for result in search_results:
                chunk_id = UUID(result["chunk_id"])
                chunk = chunks_dict.get(chunk_id)
                if chunk:  # is_active already filtered in batch query
                    chunks.append(chunk)
                    scores.append(result["score"])

            # Re-rank using keyword overlap + similarity
            if chunks:
                chunks, relevance_scores = self.rerank_chunks(
                    query=normalized_query,  # Use normalized query
                    chunks=chunks,
                    similarity_scores=scores,
                    top_k=2,  # Keep top 2 after re-ranking
                )
                scores = relevance_scores  # Update scores with reranked values

            return chunks, scores

        except Exception as e:
            logger.error(f"Error retrieving chunks: {e}")
            logger.exception(e)  # Log full traceback for debugging
            return [], []

    async def generate_ai_response(
        self,
        prompt: "PromptComponents",
        max_tokens: int = 200,  # Default for concise, direct responses
        model_name: Optional[str] = None,
    ) -> tuple[str, Optional[int], bool]:
        """
        Generate AI response using Gemini API.

        Args:
            prompt: Assembled prompt components
            max_tokens: Maximum tokens for response
            model_name: Model name to use (defaults to settings.GEMINI_MODEL)

        Returns:
            Tuple of (AI response text, token count, is_api_error)
            - is_api_error=True means technical failure (503, timeout, etc.)
              Don't evaluate quality or offer escalation for API errors
        """
        from google import genai
        from app.core.config import settings
        from loguru import logger
        from app.utils.rate_limiter import (
            gemma_rate_limiter,
            gemma_circuit_breaker,
        )

        # Use provided model or default from settings
        model = model_name or settings.GEMINI_MODEL

        try:
            # Apply rate limiting for gemma-3-12b (30 RPM, 14.4K RPD)
            await gemma_rate_limiter.acquire()
            await gemma_circuit_breaker.check_and_acquire()

            # Create client
            client = genai.Client(api_key=settings.GOOGLE_API_KEY)

            # Generate response
            response = client.models.generate_content(
                model=model,
                contents=prompt.full_prompt,
                config=genai.types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.2,  # Lower for RAG-based support (0.2-0.3 recommended)
                    top_p=0.9,  # Add nucleus sampling for controlled variation
                ),
            )

            # Extract text
            response_text = response.text

            # Extract token count with error handling
            token_count = None
            try:
                if hasattr(response, 'usage_metadata') and response.usage_metadata:
                    token_count = response.usage_metadata.total_token_count
            except Exception as token_error:
                logger.warning(f"Failed to extract token count: {token_error}")

            logger.debug(f"Generated AI response using {model} with {token_count} tokens")
            return response_text, token_count, False  # Success, not an API error

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Error generating AI response with {model}: {e}")

            # Detect specific error types
            if "503" in error_msg or "service unavailable" in error_msg:
                logger.warning("API service temporarily unavailable (503)")
                return "I apologize, but I'm temporarily unable to process requests. Please try again in a moment.", None, True

            elif "blocked" in error_msg or "safety" in error_msg or "content" in error_msg:
                logger.warning("Content blocked by safety filters")
                return "I'm unable to process that request. Would you like me to connect you with someone from our team?", None, True

            elif "rate limit" in error_msg or "quota" in error_msg:
                logger.warning("Rate limit or quota exceeded")
                return "I'm experiencing high demand right now. Please try again in a moment.", None, True

            else:
                # Generic API error
                logger.error(f"Unknown API error: {e}")
                return "I'm having trouble processing your request. Would you like me to connect you with someone from our team?", None, True

    def track_retrieval_metrics(
        self,
        query: str,
        retrieved_chunks_count: int,
        avg_similarity: float,
        response_quality: "ResponseQuality",
        escalated: bool,
        generation_skipped: bool = False,
        skip_reason: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
    ) -> None:
        """
        Track RAG retrieval and response metrics.

        Metrics to monitor:
        - Average similarity score (alert if < 0.5)
        - Retrieval precision (chunks used vs retrieved)
        - Response quality distribution
        - Escalation rate
        - Generation skip rate (safety measure)
        - Prompt token usage

        Args:
            query: User query
            retrieved_chunks_count: Number of chunks retrieved
            avg_similarity: Average similarity score
            response_quality: Response quality evaluation
            escalated: Whether conversation was escalated
            generation_skipped: Whether generation was skipped (NEW)
            skip_reason: Reason for skipping generation (NEW)
            prompt_tokens: Number of prompt tokens used (NEW)
        """
        # Log metrics (can be sent to monitoring service later)
        logger.info(
            f"RAG Metrics - "
            f"chunks={retrieved_chunks_count}, "
            f"avg_similarity={avg_similarity:.3f}, "
            f"substantial={response_quality.is_substantial}, "
            f"uncertain={response_quality.has_uncertainty}, "
            f"escalated={escalated}, "
            f"generation_skipped={generation_skipped}, "
            f"skip_reason={skip_reason}, "
            f"prompt_tokens={prompt_tokens}, "
            f"query_len={len(query)}"
        )

        # NEW: Track retrieval hit rate
        retrieval_hit = retrieved_chunks_count > 0 and avg_similarity >= self.MIN_RELEVANT_SIMILARITY
        logger.info(f"Retrieval hit: {retrieval_hit}")

        # NEW: Track generation skip rate (safety measure)
        if generation_skipped:
            logger.info(f"Generation skipped: {skip_reason}")

        # NEW: Track escalation after answer (confusion indicator)
        if not generation_skipped and escalated:
            logger.warning("Escalation after answer attempt - possible model confusion")

        # Alert on low similarity (potential retrieval issue)
        if avg_similarity < self.MIN_RELEVANT_SIMILARITY and retrieved_chunks_count > 0:
            logger.warning(
                f"Low average similarity score: {avg_similarity:.3f} - "
                f"Knowledge base may not cover this topic well"
            )
