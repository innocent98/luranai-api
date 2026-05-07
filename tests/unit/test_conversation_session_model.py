import uuid

from app.db.models.conversation import ConversationSession


def test_conversation_session_has_nullable_source():
    col = ConversationSession.__table__.columns["source"]
    assert col.nullable is True
    assert col.type.length == 32


def test_conversation_session_has_nullable_source_meta_jsonb():
    col = ConversationSession.__table__.columns["source_meta"]
    assert col.nullable is True
    # JSONB on Postgres, JSON on SQLite — both render as "JSON" in str()
    assert "JSON" in str(col.type).upper()


def test_conversation_session_source_defaults_to_none(db):
    from app.db.models.tenant import Tenant

    tenant = Tenant(
        name=f"src_default_{uuid.uuid4().hex[:8]}",
        display_name="Source Default Co",
        api_key_hash="hash_src_default",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="visitor:abc",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    assert session.source is None
    assert session.source_meta is None


def test_conversation_session_source_persists(db):
    from app.db.models.tenant import Tenant

    tenant = Tenant(
        name=f"src_persists_{uuid.uuid4().hex[:8]}",
        display_name="Source Persists Co",
        api_key_hash="hash_src_persists",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="visitor:abc",
        source="explore_browse",
        source_meta={"position": 4, "page": 1},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    assert session.source == "explore_browse"
    assert session.source_meta == {"position": 4, "page": 1}
