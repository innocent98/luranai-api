"""Text upload (POST /knowledge/upload) must index like the file path and fail loudly."""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource, SourceStatus, SourceType
from app.services.knowledge_service import KnowledgeService
from app.utils.knowledge_failures import KnowledgeError
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user
from tests.knowledge.vector_fixtures import vector_count

UPLOAD = "/api/v1/knowledge/upload"

FAQ = (
    "Opening hours\nWe are open Monday to Saturday from 7am to 6pm.\n\n"
    "Delivery\nWe deliver within 10km of the shop for 1,500 naira.\n\n"
    "Returns\nTell us within 24 hours and we will replace or refund your order."
)


def _chunks(db, source_id):
    return (
        db.execute(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id))
        .scalars()
        .all()
    )


async def test_text_path_writes_a_vector_for_every_chunk(db, vector_store, fake_embeddings):
    """Regression: the text path used ids like '<uuid>_0', which Qdrant rejects; the manager
    swallowed the error, so rows were stored and Qdrant stayed empty."""
    _, token = _make_tenant_user(db)
    from app.db.models.user import User

    tid = db.execute(select(User.tenant_id).limit(1)).scalar_one()
    source = await KnowledgeService(db).process_document(
        tenant_id=tid, document_text=FAQ, document_name="faq", source_type=SourceType.FAQ
    )
    chunks = _chunks(db, source.id)
    assert len(chunks) >= 3
    assert vector_count(vector_store) == len(chunks)
    assert source.status == SourceStatus.ACTIVE
    # the DB reference is the real point id
    points = vector_store.scroll("kb_test", limit=50, with_payload=True)[0]
    assert {str(p.id) for p in points} == {c.vector_id for c in chunks}
    assert all(p.payload["tenant_id"] == str(tid) for p in points)


async def test_collection_is_created_when_missing(db, vector_store, fake_embeddings):
    _, _ = _make_tenant_user(db)
    from app.db.models.user import User

    tid = db.execute(select(User.tenant_id).limit(1)).scalar_one()
    assert vector_count(vector_store) == 0
    await KnowledgeService(db).process_document(
        tenant_id=tid, document_text=FAQ, document_name="faq", source_type=SourceType.FAQ
    )
    names = [c.name for c in vector_store.get_collections().collections]
    assert "kb_test" in names


async def test_short_document_is_indexed_not_rejected(db, vector_store, fake_embeddings):
    user, _ = _make_tenant_user(db)
    source = await KnowledgeService(db).process_document(
        tenant_id=user.tenant_id,
        document_text="We open at 7am every day.",
        document_name="hours",
        source_type=SourceType.FAQ,
    )
    assert source.status == SourceStatus.ACTIVE
    assert source.chunk_count == 1
    assert vector_count(vector_store) == 1


def test_api_text_upload_end_to_end(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    r = client.post(
        UPLOAD,
        json={"tenant_id": str(user.tenant_id), "document_name": "faq", "document_text": FAQ},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["failure_reason"] is None
    assert vector_count(vector_store) == body["chunk_count"] >= 3


@pytest.mark.parametrize("text", ["", "   ", "too short", "x" * 19, "   " + "y" * 10 + "   "])
def test_api_text_upload_below_minimum_is_422_and_creates_no_source(
    client, db, vector_store, fake_embeddings, text
):
    user, token = _make_tenant_user(db)
    r = client.post(
        UPLOAD,
        json={"tenant_id": str(user.tenant_id), "document_name": "x", "document_text": text},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "document_too_short"
    assert body["min_chars"] == 20
    assert isinstance(body["message"], str) and body["message"]
    n = (
        db.execute(select(KnowledgeSource).where(KnowledgeSource.tenant_id == user.tenant_id))
        .scalars()
        .all()
    )
    assert n == []


def test_api_text_upload_without_letters_is_no_text_extracted(
    client, db, vector_store, fake_embeddings
):
    user, token = _make_tenant_user(db)
    r = client.post(
        UPLOAD,
        json={"tenant_id": str(user.tenant_id), "document_name": "x", "document_text": "-" * 40},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "no_text_extracted"


def test_api_embedding_failure_marks_source_failed_with_reason(
    client, db, vector_store, fake_embeddings
):
    fake_embeddings["fail"] = True
    user, token = _make_tenant_user(db)
    tid = user.tenant_id
    r = client.post(
        UPLOAD,
        json={"tenant_id": str(user.tenant_id), "document_name": "faq", "document_text": FAQ},
        headers=_auth(token),
    )
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "embedding_failed"
    src = db.execute(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tid)).scalar_one()
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "embedding_failed"
    listing = client.get(f"/api/v1/knowledge/tenants/{tid}/sources", headers=_auth(token)).json()
    assert listing[0]["status"] == "failed"
    assert listing[0]["failure_reason"]["code"] == "embedding_failed"
    assert listing[0]["failure_reason"]["message"]


def test_api_vector_store_down_marks_source_failed_with_reason(
    client, db, vector_store, fake_embeddings, monkeypatch
):
    from app.utils.qdrant_client import qdrant_manager

    def _boom(*a, **k):
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(qdrant_manager.client, "get_collections", _boom)
    monkeypatch.setattr(qdrant_manager.client, "upsert", _boom)
    user, token = _make_tenant_user(db)
    tid = user.tenant_id
    r = client.post(
        UPLOAD,
        json={"tenant_id": str(user.tenant_id), "document_name": "faq", "document_text": FAQ},
        headers=_auth(token),
    )
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "vector_store_unavailable"
    src = db.execute(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tid)).scalar_one()
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "vector_store_unavailable"


def test_upsert_failure_is_not_counted_as_success(db, vector_store, fake_embeddings, monkeypatch):
    """Even if the collection exists, a rejected upsert must fail the source."""
    from app.utils.qdrant_client import qdrant_manager

    monkeypatch.setattr(qdrant_manager, "upsert_vector", lambda **kw: False)
    user, _ = _make_tenant_user(db)
    import asyncio

    with pytest.raises(KnowledgeError):
        asyncio.run(
            KnowledgeService(db).process_document(
                tenant_id=user.tenant_id, document_text=FAQ, document_name="faq"
            )
        )
    src = db.execute(
        select(KnowledgeSource).where(KnowledgeSource.tenant_id == user.tenant_id)
    ).scalar_one()
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "vector_store_unavailable"


def test_failed_and_inactive_sources_do_not_consume_the_plan_cap(db):
    from fastapi import HTTPException

    from app.db.models.tenant import TenantCapabilities

    user, _ = _make_tenant_user(db)
    tid = user.tenant_id
    db.add(
        TenantCapabilities(
            tenant_id=tid,
            can_upload_knowledge=True,
            max_knowledge_sources=2,
            can_use_url_sources=True,
        )
    )
    for i, st in enumerate([SourceStatus.FAILED, SourceStatus.FAILED, SourceStatus.INACTIVE]):
        db.add(
            KnowledgeSource(
                tenant_id=tid,
                name=f"s{i}",
                source_type=SourceType.OTHER,
                content_hash=f"{i:064d}",
                status=st,
            )
        )
    db.flush()
    KnowledgeService(db).check_knowledge_source_limit(tid, "file")  # must not raise
    db.add(
        KnowledgeSource(
            tenant_id=tid,
            name="a",
            source_type=SourceType.OTHER,
            content_hash="a" * 64,
            status=SourceStatus.ACTIVE,
        )
    )
    db.add(
        KnowledgeSource(
            tenant_id=tid,
            name="b",
            source_type=SourceType.OTHER,
            content_hash="b" * 64,
            status=SourceStatus.ACTIVE,
        )
    )
    db.flush()
    with pytest.raises(HTTPException) as exc:
        KnowledgeService(db).check_knowledge_source_limit(tid, "file")
    assert exc.value.status_code == 403
    _ = uuid  # keep import used


LONG_DOC = " ".join(
    f"Sentence number {i} explains how our bakery handles custom orders and pickup times."
    for i in range(14)
)


async def test_root_cause_text_path_vector_ids_are_valid_for_a_long_document(
    db, vector_store, fake_embeddings
):
    """Isolates the vector bug from the chunker: a document the OLD chunker accepted still
    ended with zero vectors because the point id was '<source uuid>_<n>'."""
    user, _ = _make_tenant_user(db)
    source = await KnowledgeService(db).process_document(
        tenant_id=user.tenant_id, document_text=LONG_DOC, document_name="orders"
    )
    assert source.chunk_count >= 1
    assert vector_count(vector_store) == source.chunk_count


async def test_root_cause_invalid_point_id_even_when_collection_exists(
    db, vector_store, fake_embeddings
):
    """The local run in the brief had the collection; ids like '<uuid>_0' were still rejected."""
    from qdrant_client.models import Distance, VectorParams

    from tests.knowledge.vector_fixtures import DIM

    vector_store.create_collection(
        "kb_test", vectors_config=VectorParams(size=DIM, distance=Distance.COSINE)
    )
    user, _ = _make_tenant_user(db)
    source = await KnowledgeService(db).process_document(
        tenant_id=user.tenant_id, document_text=LONG_DOC, document_name="orders"
    )
    assert vector_count(vector_store) == source.chunk_count >= 1
