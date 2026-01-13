"""Cached embedding service with Redis to reduce API calls."""

from typing import List, Optional
import hashlib
import json
from redis import Redis
from loguru import logger

from app.core.config import settings
from app.utils.embedding import embedding_service


class CachedEmbeddingService:
    """
    Embedding service with Redis caching.

    Caches embeddings to avoid redundant API calls for identical text.
    Useful for frequently asked queries or duplicate content.
    """

    def __init__(self, redis_client: Optional[Redis] = None, ttl: int = 86400 * 7):
        """
        Initialize cached embedding service.

        Args:
            redis_client: Redis client instance (optional)
            ttl: Time to live in seconds (default: 7 days)
        """
        self.embedding_service = embedding_service
        self.redis = redis_client
        self.ttl = ttl
        self.cache_prefix = "embedding:"

    def _get_cache_key(self, text: str, task_type: str) -> str:
        """Generate cache key from text and task type."""
        content = f"{task_type}:{text}"
        hash_key = hashlib.md5(content.encode()).hexdigest()
        return f"{self.cache_prefix}{hash_key}"

    async def generate_embedding_cached(
        self, text: str, task_type: str = "retrieval_document"
    ) -> Optional[List[float]]:
        """
        Generate embedding with caching.

        Args:
            text: Text to embed
            task_type: retrieval_document or retrieval_query

        Returns:
            Embedding vector or None
        """
        if not self.redis:
            # No caching, use direct API
            if task_type == "retrieval_query":
                return await self.embedding_service.generate_query_embedding(text)
            return await self.embedding_service.generate_embedding(text)

        # Check cache
        cache_key = self._get_cache_key(text, task_type)

        try:
            cached = self.redis.get(cache_key)
            if cached:
                logger.debug(f"Cache HIT for embedding (saved API call)")
                return json.loads(cached)
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

        # Cache miss - generate embedding
        logger.debug(f"Cache MISS for embedding (API call)")

        if task_type == "retrieval_query":
            embedding = await self.embedding_service.generate_query_embedding(text)
        else:
            embedding = await self.embedding_service.generate_embedding(text)

        # Store in cache
        if embedding and self.redis:
            try:
                self.redis.setex(
                    cache_key,
                    self.ttl,
                    json.dumps(embedding)
                )
            except Exception as e:
                logger.warning(f"Cache write error: {e}")

        return embedding

    async def generate_batch_embeddings_cached(
        self, texts: List[str], task_type: str = "retrieval_document"
    ) -> List[Optional[List[float]]]:
        """
        Generate batch embeddings with caching.

        Args:
            texts: List of texts to embed
            task_type: retrieval_document or retrieval_query

        Returns:
            List of embedding vectors
        """
        embeddings = []

        for text in texts:
            embedding = await self.generate_embedding_cached(text, task_type)
            embeddings.append(embedding)

        return embeddings


# Example usage with Redis
def get_cached_embedding_service() -> CachedEmbeddingService:
    """Get cached embedding service with Redis connection."""
    try:
        from redis import Redis
        redis_client = Redis.from_url(settings.REDIS_URL)
        return CachedEmbeddingService(redis_client=redis_client)
    except Exception as e:
        logger.warning(f"Redis not available, using uncached service: {e}")
        return CachedEmbeddingService(redis_client=None)


# For immediate use (optional - requires enabling in knowledge service)
# cached_embedding_service = get_cached_embedding_service()
