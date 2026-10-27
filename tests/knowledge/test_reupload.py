"""Uploading the same content again (retry after a failure, re-upload after delete, replace).

Found by the live run: the third upload of identical text raised MultipleResultsFound (500), and
re-uploading text after deleting it hit the (tenant, content_hash, version) unique constraint.
"""

import pytest
from sqlalchemy import select

from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.services.knowledge_service import KnowledgeService
from app.utils.knowledge_failures import KnowledgeError
from tests.api.v1.test_tickets_search import _make_tenant_user
from tests.knowledge.test_text_ingestion import FAQ
from tests.knowledge.test_worker_ingestion import _make_job, _run
from tests.knowledge.vector_fixtures import vector_count


def _all(db, tid):
    return (
        db.execute(
            select(KnowledgeSource)
            .where(KnowledgeSource.tenant_id == tid)
            .order_by(KnowledgeSource.version)
        )
        .scalars()
        .all()
    )


async def _upload(db, tid):
    return await KnowledgeService(db).process_document(
        tenant_id=tid, document_text=FAQ, document_name="faq", source_type=SourceType.FAQ
    )


async def test_retrying_the_same_document_after_repeated_failures_works(
    db, vector_store, fake_embeddings
):
    user, _ = _make_tenant_user(db)
    fake_embeddings["fail"] = True
    for _ in range(3):  # three failed attempts leave three FAILED rows with the same hash
        with pytest.raises(KnowledgeError):
            await _upload(db, user.tenant_id)
    fake_embeddings["fail"] = False
    source = await _upload(db, user.tenant_id)  # used to be MultipleResultsFound
    assert source.status == SourceStatus.ACTIVE
    assert [s.version for s in _all(db, user.tenant_id)] == [1, 2, 3, 4]


async def test_reuploading_after_delete_works(db, vector_store, fake_embeddings):
    user, _ = _make_tenant_user(db)
    first = await _upload(db, user.tenant_id)
    KnowledgeService(db).delete_source(first.id)
    assert vector_count(vector_store) == 0
    second = await _upload(db, user.tenant_id)  # used to be a unique-constraint failure
    assert second.status == SourceStatus.ACTIVE and second.version == 2
    assert vector_count(vector_store) == second.chunk_count


async def test_replacing_a_document_leaves_one_active_copy_and_one_set_of_vectors(
    db, vector_store, fake_embeddings
):
    user, _ = _make_tenant_user(db)
    first = await _upload(db, user.tenant_id)
    second = await _upload(db, user.tenant_id)
    statuses = {s.version: s.status for s in _all(db, user.tenant_id)}
    assert statuses == {1: SourceStatus.INACTIVE, 2: SourceStatus.ACTIVE}
    assert vector_count(vector_store) == second.chunk_count == first.chunk_count


async def test_worker_path_also_retires_the_previous_version(db, vector_store, fake_embeddings):
    user, _ = _make_tenant_user(db)
    for _ in range(2):
        job = _make_job(db, user.tenant_id, FAQ)
        await _run(db, job.id)
    statuses = {s.version: s.status for s in _all(db, user.tenant_id)}
    assert statuses == {1: SourceStatus.INACTIVE, 2: SourceStatus.ACTIVE}
    assert vector_count(vector_store) == _all(db, user.tenant_id)[1].chunk_count
