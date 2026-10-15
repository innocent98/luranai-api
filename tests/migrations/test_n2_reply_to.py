"""N2: the self-FK really is ON DELETE SET NULL (Postgres), and the quote outlives its original."""

from uuid import uuid4

from sqlalchemy import delete, inspect

from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.tenant import Tenant
from app.schemas.conversation import MessageResponse


def _session(db_session):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()
    session = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db_session.add(session)
    db_session.flush()
    return session


def test_deleting_the_original_nulls_message_id_but_keeps_the_quote(db_session):
    session = _session(db_session)
    original = Message(session_id=session.id, role=MessageRole.ASSISTANT, content="orig")
    db_session.add(original)
    db_session.flush()
    reply = Message(
        session_id=session.id,
        role=MessageRole.USER,
        content="re",
        reply_to_message_id=original.id,
        reply_to_snapshot={"sender_type": "ai", "kind": "text", "snippet": "orig"},
    )
    db_session.add(reply)
    db_session.flush()
    reply_id = reply.id

    db_session.execute(delete(Message).where(Message.id == original.id))
    db_session.expire_all()

    fresh = db_session.get(Message, reply_id)
    assert fresh is not None
    assert fresh.reply_to_message_id is None
    assert MessageResponse.model_validate(fresh).reply_to.model_dump(mode="json") == {
        "message_id": None,
        "sender_type": "ai",
        "kind": "text",
        "snippet": "orig",
    }


def test_reply_to_columns_and_index_exist(db_session):
    insp = inspect(db_session.get_bind())
    cols = {c["name"]: c for c in insp.get_columns("messages")}
    assert cols["reply_to_message_id"]["nullable"] is True
    assert cols["reply_to_snapshot"]["nullable"] is True
    fk = [
        f
        for f in insp.get_foreign_keys("messages")
        if f["constrained_columns"] == ["reply_to_message_id"]
    ]
    assert fk and fk[0]["options"].get("ondelete") == "SET NULL"
