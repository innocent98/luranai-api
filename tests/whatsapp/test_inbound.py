"""Inbound pipeline: dedupe, AI path, source attribution, 402, owner pause, AI-off, status."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageRole,
    SessionStatus,
)
from app.db.models.visitor import Visitor
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppInboundMessage
from app.services import whatsapp_inbound
from app.services.whatsapp_inbound import (
    OTHER_KIND_REPLY,
    UNSUPPORTED_MEDIA_PLACEHOLDER,
    handle_event,
)
from app.utils.datetime import utcnow_aware, utcnow_naive
from tests.whatsapp.conftest import connect, inbound, make_tenant, owner_typed

pytestmark = pytest.mark.asyncio


@pytest.fixture
def conn(db, fake_sidecar, queue):
    return connect(db, fake_sidecar, make_tenant(db))


def _chat(db, conn) -> WhatsAppChat:
    return db.execute(
        select(WhatsAppChat).where(WhatsAppChat.connection_id == conn.id)
    ).scalar_one()


def _messages(db, session_id):
    db.expire_all()
    return list(
        db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
        ).scalars()
    )


async def test_ai_reply_is_enqueued_and_source_whatsapp_is_persisted(db, conn, queue, llm):
    out = await handle_event(db, inbound(conn))
    assert out == "ai_reply" and llm.calls == 1
    chat = _chat(db, conn)
    session = db.get(ConversationSession, chat.session_id)
    assert session.source == "whatsapp"  # actually written, not coerced to "unknown"
    assert session.source_meta["channel_connection_id"] == str(conn.id)
    assert session.source_meta["provider"] == "linked_device"
    assert session.tenant_id == conn.tenant_id
    assert chat.last_inbound_at is not None and chat.visitor_id is not None
    visitor = db.get(Visitor, chat.visitor_id)
    assert visitor.name == "Ada" and visitor.phone is None and visitor.phone_hash is None
    assert [m.meta.get("channel") for m in _messages(db, session.id)] == ["whatsapp", "whatsapp"]
    assert len(queue.sends) == 1
    sent = queue.sends[0]
    assert sent["text"] == "We open 9am to 5pm, Monday to Friday."
    assert sent["chat_id"] == chat.id and sent["connection_id"] == conn.id
    assert sent["key"].startswith("ai:") and sent["guard"] is True  # re-checked at send time


async def test_contact_ref_is_encrypted_and_lookup_key_peppered(db, conn, queue, llm):
    from sqlalchemy import text

    await handle_event(db, inbound(conn, ref="1234567890@lid"))
    row = db.execute(text("SELECT contact_ref, contact_hmac FROM whatsapp_chats")).mappings().one()
    assert "1234567890" not in row["contact_ref"] and len(row["contact_hmac"]) == 64


async def test_replayed_message_is_deduped(db, conn, queue, llm):
    ev = inbound(conn, mid="MID-REPLAY")
    assert await handle_event(db, ev) == "ai_reply"
    assert await handle_event(db, dict(ev, id="another-delivery-uuid")) == "duplicate"
    assert llm.calls == 1 and len(queue.sends) == 1
    ledger = db.execute(select(WhatsAppInboundMessage)).scalars().all()
    assert [x.provider_message_id for x in ledger] == ["MID-REPLAY"]


async def test_failed_turn_forgets_the_dedupe_row_so_the_retry_is_not_swallowed(
    db, conn, queue, llm, monkeypatch
):
    monkeypatch.setattr(db, "rollback", lambda: None)  # the harness shares one outer transaction
    with patch("app.api.v1.endpoints.chat.send_message", new=AsyncMock(side_effect=RuntimeError)):
        with pytest.raises(RuntimeError):
            await handle_event(db, inbound(conn, mid="MID-RETRY"))
    assert db.execute(select(WhatsAppInboundMessage)).first() is None
    assert await handle_event(db, inbound(conn, mid="MID-RETRY")) == "ai_reply"


async def test_same_message_id_on_two_connections_is_not_a_duplicate(db, fake_sidecar, queue, llm):
    one = connect(db, fake_sidecar, make_tenant(db), "2348000000011")
    two = connect(db, fake_sidecar, make_tenant(db), "2348000000012")
    assert await handle_event(db, inbound(one, mid="SAME")) == "ai_reply"
    assert await handle_event(db, inbound(two, mid="SAME")) == "ai_reply"


async def test_second_message_reuses_the_session(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    await handle_event(db, inbound(conn))
    assert db.execute(select(ConversationSession)).scalars().all().__len__() == 1


async def test_resolved_inside_the_window_is_reopened_not_counted_again(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    first = _chat(db, conn).session_id
    session = db.get(ConversationSession, first)
    session.status = SessionStatus.RESOLVED
    session.last_message_at = utcnow_naive() - timedelta(hours=5, minutes=59)
    db.commit()
    await handle_event(db, inbound(conn, text="one more thing"))
    db.expire_all()
    sessions = db.execute(select(ConversationSession)).scalars().all()
    assert len(sessions) == 1 and _chat(db, conn).session_id == first
    assert sessions[0].status == SessionStatus.ACTIVE


async def test_abandoned_inside_the_window_is_reopened_not_counted_again(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    first = _chat(db, conn).session_id
    session = db.get(ConversationSession, first)
    session.status = SessionStatus.ABANDONED
    session.last_message_at = utcnow_naive() - timedelta(hours=5, minutes=59)
    db.commit()
    await handle_event(db, inbound(conn, text="back"))
    db.expire_all()
    assert len(db.execute(select(ConversationSession)).scalars().all()) == 1
    assert db.get(ConversationSession, first).status == SessionStatus.ACTIVE


@pytest.mark.parametrize("closed", [SessionStatus.RESOLVED, SessionStatus.ABANDONED])
async def test_closed_past_the_window_starts_a_new_counted_conversation(
    db, conn, queue, llm, closed
):
    await handle_event(db, inbound(conn))
    first = _chat(db, conn).session_id
    session = db.get(ConversationSession, first)
    session.status = closed
    session.last_message_at = utcnow_naive() - timedelta(hours=6, minutes=1)
    db.commit()
    await handle_event(db, inbound(conn))
    db.expire_all()
    sessions = db.execute(select(ConversationSession)).scalars().all()
    assert len(sessions) == 2
    assert _chat(db, conn).session_id != first
    assert {s.source for s in sessions} == {"whatsapp"}


async def test_non_text_gets_the_fixed_reply_and_never_the_ai(db, conn, queue, llm):
    out = await handle_event(db, inbound(conn, kind="other", text=""))
    assert out == "other_kind_reply" and llm.calls == 0
    assert [s["text"] for s in queue.sends] == [OTHER_KIND_REPLY]
    # The transcript is complete: a stand-in for the media, then the fixed reply that was sent.
    msgs = _messages(db, _chat(db, conn).session_id)
    assert [(m.role, m.content) for m in msgs] == [
        (MessageRole.USER, UNSUPPORTED_MEDIA_PLACEHOLDER),
        (MessageRole.ASSISTANT, OTHER_KIND_REPLY),
    ]
    assert msgs[1].meta["source"] == "whatsapp_media_fallback"
    assert db.get(ConversationSession, msgs[0].session_id).source == "whatsapp"


async def test_non_text_is_silent_when_ai_is_off(db, conn, queue, llm):
    db.get(WhatsAppConnection, conn.id).ai_enabled = False
    db.commit()
    out = await handle_event(db, inbound(conn, kind="other", text=""))
    assert out == "stored_other_while_paused" and queue.sends == []
    # Nothing is sent, but agents still see that media arrived (and no fake reply is recorded).
    msgs = _messages(db, _chat(db, conn).session_id)
    assert [(m.role, m.content) for m in msgs] == [
        (MessageRole.USER, UNSUPPORTED_MEDIA_PLACEHOLDER)
    ]


async def test_non_text_retry_does_not_double_send(db, conn, queue, llm):
    ev = inbound(conn, kind="other", text="")
    await handle_event(db, ev)
    # What a failed turn does before arq retries: forget the dedupe row.
    db.execute(
        delete(WhatsAppInboundMessage).where(
            WhatsAppInboundMessage.provider_message_id == ev["message_id"]
        )
    )
    db.commit()
    await handle_event(db, ev)
    # Both attempts used the same idempotency key, so the queue sees one logical send.
    assert {s["key"] for s in queue.sends} == {f"other:{ev['message_id']}"}


async def test_unknown_session_key_is_dropped(db, conn, queue, llm):
    ev = inbound(conn)
    ev["session_key"] = "sk_nope"
    assert await handle_event(db, ev) == "dropped_unknown_session"
    assert queue.sends == [] and llm.calls == 0
    assert db.execute(select(WhatsAppChat)).first() is None


async def test_event_is_scoped_to_the_connection_owner(db, fake_sidecar, queue, llm):
    a = connect(db, fake_sidecar, make_tenant(db), "2348000000021")
    b = connect(db, fake_sidecar, make_tenant(db), "2348000000022")
    await handle_event(db, inbound(a))
    assert (
        db.execute(select(WhatsAppChat).where(WhatsAppChat.tenant_id == b.tenant_id)).first()
        is None
    )
    assert {s.tenant_id for s in db.execute(select(ConversationSession)).scalars()} == {a.tenant_id}


async def test_kill_switch_and_revoked_grant_drop_inbound(
    db, conn, queue, llm, wa_settings, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    assert await handle_event(db, inbound(conn)) == "dropped_disabled"
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", True)
    from app.db.models.tenant import TenantCapabilities

    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == conn.tenant_id)
    ).scalar_one()
    caps.can_use_whatsapp = False
    db.commit()
    assert await handle_event(db, inbound(conn)) == "dropped_disabled"
    assert llm.calls == 0 and queue.sends == []


# ------------------------------------------------------------------ 402
async def test_plan_limit_402_stores_sends_nothing_and_alerts_debounced(
    db, fake_sidecar, queue, llm
):
    from app.db.models.tenant import TenantCapabilities

    tenant = make_tenant(db, max_conversations=1000)
    conn = connect(db, fake_sidecar, tenant)
    await handle_event(db, inbound(conn))  # first chat gets a conversation
    queue.sends.clear()
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one()
    caps.max_conversations_per_month = 0  # limit now reached
    db.commit()
    # existing chat, resolved session -> needs a NEW conversation -> 402
    chat = _chat(db, conn)
    old = db.get(ConversationSession, chat.session_id)
    old.status = SessionStatus.RESOLVED
    old.last_message_at = utcnow_naive() - timedelta(hours=7)  # past the reuse window
    db.commit()
    llm.calls = 0
    out = await handle_event(db, inbound(conn, text="hello again"))
    assert out == "plan_limit"
    assert queue.sends == [] and llm.calls == 0  # nothing said to the customer
    db.expire_all()
    first_alert = db.get(WhatsAppConnection, conn.id).last_alert_at
    assert first_alert is not None
    await handle_event(db, inbound(conn, text="still there?"))
    db.expire_all()
    assert db.get(WhatsAppConnection, conn.id).last_alert_at == first_alert  # debounced
    assert queue.sends == []


async def test_non_text_at_the_conversation_cap_opens_nothing_and_says_nothing(
    db, fake_sidecar, queue, llm
):
    """A photo must not be a way around the cap: no new conversation, no reply, tenant alerted."""
    from app.db.models.tenant import TenantCapabilities

    tenant = make_tenant(db, max_conversations=0)  # cap already reached
    conn = connect(db, fake_sidecar, tenant)
    out = await handle_event(db, inbound(conn, kind="other", text=""))
    assert out == "plan_limit"
    assert queue.sends == [] and llm.calls == 0
    db.expire_all()
    assert db.execute(select(ConversationSession)).scalars().all() == []
    assert db.get(WhatsAppConnection, conn.id).last_alert_at is not None
    caps = db.execute(
        select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant.id)
    ).scalar_one()
    assert caps.max_conversations_per_month == 0


async def test_expired_subscription_blocks_non_text_and_the_paused_store_path(
    db, fake_sidecar, queue, llm
):
    tenant = make_tenant(db)
    conn = connect(db, fake_sidecar, tenant)
    await handle_event(db, inbound(conn))  # a conversation exists
    queue.sends.clear()
    from app.db.models.tenant import Tenant

    db.get(Tenant, tenant.id).subscription_status = "canceled"
    conn_row = db.get(WhatsAppConnection, conn.id)
    conn_row.ai_enabled = False  # the store-only path
    db.commit()
    session_id = _chat(db, conn).session_id
    before = len(_messages(db, session_id))
    assert await handle_event(db, inbound(conn, text="hi")) == "plan_limit"
    assert await handle_event(db, inbound(conn, kind="other", text="")) == "plan_limit"
    assert queue.sends == []
    # `_plan_limit` appends to the EXISTING conversation (never opens one); the fallback reply is not recorded.
    after = _messages(db, session_id)
    assert len(after) == before + 2
    assert all(m.role != MessageRole.ASSISTANT for m in after[before:])


async def test_plan_limit_stores_the_message_when_a_conversation_exists(
    db, fake_sidecar, queue, llm
):
    from app.db.models.subscription_plan import SubscriptionPlan  # noqa: F401

    tenant = make_tenant(db)
    conn = connect(db, fake_sidecar, tenant)
    await handle_event(db, inbound(conn))
    queue.sends.clear()
    # Subscription lapses mid-conversation: the pipeline raises 402 for the existing session.
    from app.db.models.tenant import Tenant

    t = db.get(Tenant, tenant.id)
    t.subscription_status = "past_due"
    db.commit()
    out = await handle_event(db, inbound(conn, text="are you there?"))
    assert out == "plan_limit" and queue.sends == []
    contents = [m.content for m in _messages(db, _chat(db, conn).session_id)]
    assert "are you there?" in contents


# ------------------------------------------------------------------ owner pause
async def test_owner_message_pauses_ai_and_is_stored_as_human_agent(db, conn, queue, llm):
    await handle_event(db, inbound(conn))  # customer wrote first
    queue.sends.clear()
    llm.calls = 0
    with patch("app.core.websocket.connection_manager.fanout_event", new=AsyncMock()) as fanout:
        out = await handle_event(db, owner_typed(conn))
    assert out == "paused_stored"
    chat = _chat(db, conn)
    assert chat.human_until is not None
    delta = chat.human_until.replace(tzinfo=None) - utcnow_aware().replace(tzinfo=None)
    assert timedelta(minutes=119) < delta <= timedelta(minutes=120)
    owner = [m for m in _messages(db, chat.session_id) if m.role == MessageRole.HUMAN_AGENT]
    assert len(owner) == 1 and owner[0].meta == {"source": "owner_phone"}
    # shown to agents only: to_customer=True would echo it back to WhatsApp
    kwargs = fanout.await_args.kwargs
    assert kwargs["to_customer"] is False and kwargs["to_agents"] is True
    assert kwargs["message"]["type"] == "agent_message"


async def test_while_paused_customer_messages_are_stored_shown_to_agents_and_not_answered(
    db, conn, queue, llm
):
    await handle_event(db, inbound(conn))
    await handle_event(db, owner_typed(conn))
    queue.sends.clear()
    llm.calls = 0
    with patch("app.api.v1.endpoints.chat.connection_manager.fanout_event", new=AsyncMock()) as fan:
        out = await handle_event(db, inbound(conn, text="Thanks! One more question"))
    assert out == "stored_owner_paused"
    assert llm.calls == 0 and queue.sends == []  # AI silent, nothing auto-replied
    users = [
        m.content for m in _messages(db, _chat(db, conn).session_id) if m.role == MessageRole.USER
    ]
    assert "Thanks! One more question" in users
    assert fan.await_args.kwargs["message"]["type"] == "customer_message"
    assert fan.await_args.kwargs["to_agents"] is True
    # non-text while paused is also silent
    assert (
        await handle_event(db, inbound(conn, kind="other", text="")) == "stored_other_while_paused"
    )
    assert queue.sends == []


async def test_new_owner_message_extends_the_pause(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    await handle_event(db, owner_typed(conn))
    chat = _chat(db, conn)
    chat.human_until = utcnow_aware() + timedelta(minutes=5)  # nearly expired
    db.commit()
    await handle_event(db, owner_typed(conn, text="Still with you"))
    db.expire_all()
    remaining = _chat(db, conn).human_until.replace(tzinfo=None) - utcnow_aware().replace(
        tzinfo=None
    )
    assert remaining > timedelta(minutes=100)


async def test_pause_expires_and_the_ai_answers_again(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    await handle_event(db, owner_typed(conn))
    chat = _chat(db, conn)
    chat.human_until = utcnow_aware() - timedelta(seconds=1)
    db.commit()
    queue.sends.clear()
    llm.calls = 0
    assert await handle_event(db, inbound(conn, text="back to the bot?")) == "ai_reply"
    assert llm.calls == 1 and len(queue.sends) == 1


async def test_owner_message_to_a_contact_never_seen_only_pauses_no_transcript(
    db, conn, queue, llm
):
    out = await handle_event(
        db, owner_typed(conn, ref="999@lid", text="private chat with a friend")
    )
    assert out == "paused_no_transcript"
    assert db.execute(select(ConversationSession)).first() is None  # nothing copied into Luran
    assert db.execute(select(Message)).first() is None
    assert _chat(db, conn).human_until is not None
    # and the AI stays silent if that contact then writes to the number
    llm.calls = 0
    assert await handle_event(db, inbound(conn, ref="999@lid", text="hi")) == "stored_owner_paused"
    assert llm.calls == 0


async def test_repeated_owner_messages_to_a_stranger_still_store_nothing(db, conn, queue, llm):
    for i in range(3):
        out = await handle_event(db, owner_typed(conn, ref="777@lid", text=f"personal {i}"))
        assert out == "paused_no_transcript"
    assert db.execute(select(Message)).first() is None
    assert db.execute(select(ConversationSession)).first() is None


async def test_owner_message_replay_is_deduped(db, conn, queue, llm):
    await handle_event(db, inbound(conn))
    ev = owner_typed(conn, mid="OWN-1")
    assert await handle_event(db, ev) == "paused_stored"
    assert await handle_event(db, ev) == "duplicate"
    owner = [
        m for m in _messages(db, _chat(db, conn).session_id) if m.role == MessageRole.HUMAN_AGENT
    ]
    assert len(owner) == 1


# ------------------------------------------------------------------ AI disabled
async def test_ai_disabled_stores_and_notifies_agents_without_ai(db, conn, queue, llm):
    conn_row = db.get(WhatsAppConnection, conn.id)
    conn_row.ai_enabled = False
    db.commit()
    with patch("app.api.v1.endpoints.chat.connection_manager.fanout_event", new=AsyncMock()) as fan:
        out = await handle_event(db, inbound(conn, text="Need a human please"))
    assert out == "stored_ai_disabled" and llm.calls == 0 and queue.sends == []
    session = db.get(ConversationSession, _chat(db, conn).session_id)
    assert session.source == "whatsapp"
    assert [m.content for m in _messages(db, session.id)] == ["Need a human please"]
    fan.assert_awaited_once()


# ------------------------------------------------------------------ status webhooks
async def test_status_events_flip_connection_and_record_disconnects(db, conn, queue):
    def status(s, code=None):
        return {
            "id": "x",
            "at": "x",
            "session_key": conn.session_key,
            "type": "session.status",
            "status": s,
            **({"code": code} if code is not None else {}),
        }

    assert await handle_event(db, status("connected")) == "status_connected"
    db.expire_all()
    row = db.get(WhatsAppConnection, conn.id)
    assert row.status == "connected" and row.last_connected_at is not None
    await handle_event(db, status("logged_out", 401))
    db.expire_all()
    row = db.get(WhatsAppConnection, conn.id)
    assert row.status == "logged_out"
    assert row.last_disconnect_reason == "logged_out_from_phone" and row.last_disconnect_at
    await handle_event(db, status("reconnecting", 428))
    db.expire_all()
    assert db.get(WhatsAppConnection, conn.id).last_disconnect_reason == "closed_428"
    assert await handle_event(db, status("exploded")) == "ignored_unknown_status"
    assert fake_never_called(queue)


def fake_never_called(queue) -> bool:
    return queue.sends == [] and queue.events == []


def test_module_exposes_no_pairing_code_handling():
    assert not hasattr(whatsapp_inbound, "pairing_code")
