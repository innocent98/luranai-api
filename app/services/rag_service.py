"""RAG service for vector retrieval and prompt assembly."""

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import openai
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.tenant import TenantAIConfig
from app.schemas.conversation import MessageResponse
from app.schemas.knowledge import KnowledgeChunkResponse
from app.utils.rate_limiter import openai_llm_rate_limiter
from app.utils.token_counter import count_tokens

if TYPE_CHECKING:
    from app.db.models.conversation import EscalationAction
    from app.schemas.rag import EscalationDecision, PromptComponents, ResponseQuality


@dataclass
class ToolRoutingResult:
    """Result of asking the LLM which tenant action (if any) to invoke."""

    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    is_api_error: bool = False


class RAGService:
    """
    Service for Retrieval-Augmented Generation.

    Handles vector search, knowledge retrieval, prompt assembly,
    and AI response generation orchestration.
    """

    def __init__(self, db: Session):
        """Initialize RAG service with database session."""
        self.db = db
        extra_stopwords = {
            token.strip().lower()
            for token in settings.RAG_EXTRA_STOPWORDS.split(",")
            if token.strip()
        }
        self.stopwords = self.STOPWORDS | extra_stopwords

    # Similarity thresholds
    MIN_SCORE_THRESHOLD = 0.35  # Vector search cutoff - Lowered from 0.40 for better recall
    MIN_RELEVANT_SIMILARITY = 0.40  # Quality gate - Balanced (was 0.42)

    # Token optimization settings
    MAX_CHUNK_CHARS = 750  # Max characters per knowledge chunk (~190 tokens)

    # Chunk retrieval configuration
    MAX_CHUNKS_RETRIEVE = 6  # Retrieve from Qdrant, then rerank
    MAX_CHUNKS_KEEP = 4  # Keep after reranking
    KNOWLEDGE_CONTEXT_TOKEN_BUDGET = 700  # Max KB tokens to inject in final prompt
    MAX_HISTORY_TOKENS = 420  # Max conversation-history tokens to inject
    MAX_HISTORY_MESSAGE_CHARS = 320  # Trim verbose history lines
    STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "who",
        "why",
        "with",
        "you",
        "your",
    }

    RUNTIME_SYSTEM_MESSAGE = """You are the AI support assistant operating on behalf of a tenant company in a multi-tenant platform.

Role and scope:
- Support end-users for the active tenant's business context.
- Follow tenant configuration and instructions in the prompt (voice, allowed topics, blocked topics, escalation rules).
- Decline requests that are outside the tenant's support scope and redirect to relevant in-scope help.
- Do not produce personal creative-writing content unrelated to tenant support workflows.

Grounding and safety:
- Use provided context for factual claims.
- Do not invent policies, pricing, timelines, account details, or commitments.
- If information is missing, ask a concise clarifying question or offer human escalation.

Style:
- Clear, concise, professional, and natural.
- Do not use emojis.
- Avoid fluff, roleplay, and emotional storytelling.
"""

    def _extract_keywords(self, text: str) -> set[str]:
        """Extract normalized keywords for lightweight overlap scoring."""
        return {
            token
            for token in re.findall(r"[a-z0-9]+", text.lower())
            if len(token) > 1 and token not in self.stopwords
        }

    def _topic_match_score(self, text: str, topic: str) -> float:
        """Compute a lightweight match score between query text and a configured topic."""
        text_lower = text.lower()
        topic_lower = topic.lower().strip()
        if not topic_lower:
            return 0.0
        if topic_lower in text_lower:
            return 1.0

        query_tokens = self._extract_keywords(text_lower)
        topic_tokens = self._extract_keywords(topic_lower)
        if not query_tokens or not topic_tokens:
            return 0.0

        overlap = len(query_tokens & topic_tokens)
        if len(topic_tokens) == 1:
            return 1.0 if overlap == 1 else 0.0
        return overlap / len(topic_tokens)

    def _matches_any_topic(self, text: str, topics: list[str] | None) -> bool:
        """Return True if text matches any configured topic."""
        if not topics:
            return False
        for topic in topics:
            score = self._topic_match_score(text, topic)
            if score >= 0.6:
                return True
        return False

    def enforce_scope_policy(
        self,
        user_query: str,
        ai_config: TenantAIConfig,
        retrieved_chunks_count: int,
        avg_similarity: float,
    ) -> tuple[bool, str | None]:
        """
        Deterministically enforce support scope and tenant topic controls.

        Returns:
            tuple[is_allowed, reason]
        """
        if ai_config.blocked_topics and self._matches_any_topic(
            user_query, cast(list[str], ai_config.blocked_topics)
        ):
            return False, "blocked_topic"

        # allowed_topics and broader scope handling are enforced via model instructions
        # so tenant-specific behavior can remain adaptive instead of regex-pattern based.
        return True, None

    def should_force_clarifying_followup(
        self,
        user_query: str,
        ai_response: str,
        retrieved_chunks_count: int,
        avg_similarity: float,
    ) -> bool:
        """
        Guardrail: for policy/pricing-sensitive questions with weak retrieval,
        ask a clarifying question instead of giving a potentially wrong final answer.
        """
        pricing_terms = {
            "price",
            "pricing",
            "cost",
            "fee",
            "refund",
            "billing",
            "subscription",
            "cancel",
            "cancellation",
        }
        policy_terms = {"policy", "terms"}
        query_tokens = self._extract_keywords(user_query)
        # Scope this guard to query intent only.
        # Using response tokens can cause false positives when answers mention "policy".
        is_pricing_like = bool(pricing_terms & query_tokens)
        is_policy_like = bool(policy_terms & query_tokens)
        has_pricing_context = is_pricing_like or (
            is_policy_like
            and bool({"billing", "refund", "cancel", "price", "subscription"} & query_tokens)
        )
        weak_coverage = retrieved_chunks_count < 2 or avg_similarity < 0.45
        return has_pricing_context and weak_coverage

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
        top_k: int = 4,
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

        query_words = self._extract_keywords(query)
        reranked: list[dict[str, Any]] = []

        for chunk, similarity in zip(chunks, similarity_scores, strict=False):
            # Calculate keyword overlap
            chunk_words = self._extract_keywords(chunk.content)
            overlap = len(query_words & chunk_words) / len(query_words) if query_words else 0

            # Combined score: 70% similarity + 30% keyword overlap
            combined_score = (0.7 * similarity) + (0.3 * overlap)

            reranked.append(
                {
                    "chunk": chunk,
                    "similarity": similarity,
                    "overlap": overlap,
                    "combined_score": combined_score,
                }
            )

        # Sort by combined score
        reranked.sort(key=lambda x: float(x["combined_score"]), reverse=True)

        # Take top K
        top_results = reranked[:top_k]

        # Extract chunks and scores
        final_chunks = [cast(KnowledgeChunkResponse, r["chunk"]) for r in top_results]
        relevance_scores = [float(r["combined_score"]) for r in top_results]

        logger.debug(
            f"Re-ranked {len(chunks)} chunks → {len(final_chunks)} chunks. "
            f"Top relevance score: {relevance_scores[0]:.3f}"
            if relevance_scores
            else "No chunks to re-rank"
        )

        return final_chunks, relevance_scores

    def compress_chunk_context(
        self,
        query: str,
        chunk_content: str,
        max_sentences: int = 10,
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
            # Token-aware truncation for short chunks
            chunk_tokens = count_tokens(chunk_content)
            if chunk_tokens > 225:  # ~900 chars
                # Truncate by characters as fallback
                if len(chunk_content) > self.MAX_CHUNK_CHARS:
                    return chunk_content[: self.MAX_CHUNK_CHARS] + "..."
            return chunk_content

        # Improved sentence splitting using regex
        # Handles: periods, question marks, exclamation marks
        # Avoids splitting on: abbreviations, decimals, initials
        sentence_pattern = r"(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?|\!)\s+"
        sentences = re.split(sentence_pattern, chunk_content.strip())

        # Filter out empty sentences
        sentences = [s.strip() for s in sentences if s.strip()]

        # If regex failed to split properly, fall back to simple split
        if len(sentences) <= 1:
            sentences = chunk_content.split(". ")

        query_words = self._extract_keywords(query)
        query_lower = query.lower()
        is_timeline_query = any(
            marker in query_lower
            for marker in (
                "how long",
                "timeline",
                "turnaround",
                "processing time",
                "processing take",
                "duration",
            )
        )
        time_pattern = re.compile(
            r"(\b\d+\s*-\s*\d+\b|\b\d+\b|\bhour[s]?\b|\bday[s]?\b|\bweek[s]?\b|\bbusiness\b)",
            re.IGNORECASE,
        )

        # Score each sentence by keyword overlap
        scored_sentences: list[tuple[str, float]] = []
        for sentence in sentences:
            sentence_words = self._extract_keywords(sentence)
            score = float(len(query_words & sentence_words))

            # For timeline questions, prioritize numeric/time-bearing sentences.
            if is_timeline_query and time_pattern.search(sentence):
                score += 3.0

            # Light domain boost for claim timelines.
            if "claim" in query_words and "claim" in sentence_words:
                score += 1.0

            scored_sentences.append((sentence, score))

        # Sort by overlap (descending)
        scored_sentences.sort(key=lambda x: x[1], reverse=True)

        # Take top sentences
        top_sentences = [s[0] for s in scored_sentences[:max_sentences]]

        # Join back (preserve original sentence endings)
        compressed = ". ".join(top_sentences)
        if not compressed.endswith((".", "!", "?")):
            compressed += "."

        # Token-aware truncation
        chunk_tokens = count_tokens(compressed)
        if chunk_tokens > 225:  # ~900 chars
            # Fallback to character truncation
            if len(compressed) > self.MAX_CHUNK_CHARS:
                compressed = compressed[: self.MAX_CHUNK_CHARS] + "..."

        return compressed

    # Compact context envelope (sent as user content). Runtime policy lives in RUNTIME_SYSTEM_MESSAGE.
    INTELLIGENT_SYSTEM_PROMPT = """Tenant:
{company_name}

Tenant configuration:
{tenant_config}

Retrieved knowledge:
{knowledge_context}

Conversation history:
{history_context}

User message:
{user_query}

Instructions:
- Answer using tenant knowledge and prior conversation context when relevant.
- Keep the response concise and directly useful.
- For follow-up questions, continue from prior turns before asking for clarification.
- If required details are still missing, ask one clarifying question or offer human support.
"""

    def assemble_prompt(
        self,
        ai_config: TenantAIConfig,
        user_query: str,
        retrieved_chunks: list[KnowledgeChunkResponse],
        conversation_history: list[MessageResponse],
        company_name: str,
        company_description: str | None = None,
        max_history_turns: int = 10,
    ) -> "PromptComponents":
        """
        Assemble intelligent adaptive prompt with knowledge availability signaling.

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

        # Build tenant configuration string
        tenant_config_parts = []
        tenant_config_parts.append(f"Company: {company_name}")
        if company_description:
            tenant_config_parts.append(f"Description: {company_description}")

        if ai_config.brand_voice:
            voice = ai_config.brand_voice
            if voice.get("tone"):
                tenant_config_parts.append(f"Tone: {voice['tone']}")
            if voice.get("formality_level"):
                tenant_config_parts.append(f"Formality: {voice['formality_level']}")

        if ai_config.allowed_topics:
            topics = ", ".join(ai_config.allowed_topics)
            tenant_config_parts.append(f"ONLY discuss: {topics}")

        if ai_config.blocked_topics:
            topics = ", ".join(ai_config.blocked_topics)
            tenant_config_parts.append(f"NEVER discuss: {topics}")

        if ai_config.grounding_requirement:
            tenant_config_parts.append(
                "Strict grounding: Only answer from provided information for factual questions"
            )
        tenant_config_parts.append(
            f"Grounding mode: {getattr(ai_config, 'grounding_mode', 'balanced')}"
        )
        tenant_config_parts.append(
            f"Low-confidence action: {getattr(ai_config, 'low_confidence_action', 'clarify')}"
        )
        tenant_config_parts.append(
            f"No-retrieval action: {getattr(ai_config, 'no_retrieval_action', 'clarify')}"
        )

        if ai_config.custom_instructions:
            tenant_config_parts.append(f"Additional: {ai_config.custom_instructions}")

        tenant_config = "\n".join(tenant_config_parts)

        # Knowledge context
        if not retrieved_chunks:
            knowledge_context = "(No retrieved knowledge for this query.)"
        else:
            context_parts: list[str] = []
            context_tokens = 0
            source_number = 1
            for chunk in retrieved_chunks:
                chunk_content = self.compress_chunk_context(
                    query=user_query,
                    chunk_content=chunk.content,
                    max_sentences=6,
                )
                candidate = f"[Source {source_number}]\n{chunk_content}"
                candidate_tokens = count_tokens(candidate)
                # Always keep at least the top chunk, then enforce KB context budget.
                if (
                    context_parts
                    and context_tokens + candidate_tokens > self.KNOWLEDGE_CONTEXT_TOKEN_BUDGET
                ):
                    break
                context_parts.append(candidate)
                context_tokens += candidate_tokens
                source_number += 1
            knowledge_context = "\n\n".join(context_parts)

        # Conversation history
        recent_messages: list[MessageResponse] = []
        if not conversation_history:
            history_context = "(This is the first message in the conversation)"
        else:
            recent_messages = conversation_history[-(max_history_turns * 2) :]
            history_parts_reversed: list[str] = []
            history_tokens = 0
            for msg in reversed(recent_messages):
                role = msg.role.value.upper()
                msg_content = (msg.content or "").strip()
                if len(msg_content) > self.MAX_HISTORY_MESSAGE_CHARS:
                    msg_content = msg_content[: self.MAX_HISTORY_MESSAGE_CHARS] + "..."
                line = f"{role}: {msg_content}"
                line_tokens = count_tokens(line)
                # Always keep most recent line, then enforce history budget.
                if (
                    history_parts_reversed
                    and history_tokens + line_tokens > self.MAX_HISTORY_TOKENS
                ):
                    break
                history_parts_reversed.append(line)
                history_tokens += line_tokens
            history_context = "\n".join(reversed(history_parts_reversed))

        # Assemble full prompt using new intelligent template
        full_prompt = self.INTELLIGENT_SYSTEM_PROMPT.format(
            company_name=company_name,
            tenant_config=tenant_config,
            knowledge_context=knowledge_context,
            history_context=history_context,
            user_query=user_query,
        )

        # Calculate token counts
        system_tokens = count_tokens(self.INTELLIGENT_SYSTEM_PROMPT)
        tenant_tokens = count_tokens(tenant_config)
        knowledge_tokens = count_tokens(knowledge_context)
        history_tokens = count_tokens(history_context)
        query_tokens = count_tokens(user_query)

        total_prompt_tokens = (
            system_tokens + tenant_tokens + knowledge_tokens + history_tokens + query_tokens
        )

        logger.debug(
            f"Prompt token breakdown: "
            f"system={system_tokens}, tenant={tenant_tokens}, "
            f"knowledge={knowledge_tokens}, history={history_tokens}, "
            f"query={query_tokens}, total={total_prompt_tokens}"
        )

        return PromptComponents(
            full_prompt=full_prompt,
            knowledge_chunks_used=len(retrieved_chunks),
            history_turns_used=len(recent_messages) if conversation_history else 0,
            prompt_tokens=total_prompt_tokens,
        )

    def should_escalate(
        self,
        user_query: str,
        ai_config: TenantAIConfig,
        retrieved_chunks_count: int,
        similarity_scores: list[float] | None = None,
    ) -> "EscalationDecision":
        """
        Determine escalation strategy based on query patterns and knowledge availability.

        NEW BEHAVIOR: Allows AI to handle scenarios conversationally before escalating.

        Args:
            user_query: User query text
            ai_config: Tenant AI configuration
            retrieved_chunks_count: Number of knowledge chunks retrieved
            similarity_scores: Similarity scores for retrieved chunks

        Returns:
            EscalationDecision with action, reason, and skip_quality_check flag
        """
        from app.db.models.conversation import EscalationAction
        from app.schemas.rag import EscalationDecision

        query_lower = user_query.lower().strip()

        # Scenario 1: User satisfaction/closing (NO ESCALATION)
        satisfaction_patterns = [
            "thank",
            "thanks",
            "appreciate",
            "grateful",
            "that's all",
            "all set",
            "all good",
            "perfect",
            "great",
            "got it",
            "understood",
            "makes sense",
            "okay",
            "ok",
            "alright",
            "cool",
            "awesome",
            "nice",
            "bye",
            "goodbye",
            "see you",
            "talk later",
            "have a good",
        ]
        is_satisfaction = any(pattern in query_lower for pattern in satisfaction_patterns)
        if is_satisfaction and len(query_lower.split()) <= 10:
            logger.info("Detected satisfaction/closing statement, no escalation")
            return EscalationDecision(
                action=EscalationAction.NO_ESCALATION,
                reason="satisfaction_detected",
                skip_quality_check=True,
            )

        # Scenario 2: User explicitly requests human agent (AUTO ESCALATE)
        human_request_patterns = [
            "speak to agent",
            "speak to an agent",
            "speak to a agent",
            "speak with agent",
            "speak with an agent",
            "speak with a agent",
            "talk to agent",
            "talk to an agent",
            "talk to a agent",
            "talk with agent",
            "talk with an agent",
            "talk with a agent",
            "human agent",
            "real person",
            "actual person",
            "speak to someone",
            "talk to someone",
            "escalate",
            "transfer me",
            "get me someone",
            "customer service",
            "support team",
        ]
        if any(pattern in query_lower for pattern in human_request_patterns):
            logger.info("User explicitly requested human agent")
            return EscalationDecision(
                action=EscalationAction.AUTO_ESCALATE,
                reason="User requested human assistance",
            )

        # Scenario 3: Critical escalation keywords (AUTO ESCALATE)
        for keyword in ai_config.escalation_keywords:
            if keyword.lower() in query_lower:
                logger.warning(f"Critical escalation keyword detected: {keyword}")
                return EscalationDecision(
                    action=EscalationAction.AUTO_ESCALATE,
                    reason=f"Escalation keyword detected: '{keyword}'",
                )

        # Scenario 4: NO knowledge - Let AI handle conversationally
        # CHANGED: Don't immediately escalate - allow AI to clarify first
        if retrieved_chunks_count == 0:
            logger.info("No knowledge chunks found - allowing conversational mode")
            return EscalationDecision(
                action=EscalationAction.NO_ESCALATION,
                reason="conversational_mode",  # Signal for prompt assembly
            )

        # Scenario 5: Low quality knowledge - Let AI use what it has + clarify
        if similarity_scores and len(similarity_scores) > 0:
            avg_similarity = sum(similarity_scores) / len(similarity_scores)
            if avg_similarity < self.MIN_RELEVANT_SIMILARITY:
                logger.info(
                    f"Low similarity ({avg_similarity:.2f}) - allowing partial knowledge mode"
                )
                return EscalationDecision(
                    action=EscalationAction.NO_ESCALATION,
                    reason="partial_knowledge_mode",
                )

        # Scenario 6: Good knowledge available (NO ESCALATION)
        logger.info(f"Knowledge available ({retrieved_chunks_count} chunks), AI will answer")
        return EscalationDecision(
            action=EscalationAction.NO_ESCALATION,
            reason="sufficient_knowledge",
        )

    def compute_can_answer(
        self,
        retrieved_chunks_count: int,
        avg_similarity: float,
        escalation_action: "EscalationAction",
    ) -> tuple[bool, str | None]:
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
            "i'm not sure",
            "i don't know",
            "not certain",
            "unclear",
            "i don't have",
            "i'm not aware",
            "i can't find",
            "i'm unable to",
            "no information about",
            "don't have information",
            "i apologize",
            "unfortunately, i don't",
        ]
        has_uncertainty = any(pattern in ai_response.lower() for pattern in uncertainty_patterns)

        # Substantial = more than 50 chars OR more than 10 words
        # AND no strong uncertainty markers
        # Relaxed thresholds to allow concise but complete answers
        is_substantial = (char_count > 50 or word_count > 10) and not has_uncertainty

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

    def detect_escalation_consent(self, user_message: str) -> str | None:
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
            "yes",
            "yeah",
            "yep",
            "sure",
            "ok",
            "okay",
            "please",
            "go ahead",
            "connect me",
            "transfer",
            "escalate",
            "i would",
            "i'd like",
            "that would be great",
            "yes please",
            "sure thing",
            "sounds good",
        ]

        # Rejection patterns
        rejection_patterns = [
            "no",
            "nope",
            "nah",
            "never mind",
            "nevermind",
            "i'm good",
            "im good",
            "i'm ok",
            "im ok",
            "no thanks",
            "no thank",
            "that's fine",
            "thats fine",
            "all good",
            "all set",
            "don't need",
            "dont need",
        ]

        # Check for consent (must be short message, < 15 words)
        if len(message_lower.split()) <= 15:
            if any(pattern in message_lower for pattern in consent_patterns):
                logger.info("Detected escalation consent")
                return "consent"

            if any(pattern in message_lower for pattern in rejection_patterns):
                logger.info("Detected escalation rejection")
                return "reject"

        # Unclear or unrelated message
        return None

    # Vector search and AI generation methods

    async def retrieve_relevant_chunks(
        self,
        tenant_id: UUID,
        query: str,
        max_chunks: int = 5,
        score_threshold: float | None = None,
    ) -> tuple[list[KnowledgeChunkResponse], list[float]]:
        """
        Retrieve relevant knowledge chunks using vector similarity.

        Args:
            tenant_id: Tenant UUID for filtering
            query: User query
            max_chunks: Maximum chunks to retrieve (default: 5, re-ranked to top 4)
            score_threshold: Minimum similarity score (defaults to MIN_SCORE_THRESHOLD)

        Returns:
            Tuple of (retrieved chunks, similarity scores after re-ranking)
        """
        from app.services.knowledge_service import KnowledgeService
        from app.utils.embedding import embedding_service
        from app.utils.qdrant_client import qdrant_manager

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
                    top_k=self.MAX_CHUNKS_KEEP,
                )
                scores = relevance_scores  # Update scores with reranked values

            return chunks, scores

        except Exception as e:
            logger.error(f"Error retrieving chunks: {e}")
            logger.exception(e)  # Log full traceback for debugging
            return [], []

    async def route_action_via_tools(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model_name: str | None = None,
    ) -> ToolRoutingResult:
        """
        Ask the model whether to call a tenant action tool, and with what args.

        Returns ToolRoutingResult. On any API/parse failure, is_api_error=True
        and no tool — callers fall back to normal RAG.
        """
        model = model_name or settings.OPENAI_LLM_MODEL
        try:
            await openai_llm_rate_limiter.acquire()
            client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools or None,
                tool_choice="auto" if tools else "none",
                temperature=0.1,
                max_tokens=300,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                return ToolRoutingResult(tool_name=None, arguments={}, is_api_error=False)
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            return ToolRoutingResult(
                tool_name=call.function.name, arguments=args, is_api_error=False
            )
        except Exception as exc:  # noqa: BLE001 — any failure => RAG fallback
            logger.warning(f"Action routing LLM call failed: {exc}", exc_info=True)
            return ToolRoutingResult(tool_name=None, arguments={}, is_api_error=True)

    async def generate_ai_response(
        self,
        prompt: "PromptComponents",
        max_tokens: int = 800,  # Increased for comprehensive responses (was 200)
        model_name: str | None = None,
        system_message: str | None = None,
    ) -> tuple[str, int | None, bool]:
        """
        Generate AI response using OpenAI API.

        Args:
            prompt: Assembled prompt components
            max_tokens: Maximum tokens for response
            model_name: Model name to use (defaults to settings.OPENAI_LLM_MODEL)

        Returns:
            Tuple of (AI response text, token count, is_api_error)
            - is_api_error=True means technical failure (503, timeout, etc.)
              Don't evaluate quality or offer escalation for API errors
        """
        # Use provided model or default from settings
        model = model_name or settings.OPENAI_LLM_MODEL

        try:
            # Apply rate limiting (450 RPM, 90% of 500 limit)
            await openai_llm_rate_limiter.acquire()

            # Create client
            client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

            # Generate response
            logger.debug(f"Calling OpenAI API with max_tokens={max_tokens}")
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": system_message or self.RUNTIME_SYSTEM_MESSAGE,
                    },
                    {"role": "user", "content": prompt.full_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
                top_p=0.8,
            )

            # Extract text
            response_text = response.choices[0].message.content

            # Extract token count
            token_count = None
            try:
                if response.usage:
                    token_count = response.usage.total_tokens
            except Exception as token_error:
                logger.warning(f"Failed to extract token count: {token_error}")

            response_text = response_text or ""
            logger.debug(f"Generated AI response using {model} with {token_count} tokens")
            logger.debug(
                f"Response text length: {len(response_text)} chars, {len(response_text.split())} words"
            )
            return response_text, token_count, False  # Success, not an API error

        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Error generating AI response with {model}: {e}")

            # Detect specific error types
            if "503" in error_msg or "service unavailable" in error_msg:
                logger.warning("API service temporarily unavailable (503)")
                return (
                    "I apologize, but I'm temporarily unable to process requests. Please try again in a moment.",
                    None,
                    True,
                )

            elif (
                "blocked" in error_msg
                or "safety" in error_msg
                or "content" in error_msg
                or "content_filter" in error_msg
            ):
                logger.warning("Content blocked by safety filters")
                return (
                    "I'm unable to process that request. Would you like me to connect you with someone from our team?",
                    None,
                    True,
                )

            elif "rate limit" in error_msg or "quota" in error_msg:
                logger.warning("Rate limit or quota exceeded")
                return (
                    "I'm experiencing high demand right now. Please try again in a moment.",
                    None,
                    True,
                )

            else:
                # Generic API error
                logger.error(f"Unknown API error: {e}")
                return (
                    "I'm having trouble processing your request. Would you like me to connect you with someone from our team?",
                    None,
                    True,
                )

    def track_retrieval_metrics(
        self,
        query: str,
        retrieved_chunks_count: int,
        avg_similarity: float,
        response_quality: "ResponseQuality",
        escalated: bool,
        generation_skipped: bool = False,
        skip_reason: str | None = None,
        prompt_tokens: int | None = None,
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
        retrieval_hit = (
            retrieved_chunks_count > 0 and avg_similarity >= self.MIN_RELEVANT_SIMILARITY
        )
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
