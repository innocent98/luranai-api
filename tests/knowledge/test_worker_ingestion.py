"""File / URL uploads: the background worker must index for real and report why it failed."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models.job import JobStatus, JobType, ProcessingJob
from app.db.models.knowledge import KnowledgeChunk, KnowledgeSource, SourceStatus
from app.schemas.job import JobCreate
from app.services.job_service import JobService
from app.workers.document_processor import process_document_ingestion
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user
from tests.knowledge.vector_fixtures import vector_count

DOC = (
    "Opening hours\nWe are open Monday to Saturday from 7am to 6pm.\n\n"
    "Delivery\nWe deliver within 10km of the shop for 1,500 naira.\n\n"
    "Returns\nTell us within 24 hours and we will replace or refund your order."
)


def _make_job(db, tenant_id, text, **extra):
    return JobService(db).create_job(
        JobCreate(
            tenant_id=tenant_id,
            job_type=JobType.DOCUMENT_INGESTION,
            input_data={
                "tenant_id": str(tenant_id),
                "document_text": text,
                "document_name": "menu.txt",
                "source_type": "faq",
                "source_category": "file",
                "file_size_bytes": len(text),
                "metadata": {},
                **extra,
            },
        )
    )


@contextmanager
def _worker_env(db):
    with (
        patch.object(db, "close", lambda: None),
        patch("app.workers.document_processor.SessionLocal", return_value=db),
        patch("app.workers.document_processor.send_job_completion_email", new_callable=AsyncMock),
        patch("app.workers.document_processor.broadcast_job_status", new_callable=AsyncMock),
    ):
        yield


async def _run(db, job_id):
    with _worker_env(db):
        return await process_document_ingestion({}, str(job_id))


def _sources(db, tid):
    return (
        db.execute(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tid)).scalars().all()
    )


async def test_file_path_indexes_vectors_and_activates(db, vector_store, fake_embeddings):
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, DOC)
    result = await _run(db, job.id)
    assert result["success"] is True, result
    (src,) = _sources(db, user.tenant_id)
    assert src.status == SourceStatus.ACTIVE and src.failure_code is None
    chunks = (
        db.execute(select(KnowledgeChunk).where(KnowledgeChunk.source_id == src.id)).scalars().all()
    )
    assert len(chunks) >= 3
    assert vector_count(vector_store) == len(chunks)
    assert all(c.vector_id == str(c.id) for c in chunks)


async def test_short_file_fails_the_job_with_document_too_short_and_no_source(
    db, vector_store, fake_embeddings
):
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, "Open 7am.")
    result = await _run(db, job.id)
    assert result["success"] is False
    assert _sources(db, user.tenant_id) == []
    db.expire_all()
    row = db.get(ProcessingJob, job.id)
    assert row.status == JobStatus.FAILED
    assert row.result_data == {"failure_code": "document_too_short"}
    reason = JobService(db).get_job(job.id).failure_reason
    assert reason.code == "document_too_short" and reason.message


def test_job_status_endpoint_exposes_failure_reason(client, db, vector_store, fake_embeddings):
    import asyncio

    user, token = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, "Open 7am.")
    asyncio.run(_run(db, job.id))
    r = client.get(f"/api/v1/jobs/{job.id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_reason"]["code"] == "document_too_short"


async def test_embedding_failure_fails_source_and_job(db, vector_store, fake_embeddings):
    fake_embeddings["fail"] = True
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, DOC)
    await _run(db, job.id)
    (src,) = _sources(db, user.tenant_id)
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "embedding_failed"
    assert JobService(db).get_job(job.id).failure_reason.code == "embedding_failed"
    assert vector_count(vector_store) == 0


async def test_vector_store_outage_fails_source_and_job(
    db, vector_store, fake_embeddings, monkeypatch
):
    from app.utils.qdrant_client import qdrant_manager

    monkeypatch.setattr(qdrant_manager, "ensure_collection", lambda: False)
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, DOC)
    await _run(db, job.id)
    (src,) = _sources(db, user.tenant_id)
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "vector_store_unavailable"
    assert JobService(db).get_job(job.id).failure_reason.code == "vector_store_unavailable"


async def test_rejected_upserts_never_count_as_indexed(
    db, vector_store, fake_embeddings, monkeypatch
):
    from app.utils.qdrant_client import qdrant_manager

    monkeypatch.setattr(qdrant_manager, "upsert_vector", lambda **kw: False)
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, DOC)
    result = await _run(db, job.id)
    assert result["success"] is False
    (src,) = _sources(db, user.tenant_id)
    assert src.status == SourceStatus.FAILED
    assert src.failure_code == "vector_store_unavailable"


async def test_crash_mid_job_does_not_leave_a_processing_source(
    db, vector_store, fake_embeddings, monkeypatch
):
    """A source stuck in PROCESSING would count toward the plan cap forever."""
    from app.utils.qdrant_client import qdrant_manager

    def _boom():
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(qdrant_manager, "ensure_collection", _boom)
    user, _ = _make_tenant_user(db)
    job = _make_job(db, user.tenant_id, DOC)
    result = await _run(db, job.id)
    assert result["success"] is False
    (src,) = _sources(db, user.tenant_id)
    assert src.status == SourceStatus.FAILED


@pytest.mark.parametrize("text,code", [("12345 short", "document_too_short")])
async def test_audit_import_precreated_source_is_failed_not_left_processing(
    db, vector_store, fake_embeddings, text, code
):
    user, _ = _make_tenant_user(db)
    src = KnowledgeSource(
        tenant_id=user.tenant_id,
        name="audit",
        source_type=list(KnowledgeSource.__table__.c.source_type.type.enums)[0],
        content_hash="c" * 64,
        status=SourceStatus.PROCESSING,
    )
    db.add(src)
    db.commit()
    job = _make_job(db, user.tenant_id, text, knowledge_source_id=str(src.id))
    await _run(db, job.id)
    db.expire_all()
    assert db.get(KnowledgeSource, src.id).status == SourceStatus.FAILED
    assert db.get(KnowledgeSource, src.id).failure_code == code
