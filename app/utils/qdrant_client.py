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

    def build_search_filter(
        self,
        tenant_id: UUID,
        is_active: bool = True,
        source_status: Optional[str] = None,
        visibility_level: Optional[str] = None,
    ) -> Filter:
        """
        Build comprehensive filter for vector search.

        Args:
            tenant_id: Tenant UUID (required)
            is_active: Filter for active chunks (default: True)
            source_status: Optional source status filter (ACTIVE, ARCHIVED)
            visibility_level: Optional visibility filter (PUBLIC, INTERNAL, RESTRICTED)

        Returns:
            Qdrant Filter object
        """
        conditions = [
            FieldCondition(
                key="tenant_id",
                match=MatchValue(value=str(tenant_id)),
            ),
        ]

        if is_active:
            conditions.append(
                FieldCondition(
                    key="is_active",
                    match=MatchValue(value=True),
                )
            )

        if source_status:
            conditions.append(
                FieldCondition(
                    key="source_status",
                    match=MatchValue(value=source_status),
                )
            )

        if visibility_level:
            conditions.append(
                FieldCondition(
                    key="visibility_level",
                    match=MatchValue(value=visibility_level),
                )
            )

        return Filter(must=conditions)

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
            # Build enhanced payload with metadata for filtering
            payload = {
                "tenant_id": str(tenant_id),
                "chunk_id": str(chunk_id),
                "source_id": str(source_id),
                "content": content,
                "is_active": metadata.get("is_active", True),  # Add this
                "source_status": metadata.get("source_status", "ACTIVE"),  # Add this
                "visibility_level": metadata.get("visibility_level", "PUBLIC"),  # Add this
                "content_type": metadata.get("content_type", "unknown"),  # Add this (faq, policy, contact, pricing)
                "language": metadata.get("language", "en"),  # Add this
                **metadata,
            }

            point = PointStruct(
                id=vector_id,
                vector=embedding,
                payload=payload,
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
        is_active: bool = True,
        source_status: Optional[str] = "ACTIVE",
        visibility_level: Optional[str] = "PUBLIC",
    ) -> List[dict]:
        """
        Search for similar vectors with enhanced metadata filtering.

        SECURITY: Tenant filtering is enforced SERVER-SIDE by Qdrant via query_filter.
        This ensures multi-tenant isolation at the vector database level.

        Args:
            query_vector: Query embedding vector
            tenant_id: Tenant UUID to filter by (enforced server-side)
            limit: Maximum number of results
            score_threshold: Minimum similarity score
            is_active: Filter for active chunks (default: True)
            source_status: Optional source status filter (default: ACTIVE)
            visibility_level: Visibility filter (default: PUBLIC for security)

        Returns:
            List of search results with scores
        """
        try:
            # Create enhanced filter with metadata (server-side enforcement)
            search_filter = self.build_search_filter(
                tenant_id=tenant_id,
                is_active=is_active,
                source_status=source_status,
                visibility_level=visibility_level,
            )

            # Perform search using query_points (qdrant-client 1.16+)
            search_result = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=search_filter,
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

        This operation is IDEMPOTENT - deleting non-existent vectors is a no-op.

        Args:
            chunk_id: Knowledge chunk UUID

        Returns:
            True if successful (including if vector already deleted)
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

            logger.debug(f"Deleted vectors for chunk {chunk_id} (idempotent)")
            return True

        except Exception as e:
            logger.warning(f"Failed to delete vectors for chunk {chunk_id}: {e}")
            return False

    def delete_by_source_id(self, source_id: UUID) -> bool:
        """
        Delete all vectors for a knowledge source.

        This operation is IDEMPOTENT - deleting non-existent vectors is a no-op.
        Safe to call even if vectors were already deleted or never existed.

        Args:
            source_id: Knowledge source UUID

        Returns:
            True if successful (including if vectors already deleted)
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

            logger.debug(f"Deleted vectors for source {source_id} (idempotent)")
            return True

        except Exception as e:
            # Log as warning, not error, since deletion failures don't affect DB state
            logger.warning(f"Failed to delete vectors for source {source_id}: {e}")
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
