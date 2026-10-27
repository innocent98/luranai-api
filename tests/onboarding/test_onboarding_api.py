"""GET /api/v1/tenant/onboarding: the activation checklist and the 'will the AI answer?' blocker."""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.db.models.tenant import TenantAIConfig
from app.services.knowledge_service import KnowledgeService
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user

URL = "/api/v1/tenant/onboarding"

FAQ = (
    "Opening hours\nWe are open Monday to Saturday from 7am to 6pm.\n\n"
    "Delivery\nWe deliver within 10km of the shop for 1,500 naira."
)


def _set_first_responder(db, tenant_id, value):
    conf = db.execute(
        select(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if conf is None:
        conf = TenantAIConfig(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
        db.add(conf)
    conf.first_responder = value
    db.commit()


def _db_source(db, tenant_id, status):
    src = KnowledgeSource(
        tenant_id=tenant_id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        source_type=SourceType.FAQ,
        content_hash=uuid.uuid4().hex,
        status=status,
    )
    db.add(src)
    db.commit()
    return src


async def _indexed_source(db, tenant_id):
    return await KnowledgeService(db).process_document(
        tenant_id=tenant_id, document_text=FAQ, document_name="faq", source_type=SourceType.FAQ
    )


def _conversation(db, tenant_id, roles):
    s = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=f"v-{uuid.uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
    )
    db.add(s)
    db.flush()
    for role in roles:
        db.add(Message(session_id=s.id, role=role, content="hello"))
    db.commit()
    return s


def _get(client, token):
    r = client.get(URL, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_requires_authentication(client):
    assert client.get(URL).status_code in (401, 403)


def test_fresh_tenant_has_nothing_done(client, db, vector_store):
    user, token = _make_tenant_user(db)
    body = _get(client, token)
    assert body == {
        "steps": [
            {
                "key": "knowledge",
                "done": False,
                "detail": {"sources_active": 0, "sources_processing": 0, "sources_failed": 0},
            },
            {
                "key": "widget",
                "done": False,
                "detail": {"first_seen_at": None, "last_seen_at": None, "last_seen_host": None},
            },
            {"key": "test_conversation", "done": False, "detail": {"conversations": 0}},
        ],
        "ai": {"first_responder": "ai", "will_answer": False, "blocker": "no_knowledge"},
        "completed": 0,
        "total": 3,
    }


async def test_indexed_knowledge_marks_step_done_and_ai_will_answer(
    client, db, vector_store, fake_embeddings
):
    user, token = _make_tenant_user(db)
    await _indexed_source(db, user.tenant_id)
    body = _get(client, token)
    knowledge = body["steps"][0]
    assert knowledge["done"] is True
    assert knowledge["detail"]["sources_active"] == 1
    assert body["ai"] == {"first_responder": "ai", "will_answer": True, "blocker": None}
    assert body["completed"] == 1


def test_active_source_without_vectors_is_knowledge_not_indexed(client, db, vector_store):
    """The legacy text-upload rows: ACTIVE in Postgres, nothing in the vector store."""
    user, token = _make_tenant_user(db)
    _db_source(db, user.tenant_id, SourceStatus.ACTIVE)
    body = _get(client, token)
    assert body["steps"][0]["done"] is False
    assert body["steps"][0]["detail"]["sources_active"] == 1
    assert body["ai"]["blocker"] == "knowledge_not_indexed"
    assert body["ai"]["will_answer"] is False


async def test_human_first_blocks_even_with_indexed_knowledge(
    client, db, vector_store, fake_embeddings
):
    user, token = _make_tenant_user(db)
    _set_first_responder(db, user.tenant_id, "human")
    await _indexed_source(db, user.tenant_id)
    body = _get(client, token)
    assert body["steps"][0]["done"] is True
    assert body["ai"] == {
        "first_responder": "human",
        "will_answer": False,
        "blocker": "human_first",
    }


async def test_handoff_mode_answers(client, db, vector_store, fake_embeddings):
    user, token = _make_tenant_user(db)
    _set_first_responder(db, user.tenant_id, "ai_with_handoff")
    await _indexed_source(db, user.tenant_id)
    body = _get(client, token)
    assert body["ai"] == {
        "first_responder": "ai_with_handoff",
        "will_answer": True,
        "blocker": None,
    }


@pytest.mark.parametrize(
    "sources,first_responder,expected",
    [
        ([], "human", "no_knowledge"),  # no knowledge wins over human-first
        ([SourceStatus.ACTIVE], "human", "knowledge_not_indexed"),  # not indexed wins over human
        ([SourceStatus.PROCESSING], "ai", "no_knowledge"),  # nothing ACTIVE yet
        ([SourceStatus.FAILED, SourceStatus.INACTIVE], "ai", "no_knowledge"),
    ],
)
def test_blocker_precedence(client, db, vector_store, sources, first_responder, expected):
    user, token = _make_tenant_user(db)
    _set_first_responder(db, user.tenant_id, first_responder)
    for st in sources:
        _db_source(db, user.tenant_id, st)
    assert _get(client, token)["ai"]["blocker"] == expected


def test_source_counts_by_status(client, db, vector_store):
    user, token = _make_tenant_user(db)
    for st in (
        SourceStatus.ACTIVE,
        SourceStatus.PROCESSING,
        SourceStatus.PROCESSING,
        SourceStatus.FAILED,
        SourceStatus.INACTIVE,
    ):
        _db_source(db, user.tenant_id, st)
    detail = _get(client, token)["steps"][0]["detail"]
    assert detail == {"sources_active": 1, "sources_processing": 2, "sources_failed": 1}


async def test_other_tenants_vectors_do_not_count(client, db, vector_store, fake_embeddings):
    """Tenant B has real vectors; tenant A's ACTIVE (unindexed) source must not borrow them."""
    a, token_a = _make_tenant_user(db)
    b, token_b = _make_tenant_user(db)
    _db_source(db, a.tenant_id, SourceStatus.ACTIVE)
    await _indexed_source(db, b.tenant_id)
    assert _get(client, token_a)["steps"][0]["done"] is False
    assert _get(client, token_a)["ai"]["blocker"] == "knowledge_not_indexed"
    assert _get(client, token_b)["steps"][0]["done"] is True


def test_conversations_counted_per_tenant_and_only_with_a_customer_message(
    client, db, vector_store
):
    a, token_a = _make_tenant_user(db)
    b, token_b = _make_tenant_user(db)
    _conversation(db, a.tenant_id, [MessageRole.USER, MessageRole.ASSISTANT])
    _conversation(db, a.tenant_id, [MessageRole.USER])
    _conversation(db, a.tenant_id, [MessageRole.ASSISTANT, MessageRole.SYSTEM])  # no customer msg
    _conversation(db, b.tenant_id, [MessageRole.USER])
    step_a = _get(client, token_a)["steps"][2]
    step_b = _get(client, token_b)["steps"][2]
    assert step_a == {"key": "test_conversation", "done": True, "detail": {"conversations": 2}}
    assert step_b["detail"]["conversations"] == 1


def test_widget_step_reflects_tenant_columns(client, db, vector_store):
    from datetime import UTC, datetime

    from app.db.models.tenant import Tenant

    user, token = _make_tenant_user(db)
    now = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
    t = db.get(Tenant, user.tenant_id)
    t.widget_first_seen_at = now
    t.widget_last_seen_at = now
    t.widget_last_seen_host = "shop.example.com"
    db.commit()
    step = _get(client, token)["steps"][1]
    assert step["done"] is True
    assert step["detail"]["last_seen_host"] == "shop.example.com"
    assert step["detail"]["first_seen_at"].startswith("2026-10-28T09:30:00")


async def test_completed_counts_every_done_step(client, db, vector_store, fake_embeddings):
    from datetime import UTC, datetime

    from app.db.models.tenant import Tenant

    user, token = _make_tenant_user(db)
    await _indexed_source(db, user.tenant_id)
    t = db.get(Tenant, user.tenant_id)
    t.widget_first_seen_at = datetime.now(UTC)
    db.commit()
    _conversation(db, user.tenant_id, [MessageRole.USER])
    body = _get(client, token)
    assert [s["done"] for s in body["steps"]] == [True, True, True]
    assert body["completed"] == 3 and body["total"] == 3


async def test_vector_store_outage_does_not_500_and_is_not_claimed_as_indexed(
    client, db, vector_store, fake_embeddings, monkeypatch
):
    from app.utils.qdrant_client import qdrant_manager

    user, token = _make_tenant_user(db)
    await _indexed_source(db, user.tenant_id)

    def _boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(qdrant_manager.client, "get_collections", _boom)
    body = _get(client, token)
    assert body["steps"][0]["done"] is False
    assert body["ai"]["blocker"] == "knowledge_not_indexed"
