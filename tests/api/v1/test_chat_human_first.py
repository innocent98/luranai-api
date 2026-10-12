"""N3 — human-only first responder: no AI, no flows, fixed reply + escalation."""

from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.v1.endpoints import chat as chat_module
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.tenant import TenantAIConfig
from app.services.first_responder import HUMAN_FIRST_REASON, HUMAN_FIRST_REPLY
from tests.api.v1 import test_chat_image_send as img


def _human_session(db, **cfg):
    sid, tid = img._session(db)
    conf = db.execute(select(TenantAIConfig).where(TenantAIConfig.tenant_id == tid)).scalar_one()
    conf.first_responder = "human"
    for k, v in cfg.items():
        setattr(conf, k, v)
    db.commit()
    return sid, tid


def test_human_first_escalates_with_fixed_reply_and_no_ai(client, db):
    sid, _ = _human_session(db)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()) as ring:
        r = img._send(client, sid, "my order is late", [])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["should_escalate"] is True
    assert body["escalation_reason"] == HUMAN_FIRST_REASON
    assert body["ticket_id"]
    assert body["message"]["content"] == HUMAN_FIRST_REPLY
    assert img._SEEN_IMAGE_URLS == []  # generation never ran
    ring.assert_awaited_once()
    db.expire_all()
    assert db.get(ConversationSession, sid).status == SessionStatus.ESCALATED


def test_human_first_second_message_hits_escalated_suppression(client, db):
    sid, _ = _human_session(db)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()):
        img._send(client, sid, "first", [])
        r = img._send(client, sid, "second", [])
    assert r.json()["message"]["role"] == "user"
    assert r.json()["should_escalate"] is False


def test_human_first_requires_email_then_escalates_after_capture(client, db):
    sid, _ = _human_session(db, require_email_before_escalation=True)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()) as ring:
        r1 = img._send(client, sid, "help please", [])
        assert r1.json()["should_escalate"] is False
        assert r1.json()["message"]["content"] == chat_module._build_contact_capture_prompt(
            ["email"]
        )
        r2 = img._send(client, sid, "me@example.com", [])
    assert r2.json()["should_escalate"] is True
    assert r2.json()["escalation_reason"] == HUMAN_FIRST_REASON
    ring.assert_awaited_once()
    assert img._SEEN_IMAGE_URLS == []


def test_human_first_welcome_trigger_uses_human_copy_and_no_ticket(client, db):
    sid, _ = _human_session(db)
    r = img._send(client, sid, "__welcome__", [])
    body = r.json()
    assert body["should_escalate"] is False
    assert body["ticket_id"] is None
    assert "someone from our team will reply" in body["message"]["content"]


def test_human_first_skips_flows(client, db):
    sid, _ = _human_session(db)
    with (
        patch.object(chat_module.FlowEngine, "match_flow", return_value=None) as match,
        patch.object(chat_module.FlowEngine, "list_router_flows", return_value=[]) as router,
        patch.object(chat_module, "_ring_agents", new=AsyncMock()),
    ):
        img._send(client, sid, "pricing", [])
        img._send(client, sid, "start", [])
    match.assert_not_called()
    router.assert_not_called()


def test_ai_mode_unchanged(client, db):
    sid, _ = img._session(db)  # first_responder defaults to "ai"
    r = img._send(client, sid, "hello there question", [])
    assert r.status_code == 200
    assert r.json()["message"]["content"] != HUMAN_FIRST_REPLY


def test_human_first_talk_to_human_token_stored_as_readable_text(client, db):
    from app.db.models.conversation import Message, Ticket

    sid, _ = _human_session(db)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()):
        r = img._send(client, sid, chat_module.TALK_TO_HUMAN_TOKEN, [])
    assert r.status_code == 200, r.text
    assert r.json()["should_escalate"] is True
    db.expire_all()
    texts = [
        m.content for m in db.execute(select(Message).where(Message.session_id == sid)).scalars()
    ]
    assert chat_module.TALK_TO_HUMAN_TEXT in texts
    assert chat_module.TALK_TO_HUMAN_TOKEN not in texts
    tickets = db.execute(select(Ticket).where(Ticket.session_id == sid)).scalars().all()
    assert tickets
    assert all(chat_module.TALK_TO_HUMAN_TOKEN not in (t.summary or "") for t in tickets)


def test_human_first_contact_capture_keeps_name_from_request_metadata(client, db):
    sid, _ = _human_session(db, require_email_before_escalation=True)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()):
        r = client.post(
            "/api/v1/chat/message",
            json={
                "session_id": str(sid),
                "message": "help",
                "metadata": {"contact_name": "Ada Lovelace"},
            },
        )
    assert r.status_code == 200, r.text
    db.expire_all()
    meta = db.get(ConversationSession, sid).meta or {}
    assert meta.get("contact_name") == "Ada Lovelace"
    assert meta.get("pending_contact_capture") is True
