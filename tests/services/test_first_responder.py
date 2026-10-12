import uuid

from app.db.models.knowledge import KnowledgeSource, SourceStatus
from app.services.first_responder import (
    count_active_knowledge_sources,
    default_first_responder,
)
from tests.api.v1 import test_chat_image_send as img


def _source(db, tenant_id, status):
    src = KnowledgeSource(
        tenant_id=tenant_id,
        name=f"src-{uuid.uuid4().hex[:6]}",
        source_type=list(KnowledgeSource.__table__.c.source_type.type.enums)[0],
        content_hash=uuid.uuid4().hex,
        status=status,
    )
    db.add(src)
    db.commit()
    return src


def test_count_only_active_sources(db):
    _, tid = img._session(db)
    _source(db, tid, SourceStatus.PROCESSING)
    _source(db, tid, SourceStatus.FAILED)
    _source(db, tid, SourceStatus.INACTIVE)
    assert count_active_knowledge_sources(db, tid) == 0
    _source(db, tid, SourceStatus.ACTIVE)
    assert count_active_knowledge_sources(db, tid) == 1


def test_default_is_human_without_active_sources(db):
    _, tid = img._session(db)
    _source(db, tid, SourceStatus.PROCESSING)
    assert default_first_responder(db, tid) == "human"
    _source(db, tid, SourceStatus.ACTIVE)
    assert default_first_responder(db, tid) == "ai"
