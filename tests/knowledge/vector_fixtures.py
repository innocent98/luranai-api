"""Fixtures for knowledge-ingestion tests.

The vector store is qdrant-client's in-memory mode: it enforces the same point-id rule as the
server (UUID or unsigned int), which is exactly what the text-upload path used to violate.
Embeddings are stubbed (no network, no OpenAI key needed).
"""

import hashlib
import math

import pytest
from qdrant_client import QdrantClient

DIM = 16


def fake_vector(text: str) -> list[float]:
    """Deterministic bag-of-words style vector: similar words -> similar vectors."""
    vec = [0.0] * DIM
    for word in text.lower().split():
        w = "".join(ch for ch in word if ch.isalnum())
        if not w:
            continue
        slot = int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM  # noqa: S324 - not security
        vec[slot] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    if norm == 1.0 and not any(vec):
        vec[0] = 1.0
    return [v / norm for v in vec]


@pytest.fixture
def vector_store(monkeypatch):
    """Point the global qdrant manager at an empty in-memory instance (no collection yet)."""
    from app.utils.qdrant_client import qdrant_manager

    client = QdrantClient(":memory:")
    monkeypatch.setattr(qdrant_manager, "client", client)
    monkeypatch.setattr(qdrant_manager, "dimension", DIM)
    monkeypatch.setattr(qdrant_manager, "collection_name", "kb_test")
    return client


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Stub every embedding entry point. Returns a switch to make embedding fail."""
    from app.utils.embedding import embedding_service

    state = {"fail": False}

    async def _one(text, use_rate_limit=True):
        return None if state["fail"] else fake_vector(text)

    async def _batch(texts, task_type="retrieval_document", use_rate_limit=True):
        return [None if state["fail"] else fake_vector(t) for t in texts]

    monkeypatch.setattr(embedding_service, "generate_embedding", _one)
    monkeypatch.setattr(embedding_service, "generate_query_embedding", _one)
    monkeypatch.setattr(embedding_service, "generate_batch_embeddings", _batch)
    return state


def vector_count(client: QdrantClient, collection: str = "kb_test") -> int:
    names = [c.name for c in client.get_collections().collections]
    if collection not in names:
        return 0
    return int(client.count(collection).count)
