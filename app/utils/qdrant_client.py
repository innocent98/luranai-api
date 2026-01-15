"""Qdrant client utility for vector database operations."""

from typing import Optional, List
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    SearchParams,
)
from loguru import logger

from app.core.config import settings


class QdrantManager:
    """Manager for Qdrant vector database operations."""

    def __init__(self):
        """Initialize Qdrant client."""
        self.client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=30,
        )
        self.collection_name = settings.QDRANT_COLLECTION_NAME
        self.dimension = settings.EMBEDDING_DIMENSION

    def ensure_collection(self) -> bool:
        """
        Ensure the collection exists, create if it doesn't.

        Returns:
            True if collection was created or already exists
        """
        try:
            # Check if collection exists
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]

            if self.collection_name in collection_names:
                logger.info(f"Collection '{self.collection_name}' already exists")
                return True

            # Create collection
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.dimension,
                    distance=Distance.COSINE,
                ),
            )

            logger.info(f"Created collection '{self.collection_name}'")
            return True

        except Exception as e:
            logger.error(f"Error ensuring collection: {e}")
            return False

    def upsert_vector(
        self,
        vector_id: str,
        embedding: List[float],
        tenant_id: UUID,
        chunk_id: UUID,
        source_id: UUID,
        content: str,
        metadata: dict,
    ) -> bool:
        """
        Insert or update a vector in Qdrant.

        Args:
            vector_id: Unique vector ID
            embedding: Vector embedding
            tenant_id: Tenant UUID for filtering
            chunk_id: Knowledge chunk UUID
            source_id: Knowledge source UUID
            content: Original chunk content
            metadata: Additional metadata

        Returns:
            True if successful
        """
        try:
            point = PointStruct(
                id=vector_id,
                vector=embedding,
                payload={
                    "tenant_id": str(tenant_id),
                    "chunk_id": str(chunk_id),
                    "source_id": str(source_id),
                    "content": content,
                    **metadata,
                },
            )

            self.client.upsert(
                collection_name=self.collection_name,
                points=[point],
            )

            logger.debug(f"Upserted vector {vector_id} for tenant {tenant_id}")
            return True

        except Exception as e:
            logger.error(f"Error upserting vector: {e}")
            return False

    def search(
        self,
        query_vector: List[float],
        tenant_id: UUID,
        limit: int = 5,
        score_threshold: Optional[float] = None,
    ) -> List[dict]:
        """
        Search for similar vectors with tenant filtering.

        Args:
            query_vector: Query embedding vector
            tenant_id: Tenant UUID to filter by
            limit: Maximum number of results
            score_threshold: Minimum similarity score

        Returns:
            List of search results with scores
        """
        try:
            # Create filter for tenant isolation
            tenant_filter = Filter(
                must=[
                    FieldCondition(
                        key="tenant_id",
                        match=MatchValue(value=str(tenant_id)),
                    )
                ]
            )

            # Perform search using query_points (qdrant-client 1.16+)
            search_result = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=tenant_filter,
                limit=limit,
                score_threshold=score_threshold,
                search_params=SearchParams(
                    hnsw_ef=128,  # Higher values = more accurate but slower
                    exact=False,
                ),
            ).points

            # Format results
            results = []
            for hit in search_result:
                results.append(
                    {
                        "id": hit.id,
                        "score": hit.score,
                        "chunk_id": hit.payload.get("chunk_id"),
                        "source_id": hit.payload.get("source_id"),
                        "content": hit.payload.get("content"),
                        "metadata": {
                            k: v
                            for k, v in hit.payload.items()
                            if k not in ["tenant_id", "chunk_id", "source_id", "content"]
                        },
                    }
                )

            logger.debug(
                f"Found {len(results)} results for tenant {tenant_id} with scores: {[r['score'] for r in results]}"
            )
            return results

        except Exception as e:
            logger.error(f"Error searching vectors: {e}")
            return []

    def delete_by_chunk_id(self, chunk_id: UUID) -> bool:
        """
        Delete vector by chunk ID.

        Args:
            chunk_id: Knowledge chunk UUID

        Returns:
            True if successful
        """
        try:
            # Delete points with matching chunk_id
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="chunk_id",
                            match=MatchValue(value=str(chunk_id)),
                        )
                    ]
                ),
            )

            logger.debug(f"Deleted vectors for chunk {chunk_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting vectors: {e}")
            return False

    def delete_by_source_id(self, source_id: UUID) -> bool:
        """
        Delete all vectors for a knowledge source.

        Args:
            source_id: Knowledge source UUID

        Returns:
            True if successful
        """
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="source_id",
                            match=MatchValue(value=str(source_id)),
                        )
                    ]
                ),
            )

            logger.debug(f"Deleted all vectors for source {source_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting vectors: {e}")
            return False

    def count_vectors(self, tenant_id: Optional[UUID] = None) -> int:
        """
        Count vectors in collection, optionally filtered by tenant.

        Args:
            tenant_id: Optional tenant UUID to filter by

        Returns:
            Number of vectors
        """
        try:
            if tenant_id:
                result = self.client.count(
                    collection_name=self.collection_name,
                    count_filter=Filter(
                        must=[
                            FieldCondition(
                                key="tenant_id",
                                match=MatchValue(value=str(tenant_id)),
                            )
                        ]
                    ),
                )
            else:
                result = self.client.count(collection_name=self.collection_name)

            return result.count

        except Exception as e:
            logger.error(f"Error counting vectors: {e}")
            return 0


# Global instance
qdrant_manager = QdrantManager()
