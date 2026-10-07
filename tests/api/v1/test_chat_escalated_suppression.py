"""POST /api/v1/chat/message on an ESCALATED session must not generate an AI reply.

When a human agent is handling a conversation (SessionStatus.ESCALATED), a REST send
(text or image) must persist the customer's user message and return a valid ChatResponse,
but must NOT run RAG/model generation. The generation stub in the shared harness returns
"Nice picture." and records calls, so a leaked AI reply is directly observable here.
"""

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)

from tests.api.v1 import test_chat_image_send as img


def _escalated_session(db):
    sid, tid = img._session(db)
    session = db.get(ConversationSession, sid)
    session.status = SessionStatus.ESCALATED
    db.commit()
    return sid, tid


def _roles(db, sid):
    return db.execute(select(Message.role).where(Message.session_id == sid)).scalars().all()


def test_escalated_text_send_persists_user_message_without_ai_reply(client, db):
    sid, _ = _escalated_session(db)

    r = img._send(client, sid, "hey are you there", [])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["should_escalate"] is False
    assert body["escalation_reason"] is None
    assert body["ticket_id"] is None
    # Response echoes the persisted USER message; there is deliberately no assistant reply.
    assert body["message"]["role"] == "user"
    assert body["message"]["content"] == "hey are you there"
    # Only the user message exists — generation was skipped (no ASSISTANT row).
    assert _roles(db, sid) == [MessageRole.USER]
    assert img._SEEN_IMAGE_URLS == []


def test_escalated_image_only_send_links_attachment_without_ai_reply(client, db):
    sid, tid = _escalated_session(db)
    aid = img._att(db, sid, tid)

    r = img._send(client, sid, "", [str(aid)])

    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["user_message_attachments"]] == [str(aid)]
    # Attachment linked to the persisted user message.
    row = db.execute(select(MessageAttachment).where(MessageAttachment.id == aid)).scalar_one()
    db.refresh(row)
    assert row.status == AttachmentStatus.LINKED
    assert row.message_id is not None
    user_msg = db.execute(select(Message).where(Message.id == row.message_id)).scalar_one()
    assert user_msg.role == MessageRole.USER
    assert user_msg.content == "[image]"
    # No AI reply, generation never invoked, no crash on empty message text.
    assert MessageRole.ASSISTANT not in _roles(db, sid)
    assert img._SEEN_IMAGE_URLS == []


def test_escalated_image_send_fans_out_to_agents_but_no_ai_reply(client, db):
    from unittest.mock import AsyncMock, patch

    from app.api.v1.endpoints import chat as chat_module

    sid, tid = _escalated_session(db)
    aid = img._att(db, sid, tid)

    fanout = AsyncMock()
    with patch.object(chat_module.connection_manager, "fanout_event", new=fanout):
        r = img._send(client, sid, "look at this", [str(aid)])

    assert r.status_code == 200, r.text
    frames = [
        c.kwargs for c in fanout.call_args_list if c.kwargs["message"]["type"] == "customer_message"
    ]
    assert len(frames) == 1
    assert frames[0]["to_agents"] is True and frames[0]["to_customer"] is False
    assert MessageRole.ASSISTANT not in _roles(db, sid)


def test_active_session_still_gets_ai_reply(client, db):
    """Regression: the normal (ACTIVE) path still generates an assistant reply."""
    sid, _ = img._session(db)  # ACTIVE by default

    # A normal support query (not a welcome trigger) exercises the real RAG/generation path.
    r = img._send(client, sid, "what are your opening hours", [])

    assert r.status_code == 200, r.text
    roles = _roles(db, sid)
    assert MessageRole.USER in roles
    assert MessageRole.ASSISTANT in roles
    # Generation stub was invoked (text-only -> image_urls=None recorded).
    assert img._SEEN_IMAGE_URLS == [None]
