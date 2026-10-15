"""Escalated session + REST send: the customer's text must reach watching agents live.

The widget sends over the socket once escalated, but the signed-in customer chat (and the
widget when its socket is down) send over REST. Before this fix only image messages were
fanned out from the REST path, so agents saw text only after something refreshed the thread.
"""

from unittest.mock import AsyncMock, patch

from app.db.models.conversation import ConversationSession, MessageRole, SessionStatus
from tests.api.v1 import test_chat_image_send as img
from tests.api.v1.test_reply_to_api import WS, _seed_msg, _send


def _escalate(db, sid):
    db.get(ConversationSession, sid).status = SessionStatus.ESCALATED
    db.commit()


def test_escalated_text_send_is_fanned_out_to_agents(client, db):
    sid, _ = img._session(db)
    _escalate(db, sid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = _send(client, sid, "are you still there?")
    assert r.status_code == 200, r.text
    assert ws.fanout_event.await_count == 1
    kwargs = ws.fanout_event.await_args.kwargs
    assert kwargs["to_agents"] is True and kwargs["to_customer"] is False
    frame = kwargs["message"]
    assert frame["type"] == "customer_message"
    assert frame["content"] == "are you still there?"
    assert frame["message_id"] == r.json()["message"]["id"]
    assert frame["attachments"] == []
    assert img._identifier(sid) not in str(frame)


def test_escalated_reply_send_carries_the_quote(client, db):
    sid, _ = img._session(db)
    original = _seed_msg(db, sid, MessageRole.HUMAN_AGENT, "checking now")
    _escalate(db, sid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = _send(client, sid, "thanks!", original.id)
    assert r.status_code == 200, r.text
    frame = ws.fanout_event.await_args.kwargs["message"]
    assert frame["reply_to"]["message_id"] == str(original.id)


def test_active_session_send_is_not_fanned_out_as_customer_message(client, db):
    sid, _ = img._session(db)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = _send(client, sid, "hello")
    assert r.status_code == 200, r.text
    types = [c.kwargs["message"]["type"] for c in ws.fanout_event.await_args_list]
    assert "customer_message" not in types
