"""POST /knowledge/upload must apply the same guards as the file endpoint.

Review finding: the text endpoint had no plan-cap, subscription, rate-limit or size guard, and
now that it really indexes, that made it an uncapped way to add knowledge.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.db.models.tenant import Tenant, TenantCapabilities
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user
from tests.knowledge.test_text_ingestion import FAQ
from tests.knowledge.vector_fixtures import vector_count

UPLOAD = "/api/v1/knowledge/upload"


def _caps(db, tid, **kw):
    db.add(TenantCapabilities(tenant_id=tid, can_upload_knowledge=True, **kw))
    db.commit()


def _post(client, token, tid, text=FAQ):
    return client.post(
        UPLOAD,
        json={"tenant_id": str(tid), "document_name": "faq", "document_text": text},
        headers=_auth(token),
    )


def test_text_upload_refused_at_the_plan_cap(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    tid = user.tenant_id
    _caps(db, tid, max_knowledge_sources=1)
    assert _post(client, token, tid).status_code == 201
    r = _post(client, token, tid, text="A second, different document about delivery times.")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "knowledge_limit_reached"
    assert vector_count(vector_store) > 0
    live = (
        db.execute(select(KnowledgeSource).where(KnowledgeSource.tenant_id == tid)).scalars().all()
    )
    assert len([s for s in live if s.status == SourceStatus.ACTIVE]) == 1


def test_failed_sources_do_not_block_a_text_retry_at_the_cap(
    client, db, vector_store, fake_embeddings
):
    user, token = _make_tenant_user(db)
    tid = user.tenant_id
    _caps(db, tid, max_knowledge_sources=1)
    db.add(
        KnowledgeSource(
            tenant_id=tid,
            name="f",
            source_type=SourceType.FAQ,
            content_hash="f" * 64,
            status=SourceStatus.FAILED,
        )
    )
    db.commit()
    assert _post(client, token, tid).status_code == 201


def test_text_upload_refused_when_uploads_are_disabled(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    db.add(TenantCapabilities(tenant_id=user.tenant_id, can_upload_knowledge=False))
    db.commit()
    assert _post(client, token, user.tenant_id).status_code == 403


def test_text_upload_refused_after_trial_expiry(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    tid = user.tenant_id
    _caps(db, tid, max_knowledge_sources=5)
    t = db.get(Tenant, tid)
    t.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
    db.commit()
    r = _post(client, token, tid)
    assert r.status_code == 402, r.text
    assert vector_count(vector_store) == 0


def test_oversize_text_is_rejected_before_any_work(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    _caps(db, user.tenant_id, max_knowledge_sources=5)
    r = _post(client, token, user.tenant_id, text="word " * (3 * 1024 * 1024))
    assert r.status_code == 422, r.text
    assert vector_count(vector_store) == 0
