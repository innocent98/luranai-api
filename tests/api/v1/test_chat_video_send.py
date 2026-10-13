"""POST /chat/message with a video attachment escalates and never reaches the AI."""

from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentKind,
    ConversationSession,
    Message,
    MessageAttachment,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.tenant import TenantAIConfig
from app.services.first_responder import SCREEN_RECORDING_ACK, SCREEN_RECORDING_REASON
from tests.api.v1 import test_chat_image_send as img

RING = "app.api.v1.endpoints.chat._ring_agents"
EMAILS = "app.api.v1.endpoints.chat._queue_escalation_emails"


def _video(db, sid, tid):
    aid = img._att(db, sid, tid)
    db.query(MessageAttachment).filter_by(id=aid).update(
        {
            "kind": AttachmentKind.VIDEO,
            "content_type": "video/mp4",
            "url": "https://c/v.mp4",
            "cloudinary_public_id": f"vid/{aid}",
            "duration_ms": 5000,
        }
    )
    db.commit()
    return aid


def _send(client, sid, message, ids):
    with patch(RING, new=AsyncMock()) as ring, patch(EMAILS):
        r = img._send(client, sid, message, [str(i) for i in ids])
    return r, ring


def test_video_escalates_with_ack_and_no_ai(client, db):
    sid, tid = img._session(db)
    vid = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [vid])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["content"] == SCREEN_RECORDING_ACK
    assert body["should_escalate"] is True and body["escalation_reason"] == SCREEN_RECORDING_REASON
    assert body["ticket_id"]
    assert body["user_message_attachments"][0]["kind"] == "video"
    assert img._SEEN_IMAGE_URLS == []  # generation never ran
    ring.assert_awaited_once()
    s = db.get(ConversationSession, sid)
    db.refresh(s)
    assert s.status == SessionStatus.ESCALATED
    row = db.get(MessageAttachment, vid)
    db.refresh(row)
    user_msg = db.get(Message, row.message_id)
    assert user_msg.content == "[screen recording]"


def test_video_with_text_keeps_text(client, db):
    sid, tid = img._session(db)
    vid = _video(db, sid, tid)
    r, _ = _send(client, sid, "the button does nothing", [vid])
    row = db.get(MessageAttachment, vid)
    db.refresh(row)
    assert db.get(Message, row.message_id).content == "the button does nothing"


def test_two_videos_rejected(client, db):
    sid, tid = img._session(db)
    a, b = _video(db, sid, tid), _video(db, sid, tid)
    r, _ = _send(client, sid, "", [a, b])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_videos"
    assert db.get(MessageAttachment, a).message_id is None


def test_video_plus_image_allowed_and_image_not_sent_to_ai(client, db):
    sid, tid = img._session(db)
    vid, pic = _video(db, sid, tid), img._att(db, sid, tid)
    r, _ = _send(client, sid, "", [vid, pic])
    assert r.status_code == 200 and r.json()["message"]["content"] == SCREEN_RECORDING_ACK
    assert img._SEEN_IMAGE_URLS == []


def test_contact_capture_required_persists_video_then_prompts(client, db):
    sid, tid = img._session(db)
    db.query(TenantAIConfig).filter_by(tenant_id=tid).update(
        {"require_email_before_escalation": True}
    )
    db.commit()
    vid = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [vid])
    assert r.status_code == 200
    assert r.json()["should_escalate"] is False
    assert r.json()["message"]["meta"]["contact_capture_required"] is True
    ring.assert_not_awaited()
    assert db.get(MessageAttachment, vid).message_id is not None
    s = db.get(ConversationSession, sid)
    db.refresh(s)
    assert s.meta["contact_capture_context"]["reason"] == SCREEN_RECORDING_REASON


def test_already_escalated_in_progress_no_ack_no_ring(client, db):
    sid, tid = img._session(db)
    first = _video(db, sid, tid)
    _send(client, sid, "", [first])
    t = db.execute(select(Ticket).where(Ticket.session_id == sid)).scalar_one()
    t.status = TicketStatus.IN_PROGRESS
    db.commit()
    second = _video(db, sid, tid)
    r, ring = _send(client, sid, "", [second])
    assert r.status_code == 200
    assert r.json()["message"]["role"] == "user"  # echo, no ACK
    ring.assert_not_awaited()
    assert len(db.execute(select(Ticket).where(Ticket.session_id == sid)).scalars().all()) == 1


def test_already_escalated_missed_rerings(client, db):
    sid, tid = img._session(db)
    _send(client, sid, "", [_video(db, sid, tid)])
    t = db.execute(select(Ticket).where(Ticket.session_id == sid)).scalar_one()
    t.status = TicketStatus.MISSED
    t.assigned_to = None
    db.commit()
    r, ring = _send(client, sid, "", [_video(db, sid, tid)])
    assert r.status_code == 200
    ring.assert_awaited_once()


def test_human_first_tenant_gets_video_ack(client, db):
    sid, tid = img._session(db)
    db.query(TenantAIConfig).filter_by(tenant_id=tid).update({"first_responder": "human"})
    db.commit()
    r, _ = _send(client, sid, "", [_video(db, sid, tid)])
    assert r.json()["message"]["content"] == SCREEN_RECORDING_ACK
