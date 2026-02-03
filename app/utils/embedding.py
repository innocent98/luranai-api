"""Embedding generation utilities using OpenAI."""

from typing import List, Optional
from openai import AsyncOpenAI
from loguru import logger

from app.core.config import settings
from app.utils.rate_limiter import openai_embedding_rate_limiter


class EmbeddingService:
    """Service for generating text embeddings using OpenAI."""

    def __init__(self):
        """Initialize embedding service with OpenAI."""
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.model = settings.OPENAI_EMBEDDING_MODEL

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
            # Apply rate limiting
            if use_rate_limit:
                await openai_embedding_rate_limiter.acquire()

            # Generate embedding
            response = await self.client.embeddings.create(
                model=self.model,
                input=text,
            )

            # Extract embedding from response
            embedding = response.data[0].embedding

            logger.debug(f"Generated embedding of dimension {len(embedding)}")
            return embedding

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
            # Apply rate limiting
            if use_rate_limit:
                await openai_embedding_rate_limiter.acquire()

            # Generate embedding (OpenAI doesn't distinguish between document and query embeddings)
            response = await self.client.embeddings.create(
                model=self.model,
                input=query,
            )

            # Extract embedding from response
            embedding = response.data[0].embedding

            logger.debug(f"Generated query embedding of dimension {len(embedding)}")
            return embedding

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
        (e.g., 100 texts = ~2 minutes at 2,700 requests/minute with rate limiting).

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
