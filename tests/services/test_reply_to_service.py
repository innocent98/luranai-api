"""N2 reply-to: validation + snapshot rules (app/services/reply_to.py)."""

from uuid import uuid4

import pytest

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.services.conversation_service import ConversationService
from app.services.reply_to import ReplyToError, resolve_reply_to
from tests.api.v1 import test_chat_image_send as img


def _msg(db, sid, role=MessageRole.USER, content="hello", meta=None):
    m = Message(session_id=sid, role=role, content=content, meta=meta or {})
    db.add(m)
    db.commit()
    return m


def _attach(db, sid, tid, mid, kind=AttachmentKind.IMAGE, status=AttachmentStatus.LINKED):
    extra = {"cloudinary_public_id": f"chat/{uuid4().hex}"}
    a = MessageAttachment(
        session_id=sid,
        tenant_id=tid,
        message_id=mid,
        direction=AttachmentDirection.INBOUND,
        url="https://c/x.jpg",
        content_type="image/png" if kind == AttachmentKind.IMAGE else "video/mp4",
        bytes=10,
        moderation_status=AttachmentModeration.PASSED,
        status=status,
        kind=kind,
        **extra,
    )
    db.add(a)
    db.commit()


def _err(db, session, target_id) -> ReplyToError:
    with pytest.raises(ReplyToError) as exc:
        resolve_reply_to(db, session, target_id)
    return exc.value


def test_snapshot_for_each_sender_type(db):
    sid, _ = img._session(db)
    session = db.get(ConversationSession, sid)
    cases = {
        MessageRole.USER: "customer",
        MessageRole.ASSISTANT: "ai",
        MessageRole.HUMAN_AGENT: "agent",
    }
    for role, expected in cases.items():
        m = _msg(db, sid, role, f"from {expected}")
        snap = resolve_reply_to(db, session, m.id)
        assert snap.message_id == m.id
        assert snap.snapshot == {
            "sender_type": expected,
            "kind": "text",
            "snippet": f"from {expected}",
        }


def test_snippet_truncated_to_200_chars(db):
    sid, _ = img._session(db)
    m = _msg(db, sid, content="x" * 500)
    snap = resolve_reply_to(db, db.get(ConversationSession, sid), m.id)
    assert len(snap.snapshot["snippet"]) == 200
    assert snap.snapshot["snippet"].endswith("…")


def test_exactly_200_chars_is_not_truncated(db):
    sid, _ = img._session(db)
    m = _msg(db, sid, content="y" * 200)
    snap = resolve_reply_to(db, db.get(ConversationSession, sid), m.id)
    assert snap.snapshot["snippet"] == "y" * 200


def test_image_and_video_kinds_with_placeholder_snippet_blank(db):
    sid, tid = img._session(db)
    session = db.get(ConversationSession, sid)
    image = _msg(db, sid, content="[image]")
    _attach(db, sid, tid, image.id)
    video = _msg(db, sid, content="[screen recording]")
    _attach(db, sid, tid, video.id, kind=AttachmentKind.VIDEO)
    captioned = _msg(db, sid, content="see this")
    _attach(db, sid, tid, captioned.id)

    assert resolve_reply_to(db, session, image.id).snapshot == {
        "sender_type": "customer",
        "kind": "image",
        "snippet": "",
    }
    assert resolve_reply_to(db, session, video.id).snapshot["kind"] == "video"
    assert resolve_reply_to(db, session, video.id).snapshot["snippet"] == ""
    assert resolve_reply_to(db, session, captioned.id).snapshot["snippet"] == "see this"


def test_expired_attachment_still_counts_as_image(db):
    sid, tid = img._session(db)
    m = _msg(db, sid, content="[image]")
    _attach(db, sid, tid, m.id, status=AttachmentStatus.EXPIRED)
    snap = resolve_reply_to(db, db.get(ConversationSession, sid), m.id)
    assert snap.snapshot["kind"] == "image"


def test_unknown_target_is_not_found(db):
    sid, _ = img._session(db)
    e = _err(db, db.get(ConversationSession, sid), uuid4())
    assert (e.code, e.status_code) == ("REPLY_TARGET_NOT_FOUND", 422)


def test_other_session_target_is_indistinguishable_from_unknown(db):
    sid, _ = img._session(db)
    other_sid, _ = img._session(db)
    foreign = _msg(db, other_sid)
    e = _err(db, db.get(ConversationSession, sid), foreign.id)
    assert (e.code, e.status_code) == ("REPLY_TARGET_NOT_FOUND", 422)


def test_system_role_and_system_notice_not_quotable(db):
    sid, _ = img._session(db)
    session = db.get(ConversationSession, sid)
    system = _msg(db, sid, MessageRole.SYSTEM, "Agent joined")
    notice = _msg(
        db, sid, MessageRole.ASSISTANT, "ack", meta={"system_notice": "screen_recording_ack"}
    )
    for target in (system, notice):
        e = _err(db, session, target.id)
        assert (e.code, e.status_code) == ("REPLY_TARGET_NOT_QUOTABLE", 422)


def test_voice_line_not_quotable(db):
    sid, _ = img._session(db)
    voice = _msg(db, sid, MessageRole.USER, "spoken words", meta={"channel": "voice"})
    e = _err(db, db.get(ConversationSession, sid), voice.id)
    assert (e.code, e.status_code) == ("REPLY_TARGET_NOT_QUOTABLE", 422)


def test_resolved_session_rejected_before_target_checks(db):
    sid, _ = img._session(db)
    m = _msg(db, sid)
    session = db.get(ConversationSession, sid)
    session.status = SessionStatus.RESOLVED
    db.commit()
    e = _err(db, session, m.id)
    assert (e.code, e.status_code) == ("REPLY_SESSION_CLOSED", 409)


def test_add_message_persists_snapshot_and_exposes_reply_to(db):
    sid, _ = img._session(db)
    session = db.get(ConversationSession, sid)
    original = _msg(db, sid, MessageRole.HUMAN_AGENT, "your order shipped")
    snap = resolve_reply_to(db, session, original.id)
    svc = ConversationService(db)

    reply = svc.add_message(sid, MessageRole.USER, "thanks", reply_to=snap)
    assert reply.reply_to.model_dump(mode="json") == {
        "message_id": str(original.id),
        "sender_type": "agent",
        "kind": "text",
        "snippet": "your order shipped",
    }
    plain = svc.add_message(sid, MessageRole.USER, "also")
    assert plain.reply_to is None
    assert plain.model_dump(mode="json")["reply_to"] is None


def test_queued_reply_is_consumed_by_first_user_message_only(db):
    sid, _ = img._session(db)
    session = db.get(ConversationSession, sid)
    original = _msg(db, sid, MessageRole.ASSISTANT, "quote me")
    svc = ConversationService(db)
    svc.queue_reply_to(resolve_reply_to(db, session, original.id))

    assistant = svc.add_message(sid, MessageRole.ASSISTANT, "reply")
    first_user = svc.add_message(sid, MessageRole.USER, "q1")
    second_user = svc.add_message(sid, MessageRole.USER, "q2")
    assert assistant.reply_to is None
    assert first_user.reply_to is not None and first_user.reply_to.snippet == "quote me"
    assert second_user.reply_to is None


def test_voice_channel_marks_persisted_messages(db):
    from app.services.conversation_service import voice_channel

    sid, _ = img._session(db)
    svc = ConversationService(db)
    token = voice_channel.set(True)
    try:
        spoken = svc.add_message(sid, MessageRole.USER, "hi", meta=None)
        answer = svc.add_message(sid, MessageRole.ASSISTANT, "hello", meta={"chunks_used": 1})
    finally:
        voice_channel.reset(token)
    typed = svc.add_message(sid, MessageRole.USER, "typed")
    assert spoken.meta == {"channel": "voice"}
    assert answer.meta == {"chunks_used": 1, "channel": "voice"}
    assert typed.meta in (None, {})
