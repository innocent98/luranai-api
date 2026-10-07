"""ConversationService.link_attachments validation + linking."""

from uuid import uuid4

import pytest

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.tenant import Tenant
from app.services.conversation_service import ConversationService


def _session(db):
    prefix = uuid4().hex[:8]
    tenant = Tenant(
        id=uuid4(),
        name=f"t_{prefix}",
        display_name=f"T {prefix}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(tenant)
    db.flush()
    s = ConversationSession(
        tenant_id=tenant.id, user_identifier=f"v-{prefix}", status=SessionStatus.ACTIVE
    )
    db.add(s)
    db.commit()
    return s


def _att(db, session, **kw):
    a = MessageAttachment(
        session_id=session.id,
        tenant_id=session.tenant_id,
        direction=AttachmentDirection.INBOUND,
        cloudinary_public_id="chat/x",
        url="https://c/x.jpg",
        content_type="image/png",
        width=4,
        height=3,
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=kw.pop("status", AttachmentStatus.UPLOADED),
        **kw,
    )
    db.add(a)
    db.commit()
    return a


def test_links_valid_attachment(db):
    s = _session(db)
    a = _att(db, s)
    svc = ConversationService(db)
    msg = svc.add_message(s.id, MessageRole.USER, "hi")
    rows = svc.link_attachments(s.id, msg.id, [a.id])
    db.commit()
    assert [r.id for r in rows] == [a.id]
    db.refresh(a)
    assert a.message_id == msg.id
    assert a.status == AttachmentStatus.LINKED


def test_unknown_id_rejected(db):
    s = _session(db)
    svc = ConversationService(db)
    msg = svc.add_message(s.id, MessageRole.USER, "hi")
    with pytest.raises(ValueError):
        svc.link_attachments(s.id, msg.id, [uuid4()])


def test_foreign_session_rejected(db):
    s, other = _session(db), _session(db)
    foreign = _att(db, other)
    svc = ConversationService(db)
    msg = svc.add_message(s.id, MessageRole.USER, "hi")
    with pytest.raises(ValueError):
        svc.link_attachments(s.id, msg.id, [foreign.id])


def test_already_linked_rejected_and_nothing_linked(db):
    s = _session(db)
    good = _att(db, s)
    svc = ConversationService(db)
    first = svc.add_message(s.id, MessageRole.USER, "one")
    used = _att(db, s)
    svc.link_attachments(s.id, first.id, [used.id])
    db.commit()
    msg = svc.add_message(s.id, MessageRole.USER, "two")
    with pytest.raises(ValueError):
        svc.link_attachments(s.id, msg.id, [good.id, used.id])
    db.refresh(good)
    assert good.message_id is None
    assert good.status == AttachmentStatus.UPLOADED


def test_history_returns_linked_attachments(db):
    from app.db.models.conversation import Message

    s = _session(db)
    msg = Message(session_id=s.id, role=MessageRole.USER, content="pic", meta={})
    db.add(msg)
    db.commit()
    att = _att(db, s, message_id=msg.id, status=AttachmentStatus.LINKED)
    plain = Message(session_id=s.id, role=MessageRole.ASSISTANT, content="ok", meta={})
    db.add(plain)
    db.commit()

    svc = ConversationService(db)
    history = {m.id: m for m in svc.get_session_messages(s.id)}
    assert [a.id for a in history[msg.id].attachments] == [att.id]
    assert history[msg.id].attachments[0].url == "https://c/x.jpg"
    assert history[plain.id].attachments == []
    paginated, _ = svc.get_session_messages_paginated(s.id)
    assert any(a.id == att.id for m in paginated for a in m.attachments)


def test_too_many_attachments_rejected(db):
    s = _session(db)
    ids = [_att(db, s).id for _ in range(4)]
    svc = ConversationService(db)
    with pytest.raises(ValueError, match="Too many attachments"):
        svc.get_linkable_attachments(s.id, ids)
    msg = svc.add_message(s.id, MessageRole.USER, "hi")
    with pytest.raises(ValueError, match="Too many attachments"):
        svc.link_attachments(s.id, msg.id, ids)
    assert len(svc.get_linkable_attachments(s.id, ids[:3])) == 3
