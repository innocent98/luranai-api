"""Embedding generation utilities using Google AI."""

from typing import List, Optional
from google import genai
from loguru import logger

from app.core.config import settings
from app.utils.rate_limiter import embedding_rate_limiter, embedding_circuit_breaker


class EmbeddingService:
    """Service for generating text embeddings using Google AI."""

    def __init__(self):
        """Initialize embedding service with Google AI."""
        self.client = genai.Client(api_key=settings.GOOGLE_API_KEY)
        self.model = settings.EMBEDDING_MODEL

    async def generate_embedding(self, text: str, use_rate_limit: bool = True) -> Optional[List[float]]:
        """
        Generate embedding vector for text.

        Args:
            text: Text to embed
            use_rate_limit: Whether to enforce rate limiting (default: True)

        Returns:
            Embedding vector or None if error
        """
        try:
            # Apply rate limiting with circuit breaker
            if use_rate_limit:
                # Check per-minute rate limit
                await embedding_rate_limiter.acquire()

                # Check daily quota with circuit breaker
                # (will raise RuntimeError if quota nearly exhausted)
                await embedding_circuit_breaker.check_and_acquire()

            # Generate embedding
            result = self.client.models.embed_content(
                model=self.model,
                contents=text,
            )

            # Access embedding from response object
            embedding = result.embeddings[0].values

            logger.debug(f"Generated embedding of dimension {len(embedding)}")
            return embedding

        except RuntimeError as e:
            # Circuit breaker opened - quota exhausted
            logger.error(f"Circuit breaker triggered: {e}")
            return None
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            return None

    async def generate_query_embedding(self, query: str, use_rate_limit: bool = True) -> Optional[List[float]]:
        """
        Generate embedding vector for search query.

        Args:
            query: Search query text
            use_rate_limit: Whether to enforce rate limiting (default: True)

        Returns:
            Embedding vector or None if error
        """
        try:
            # Apply rate limiting with circuit breaker
            if use_rate_limit:
                # Check per-minute rate limit
                await embedding_rate_limiter.acquire()

                # Check daily quota with circuit breaker
                await embedding_circuit_breaker.check_and_acquire()

            # Generate embedding with query task type
            result = self.client.models.embed_content(
                model=self.model,
                contents=query,
            )

            # Access embedding from response object
            embedding = result.embeddings[0].values

            logger.debug(f"Generated query embedding of dimension {len(embedding)}")
            return embedding

        except RuntimeError as e:
            # Circuit breaker opened - quota exhausted
            logger.error(f"Circuit breaker triggered: {e}")
            return None
        except Exception as e:
            logger.error(f"Error generating query embedding: {e}")
            return None

    async def generate_batch_embeddings(
        self, texts: List[str], task_type: str = "retrieval_document", use_rate_limit: bool = True
    ) -> List[Optional[List[float]]]:
        """
        Generate embeddings for multiple texts with rate limiting.

        IMPORTANT: This method processes texts sequentially with rate limiting
        to avoid exceeding API quotas. For large batches, this will take time
        (e.g., 30 texts = ~3 minutes at 10 requests/minute).

        Args:
            texts: List of texts to embed
            task_type: Task type (retrieval_document or retrieval_query)
            use_rate_limit: Whether to enforce rate limiting (default: True)

        Returns:
            List of embedding vectors (None for failed embeddings)
        """
        embeddings = []
        total = len(texts)

        logger.info(f"Starting batch embedding generation for {total} texts (rate limited: {use_rate_limit})")

        for i, text in enumerate(texts):
            if task_type == "retrieval_query":
                embedding = await self.generate_query_embedding(text, use_rate_limit=use_rate_limit)
            else:
                embedding = await self.generate_embedding(text, use_rate_limit=use_rate_limit)

            embeddings.append(embedding)

            # Log progress every 10 items or at the end
            if (i + 1) % 10 == 0 or (i + 1) == total:
                logger.info(f"Batch progress: {i + 1}/{total} embeddings generated")

        successful = sum(1 for e in embeddings if e is not None)
        logger.info(f"Batch complete: {successful}/{total} embeddings successful")

        return embeddings


# Global instance
embedding_service = EmbeddingService()
