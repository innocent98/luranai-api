"""Outbound: sender job rules (hard stop, reply-only, retry) and the single fanout hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.websocket import ConnectionManager
from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.whatsapp import WhatsAppChat
from app.services import whatsapp_dispatch
from app.services.whatsapp_outbound import MAX_SEND_TRIES, RetrySend, backoff_seconds, send_text
from app.utils.datetime import utcnow_aware
from tests.whatsapp.conftest import connect, make_tenant

pytestmark = pytest.mark.asyncio


class _NoCloseSession:
    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


def _chat(db, conn, *, last_inbound=True, with_session=False) -> WhatsAppChat:
    session_id = None
    if with_session:
        s = ConversationSession(
            tenant_id=conn.tenant_id,
            user_identifier=f"wa_{uuid4().hex[:8]}",
            status=SessionStatus.ACTIVE,
            source="whatsapp",
        )
        db.add(s)
        db.flush()
        session_id = s.id
    chat = WhatsAppChat(
        tenant_id=conn.tenant_id,
        connection_id=conn.id,
        contact_hmac=uuid4().hex + uuid4().hex,
        contact_ref="1234567890@lid",
        session_id=session_id,
        last_inbound_at=utcnow_aware() if last_inbound else None,
    )
    db.add(chat)
    db.commit()
    return chat


@pytest.fixture
def conn(db, fake_sidecar):
    return connect(db, fake_sidecar, make_tenant(db))


# ------------------------------------------------------------------ sender job
async def test_send_goes_through_the_adapter(db, conn, fake_sidecar):
    chat = _chat(db, conn)
    assert await send_text(db, conn.id, chat.id, "hello", 1) == "sent"
    assert fake_sidecar.sent == [
        {"key": conn.session_key, "to_ref": "1234567890@lid", "text": "hello"}
    ]


@pytest.mark.parametrize(
    "status", ["awaiting_pair", "connecting", "reconnecting", "logged_out", "failed"]
)
async def test_hard_stop_when_not_connected_sidecar_never_called(db, fake_sidecar, status):
    conn = connect(db, fake_sidecar, make_tenant(db), status=status)
    chat = _chat(db, conn)
    assert await send_text(db, conn.id, chat.id, "hi", 1) == "dropped_not_connected"
    assert fake_sidecar.calls == [] and fake_sidecar.sent == []


async def test_reply_only_guard_requires_an_inbound_first(db, conn, fake_sidecar):
    chat = _chat(db, conn, last_inbound=False)
    assert await send_text(db, conn.id, chat.id, "cold outreach", 1) == "dropped_reply_only"
    assert fake_sidecar.sent == []


async def test_sidecar_409_reply_only_and_503_not_connected_are_not_retried(db, conn, fake_sidecar):
    chat = _chat(db, conn)
    fake_sidecar.send_error = (409, "reply_only")
    assert await send_text(db, conn.id, chat.id, "x", 1) == "dropped_reply_only"
    fake_sidecar.send_error = None
    fake_sidecar.sessions[conn.session_key]["status"] = "reconnecting"  # sidecar disagrees
    assert await send_text(db, conn.id, chat.id, "x", 1) == "dropped_not_connected"


async def test_rate_cap_and_outage_retry_with_backoff_then_give_up(db, conn, fake_sidecar):
    chat = _chat(db, conn)
    fake_sidecar.send_error = (429, "rate_cap")
    with pytest.raises(RetrySend) as first:
        await send_text(db, conn.id, chat.id, "x", 1)
    assert first.value.defer_s == backoff_seconds(1)
    assert backoff_seconds(2) > backoff_seconds(1) and backoff_seconds(99) == backoff_seconds(5)
    fake_sidecar.send_error = (500, "boom")
    with pytest.raises(RetrySend):
        await send_text(db, conn.id, chat.id, "x", 2)
    assert await send_text(db, conn.id, chat.id, "x", MAX_SEND_TRIES) == "dropped_gave_up"


async def test_guarded_replies_are_dropped_if_the_owner_took_over_or_ai_went_off(
    db, conn, fake_sidecar
):
    from datetime import timedelta

    chat = _chat(db, conn)
    chat.human_until = utcnow_aware() + timedelta(minutes=30)  # owner typed while the AI thought
    db.commit()
    assert (
        await send_text(db, conn.id, chat.id, "stale AI answer", 1, guard=True) == "dropped_paused"
    )
    # agent / ticket frames are not guarded: a human wrote them
    assert await send_text(db, conn.id, chat.id, "agent says hi", 1, guard=False) == "sent"
    chat.human_until = None
    db.get(type(conn), conn.id).ai_enabled = False
    db.commit()
    assert await send_text(db, conn.id, chat.id, "x", 1, guard=True) == "dropped_ai_disabled"
    assert [m["text"] for m in fake_sidecar.sent] == ["agent says hi"]


async def test_revoked_grant_stops_sending(db, conn, fake_sidecar):
    from app.db.models.tenant import TenantCapabilities
    from sqlalchemy import select as _select

    chat = _chat(db, conn)
    caps = db.execute(
        _select(TenantCapabilities).where(TenantCapabilities.tenant_id == conn.tenant_id)
    ).scalar_one()
    caps.can_use_whatsapp = False
    db.commit()
    assert await send_text(db, conn.id, chat.id, "x", 1) == "dropped_disabled"


async def test_kill_switch_stops_sending(db, conn, fake_sidecar, wa_settings, monkeypatch):
    chat = _chat(db, conn)
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    assert await send_text(db, conn.id, chat.id, "x", 1) == "dropped_disabled"
    assert fake_sidecar.sent == []


async def test_long_text_is_truncated_to_the_channel_limit(db, conn, fake_sidecar):
    chat = _chat(db, conn)
    await send_text(db, conn.id, chat.id, "a" * 9000, 1)
    assert len(fake_sidecar.sent[0]["text"]) == 4096


# ------------------------------------------------------------------ fanout hook
@pytest.fixture
def manager(monkeypatch):
    mgr = ConnectionManager()
    monkeypatch.setattr(mgr, "_publish_event", AsyncMock())  # no Redis in unit tests
    return mgr


@pytest.fixture
def wa_session(db, conn, queue, monkeypatch):
    chat = _chat(db, conn, with_session=True)
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _NoCloseSession(db))
    return chat


AGENT_FRAME = {
    "type": "agent_message",
    "message_id": "m-1",
    "content": "Hi, this is Sam from support.",
    "agent_name": "Sam",
    "attachments": [],
}


async def test_fanout_forwards_exactly_once(db, conn, queue, manager, wa_session):
    await manager.fanout_event(wa_session.session_id, AGENT_FRAME, to_customer=True, to_agents=True)
    assert len(queue.sends) == 1
    sent = queue.sends[0]
    assert sent["text"] == "Hi, this is Sam from support." and sent["key"] == "agent_message:m-1"
    assert sent["connection_id"] == conn.id and sent["chat_id"] == wa_session.id


async def test_remote_worker_delivery_does_not_refire(db, queue, manager, wa_session):
    """`_deliver_event_local` is what every OTHER worker runs after the Redis publish."""
    await manager._deliver_event_local(
        session_id=wa_session.session_id, message=AGENT_FRAME, to_customer=True, to_agents=True
    )
    assert queue.sends == []


async def test_agent_only_and_ws_only_frames_are_not_forwarded(db, queue, manager, wa_session):
    sid = wa_session.session_id
    await manager.fanout_event(sid, AGENT_FRAME, to_customer=False, to_agents=True)
    await manager.fanout_event(sid, {"type": "customer_message", "content": "x"}, True, True)
    await manager.fanout_event(sid, {"type": "typing", "is_typing": True}, True, False)
    await manager.fanout_event(sid, {"type": "agent_joined", "agent_name": "Sam"}, True, False)
    await manager.send_typing_indicator(sid, True, "Sam", to_customer=True)  # WS-only path
    assert queue.sends == []


async def test_non_whatsapp_session_is_not_forwarded(db, queue, manager, wa_session, monkeypatch):
    other = ConversationSession(
        tenant_id=wa_session.tenant_id, user_identifier="web", status=SessionStatus.ACTIVE
    )
    db.add(other)
    db.commit()
    await manager.fanout_event(other.id, AGENT_FRAME, to_customer=True, to_agents=True)
    assert queue.sends == []


async def test_ticket_and_missed_frames_map_to_fixed_or_given_text(db, queue, manager, wa_session):
    sid = wa_session.session_id
    await manager.fanout_event(sid, {"type": "ticket_resolved", "ticket_id": "t1"}, True, True)
    await manager.fanout_event(sid, {"type": "ticket_closed", "ticket_id": "t1"}, True, True)
    await manager.fanout_event(
        sid,
        {"type": "ticket_missed", "message_id": "m9", "content": "Sorry for the wait"},
        True,
        False,
    )
    assert [s["key"] for s in queue.sends] == [
        "ticket_resolved:t1",
        "ticket_closed:t1",
        "ticket_missed:m9",
    ]
    assert queue.sends[0]["text"] == whatsapp_dispatch.TICKET_RESOLVED_TEXT
    assert queue.sends[2]["text"] == "Sorry for the wait"


async def test_hook_is_inert_when_whatsapp_is_off_and_never_breaks_fanout(
    db, queue, manager, wa_session, wa_settings, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    await manager.fanout_event(wa_session.session_id, AGENT_FRAME, True, True)
    assert queue.sends == []
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", True)
    with patch.object(whatsapp_dispatch, "_find_chat", side_effect=RuntimeError("db down")):
        await manager.fanout_event(wa_session.session_id, AGENT_FRAME, True, True)  # no raise


async def test_agent_reply_endpoint_reaches_whatsapp_end_to_end(
    client, db, conn, queue, wa_session
):
    """The real REST agent endpoint -> the real fanout_event -> one WhatsApp enqueue."""
    from app.api.deps import get_current_user  # noqa: F401
    from app.main import app
    from app.db.models.user import UserRole
    from app.schemas.user import UserResponse
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    agent = UserResponse(
        id=uuid4(),
        tenant_id=conn.tenant_id,
        email="a@example.com",
        full_name="Agent Smith",
        role=UserRole.SUPPORT_AGENT,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_user] = lambda: agent
    with patch("app.core.websocket.ConnectionManager._publish_event", new=AsyncMock()):
        r = client.post(
            f"/api/v1/chat/sessions/{wa_session.session_id}/agent-message",
            json={"message": "On it, one moment."},
        )
    assert r.status_code == 201, r.text
    assert [s["text"] for s in queue.sends] == ["On it, one moment."]
