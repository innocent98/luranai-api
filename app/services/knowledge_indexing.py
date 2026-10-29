"""Embed one chunk and write it to the vector store: the single indexing step shared by the
synchronous text upload and the background ingestion worker.

Both paths used to differ (the text path built point ids Qdrant rejects, never ensured the
collection, and ignored a failed upsert), so a document could be "stored" with no vectors.
"""

from uuid import UUID

from app.utils.knowledge_failures import FailureCode


async def embed_and_store_chunk(
    *,
    tenant_id: UUID,
    source_id: UUID,
    source_name: str,
    source_type: str,
    version: int,
    chunk_id: UUID,
    chunk_index: int,
    content: str,
    vector_id: str,
) -> FailureCode | None:
    """
    Embed `content` and upsert it under `vector_id` (must be a UUID: Qdrant rejects other ids).

    Returns:
        None on success, else why it failed. A rejected upsert is a failure, never a success.
    """
    from app.utils.embedding import embedding_service
    from app.utils.qdrant_client import qdrant_manager

    embedding = await embedding_service.generate_embedding(content)
    if not embedding:
        return FailureCode.EMBEDDING_FAILED

    stored = qdrant_manager.upsert_vector(
        vector_id=vector_id,
        embedding=embedding,
        tenant_id=tenant_id,
        chunk_id=chunk_id,
        source_id=source_id,
        content=content,
        metadata={
            "chunk_index": chunk_index,
            "source_name": source_name,
            "source_type": source_type,
            "version": version,
        },
    )
    return None if stored else FailureCode.VECTOR_STORE_UNAVAILABLE
