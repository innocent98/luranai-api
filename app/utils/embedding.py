"""Embedding generation utilities using Google AI."""

from typing import List, Optional
import google.generativeai as genai
from loguru import logger

from app.core.config import settings


class EmbeddingService:
    """Service for generating text embeddings using Google AI."""

    def __init__(self):
        """Initialize embedding service with Google AI."""
        genai.configure(api_key=settings.GOOGLE_API_KEY)
        self.model = settings.EMBEDDING_MODEL

    async def generate_embedding(self, text: str) -> Optional[List[float]]:
        """
        Generate embedding vector for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector or None if error
        """
        try:
            # Generate embedding
            result = genai.embed_content(
                model=self.model,
                content=text,
                task_type="retrieval_document",  # For document chunks
            )

            embedding = result["embedding"]

            logger.debug(f"Generated embedding of dimension {len(embedding)}")
            return embedding

        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            return None

    async def generate_query_embedding(self, query: str) -> Optional[List[float]]:
        """
        Generate embedding vector for search query.

        Args:
            query: Search query text

        Returns:
            Embedding vector or None if error
        """
        try:
            # Generate embedding with query task type
            result = genai.embed_content(
                model=self.model,
                content=query,
                task_type="retrieval_query",  # For search queries
            )

            embedding = result["embedding"]

            logger.debug(f"Generated query embedding of dimension {len(embedding)}")
            return embedding

        except Exception as e:
            logger.error(f"Error generating query embedding: {e}")
            return None

    async def generate_batch_embeddings(
        self, texts: List[str], task_type: str = "retrieval_document"
    ) -> List[Optional[List[float]]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed
            task_type: Task type (retrieval_document or retrieval_query)

        Returns:
            List of embedding vectors
        """
        embeddings = []

        for text in texts:
            if task_type == "retrieval_query":
                embedding = await self.generate_query_embedding(text)
            else:
                embedding = await self.generate_embedding(text)

            embeddings.append(embedding)

        return embeddings


# Global instance
embedding_service = EmbeddingService()
