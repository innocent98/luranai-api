"""N2 reply-to over REST: send paths, validation envelopes, history, AI context."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select

from app.api.deps import get_current_user
from app.core.security import create_access_token
from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.db.models.user import UserRole
from app.main import app
from app.schemas.user import UserResponse
from tests.api.v1 import test_chat_image_send as img

WS = "app.api.v1.endpoints.chat.connection_manager"


def _seed_msg(db, sid, role, content, meta=None):
    m = Message(session_id=sid, role=role, content=content, meta=meta or {})
    db.add(m)
    db.commit()
    return SimpleNamespace(id=m.id)  # the request closes the shared session; keep only the id


def _send(client, sid, message, reply_to=None, **extra):
    img._SEEN_IMAGE_URLS.clear()
    p1, p2, p3 = img._patches()
    body = {
        "session_id": str(sid),
        "user_identifier": img._identifier(sid),
        "message": message,
        **extra,
    }
    if reply_to is not None:
        body["reply_to_message_id"] = str(reply_to)
    with p1, p2, p3:
        return client.post("/api/v1/chat/message", json=body)


def _login_agent(tenant_id, role=UserRole.SUPPORT_AGENT):
    now = datetime.now(UTC)
    user = UserResponse(
        id=uuid4(),
        tenant_id=tenant_id,
        email="agent@example.com",
        full_name="Agent Smith",
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _user_row(db, sid, content):
    db.expire_all()  # content is encrypted at rest, so match it in Python
    rows = db.execute(
        select(Message).where(Message.session_id == sid, Message.role == MessageRole.USER)
    ).scalars()
    return next(m for m in rows if m.content == content)


# --- customer REST: /chat/message ------------------------------------------------


def test_customer_reply_persists_snapshot_and_history_carries_reply_to(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.HUMAN_AGENT, "Your refund is on the way")
    r = _send(client, sid, "when will it land?", original.id)
    assert r.status_code == 200, r.text

    row = _user_row(db, sid, "when will it land?")
    assert row.reply_to_message_id == original.id
    assert row.reply_to_snapshot == {
        "sender_type": "agent",
        "kind": "text",
        "snippet": "Your refund is on the way",
    }

    _login_agent(tid, UserRole.TENANT_ADMIN)
    hist = client.get(f"/api/v1/chat/sessions/{sid}/messages")
    assert hist.status_code == 200, hist.text
    by_content = {m["content"]: m for m in hist.json()}
    assert by_content["when will it land?"]["reply_to"] == {
        "message_id": str(original.id),
        "sender_type": "agent",
        "kind": "text",
        "snippet": "Your refund is on the way",
    }
    # null, never absent, on every non-reply
    assert by_content["Your refund is on the way"]["reply_to"] is None
    assert all("reply_to" in m for m in hist.json())
    app.dependency_overrides.clear()


def test_plain_send_has_null_reply_to_everywhere(client, db):
    sid, _ = img._session(db)
    r = _send(client, sid, "what are your opening hours")
    assert r.status_code == 200, r.text
    assert r.json()["message"]["reply_to"] is None
    assert _user_row(db, sid, "what are your opening hours").reply_to_snapshot is None


def test_reply_validation_errors_use_code_message_envelope(client, db):
    sid, _ = img._session(db)
    other_sid, _ = img._session(db)
    foreign = _seed_msg(db, other_sid, MessageRole.ASSISTANT, "elsewhere")
    system = _seed_msg(db, sid, MessageRole.SYSTEM, "Agent joined")
    voice = _seed_msg(db, sid, MessageRole.USER, "spoken", meta={"channel": "voice"})

    missing = _send(client, sid, "hi there", uuid4())
    cross = _send(client, sid, "hi there", foreign.id)
    notice = _send(client, sid, "hi there", system.id)
    spoken = _send(client, sid, "hi there", voice.id)

    for resp, code in (
        (missing, "REPLY_TARGET_NOT_FOUND"),
        (cross, "REPLY_TARGET_NOT_FOUND"),
        (notice, "REPLY_TARGET_NOT_QUOTABLE"),
        (spoken, "REPLY_TARGET_NOT_QUOTABLE"),
    ):
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == code
        assert isinstance(resp.json()["detail"]["message"], str)
    assert cross.json() == missing.json()  # no message-id probing across sessions
    # nothing persisted by the rejected sends
    db.expire_all()
    stored = db.execute(select(Message).where(Message.session_id == sid)).scalars().all()
    assert all(m.content != "hi there" for m in stored)


def test_reply_in_resolved_session_is_409_and_does_not_reopen(client, db):
    sid, _ = img._session(db)
    original = _seed_msg(db, sid, MessageRole.ASSISTANT, "anything else?")
    db.get(ConversationSession, sid).status = SessionStatus.RESOLVED
    db.commit()
    r = _send(client, sid, "yes please", original.id)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "REPLY_SESSION_CLOSED"
    db.expire_all()
    assert db.get(ConversationSession, sid).status == SessionStatus.RESOLVED


def test_reply_without_session_id_is_rejected(client, db):
    _, tid = img._session(db)
    p1, p2, p3 = img._patches()
    with p1, p2, p3:
        r = client.post(
            "/api/v1/chat/message",
            json={
                "tenant_id": str(tid),
                "user_identifier": "brand-new",
                "message": "hello",
                "reply_to_message_id": str(uuid4()),
            },
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "REPLY_TARGET_NOT_FOUND"


def test_reply_with_image_attachment_links_both(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.ASSISTANT, "send a photo of the damage")
    aid = img._att(db, sid, tid)
    r = _send(client, sid, "", original.id, attachment_ids=[str(aid)])
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["user_message_attachments"]] == [str(aid)]
    row = _user_row(db, sid, "[image]")
    assert row.reply_to_message_id == original.id


def test_reply_to_image_message_snapshots_kind_image(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.USER, "[image]")
    aid = img._att(db, sid, tid)
    from app.db.models.conversation import AttachmentStatus, MessageAttachment

    att = db.get(MessageAttachment, aid)
    att.message_id = original.id
    att.status = AttachmentStatus.LINKED
    db.commit()
    assert _send(client, sid, "this one?", original.id).status_code == 200
    assert _user_row(db, sid, "this one?").reply_to_snapshot == {
        "sender_type": "customer",
        "kind": "image",
        "snippet": "",
    }


def test_image_fanout_frame_to_agents_carries_reply_to(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.ASSISTANT, "photo please")
    aid = img._att(db, sid, tid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = _send(client, sid, "here", original.id, attachment_ids=[str(aid)])
    assert r.status_code == 200, r.text
    frame = ws.fanout_event.await_args.kwargs["message"]
    assert frame["type"] == "customer_message"
    assert frame["reply_to"] == {
        "message_id": str(original.id),
        "sender_type": "ai",
        "kind": "text",
        "snippet": "photo please",
    }


# --- AI pipeline -----------------------------------------------------------------


def _spy_user_queries(queries: list[str]):
    from app.services.rag_service import RAGService

    original = RAGService.assemble_prompt

    def spy(self, *args, **kwargs):
        queries.append(kwargs["user_query"])
        return original(self, *args, **kwargs)

    return patch.object(RAGService, "assemble_prompt", spy)


def test_ai_prompt_receives_quoted_snippet_but_not_images(client, db):
    sid, _ = img._session(db)
    original = _seed_msg(db, sid, MessageRole.HUMAN_AGENT, "Standard shipping takes 3 days")
    queries: list[str] = []
    with _spy_user_queries(queries):
        r = _send(client, sid, "what about express shipping", original.id)
    assert r.status_code == 200, r.text
    assert len(queries) == 1
    assert "Standard shipping takes 3 days" in queries[0]
    assert queries[0].endswith("what about express shipping")
    assert "replying to" in queries[0]
    assert img._SEEN_IMAGE_URLS == [None]  # quoted media is never sent to vision


def test_ai_prompt_has_no_quote_block_for_plain_message(client, db):
    sid, _ = img._session(db)
    queries: list[str] = []
    with _spy_user_queries(queries):
        r = _send(client, sid, "what are your opening hours")
    assert r.status_code == 200, r.text
    assert queries == ["what are your opening hours"]


# --- escalated (human owns the thread): reply still stored -------------------------


def test_reply_on_escalated_session_is_persisted_without_ai(client, db):
    sid, _ = img._session(db)
    original = _seed_msg(db, sid, MessageRole.HUMAN_AGENT, "checking now")
    db.get(ConversationSession, sid).status = SessionStatus.ESCALATED
    db.commit()
    r = _send(client, sid, "thanks!", original.id)
    assert r.status_code == 200, r.text
    assert r.json()["message"]["reply_to"]["message_id"] == str(original.id)
    assert r.json()["message"]["reply_to"]["sender_type"] == "agent"


# --- agent REST -------------------------------------------------------------------


def test_agent_rest_reply_response_frame_and_validation(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.USER, "my order is late")
    _login_agent(tid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = client.post(
            f"/api/v1/chat/sessions/{sid}/agent-message",
            json={"message": "looking into it", "reply_to_message_id": str(original.id)},
        )
    assert r.status_code == 201, r.text
    expected = {
        "message_id": str(original.id),
        "sender_type": "customer",
        "kind": "text",
        "snippet": "my order is late",
    }
    assert r.json()["reply_to"] == expected
    assert ws.fanout_event.await_args.kwargs["message"]["reply_to"] == expected

    bad = client.post(
        f"/api/v1/chat/sessions/{sid}/agent-message",
        json={"message": "x", "reply_to_message_id": str(uuid4())},
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "REPLY_TARGET_NOT_FOUND"

    plain = client.post(f"/api/v1/chat/sessions/{sid}/agent-message", json={"message": "hello"})
    assert plain.status_code == 201 and plain.json()["reply_to"] is None
    app.dependency_overrides.clear()


def test_agent_rest_reply_in_resolved_session_rejected(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.USER, "hello")
    db.get(ConversationSession, sid).status = SessionStatus.RESOLVED
    db.commit()
    _login_agent(tid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = client.post(
            f"/api/v1/chat/sessions/{sid}/agent-message",
            json={"message": "x", "reply_to_message_id": str(original.id)},
        )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "REPLY_SESSION_CLOSED"
    app.dependency_overrides.clear()


def test_agent_reply_to_image_attachment_message(client, db):
    sid, tid = img._session(db)
    original = _seed_msg(db, sid, MessageRole.USER, "[image]")
    aid = img._att(db, sid, tid)
    from app.db.models.conversation import AttachmentStatus, MessageAttachment

    att = db.get(MessageAttachment, aid)
    att.message_id, att.status = original.id, AttachmentStatus.LINKED
    db.commit()
    _login_agent(tid)
    with patch(WS) as ws:
        ws.fanout_event = AsyncMock()
        r = client.post(
            f"/api/v1/chat/sessions/{sid}/agent-message",
            json={"message": "got it", "reply_to_message_id": str(original.id)},
        )
    assert r.status_code == 201, r.text
    assert r.json()["reply_to"]["kind"] == "image"
    assert r.json()["reply_to"]["snippet"] == ""
    app.dependency_overrides.clear()


# --- authed customer (/customer/conversations) ---------------------------------


def _customer(db):
    c = CustomerUser(
        id=uuid4(),
        full_name="Ada Customer",
        email=f"{uuid4().hex[:8]}@example.com",
        hashed_password="hashed",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(c)
    db.commit()
    token = create_access_token(
        subject=str(c.id), additional_claims={"email": c.email, "auth_type": "customer"}
    )
    return c, {"Authorization": f"Bearer {token}"}


def test_authed_customer_reply_and_history_shapes(client, db):
    sid, tid = img._session(db)
    customer, headers = _customer(db)
    chat_key = customer.chat_user_key
    session = db.get(ConversationSession, sid)
    session.customer_user_id = customer.id
    session.user_identifier = chat_key
    db.commit()
    original = _seed_msg(db, sid, MessageRole.ASSISTANT, "How can I help?")

    p1, p2, p3 = img._patches()
    with p1, p2, p3:
        r = client.post(
            "/api/v1/customer/conversations/message",
            headers=headers,
            json={
                "session_id": str(sid),
                "message": "what are your opening hours",
                "reply_to_message_id": str(original.id),
            },
        )
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/v1/customer/conversations/{sid}", headers=headers)
    assert detail.status_code == 200, detail.text
    msgs = detail.json()["conversation"]["messages"]
    quoted = [m for m in msgs if m["content"] == "what are your opening hours"][0]
    assert quoted["reply_to"] == {
        "message_id": str(original.id),
        "sender_type": "ai",
        "kind": "text",
        "snippet": "How can I help?",
    }
    assert [m for m in msgs if m["content"] == "How can I help?"][0]["reply_to"] is None

    paged = client.get(f"/api/v1/customer/conversations/{sid}/messages", headers=headers)
    assert paged.status_code == 200
    assert all("reply_to" in m for m in paged.json()["messages"])
    # the customer surface never receives staff-only credential material
    assert chat_key not in str(quoted["reply_to"])

    bad = client.post(
        "/api/v1/customer/conversations/message",
        headers=headers,
        json={
            "session_id": str(sid),
            "message": "x",
            "reply_to_message_id": str(uuid4()),
        },
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "REPLY_TARGET_NOT_FOUND"


# --- agent app transcript + voice marker -----------------------------------------


def test_voice_turn_marks_persisted_lines_as_voice(client, db):
    import asyncio

    from app.api.v1.endpoints import chat as chat_module
    from app.schemas.conversation import MessageRequest
    from app.services.conversation_service import voice_channel

    sid, tid = img._session(db)
    p1, p2, p3 = img._patches()

    async def run():
        token = voice_channel.set(True)
        try:
            return await chat_module.send_message(
                MessageRequest(
                    session_id=sid, tenant_id=tid, message="what are your opening hours"
                ),
                db,
            )
        finally:
            voice_channel.reset(token)

    with p1, p2, p3:
        asyncio.run(run())
    db.expire_all()
    rows = db.execute(select(Message).where(Message.session_id == sid)).scalars().all()
    voiced = [m for m in rows if (m.meta or {}).get("channel") == "voice"]
    assert {m.role for m in voiced} == {MessageRole.USER, MessageRole.ASSISTANT}


# --- widget bootstrap transcript + greeting-text replies -----------------------------


def test_bootstrap_transcript_carries_reply_to(client, db):
    sid, tid = img._session(db)
    visitor_id = uuid4()
    db.get(ConversationSession, sid).visitor_id = visitor_id
    db.commit()
    original = _seed_msg(db, sid, MessageRole.HUMAN_AGENT, "Try restarting")
    r = _send(client, sid, "did that", original.id)
    assert r.status_code == 200, r.text

    from app.services.visitor_service import VisitorService

    summary = VisitorService(db)._get_latest_conversation_summary(visitor_id, tid)
    by_content = {m.content: m for m in summary.messages}
    assert by_content["Try restarting"].reply_to is None
    assert by_content["did that"].reply_to.model_dump(mode="json") == {
        "message_id": str(original.id),
        "sender_type": "agent",
        "kind": "text",
        "snippet": "Try restarting",
    }
    assert all("reply_to" in m.model_dump(mode="json") for m in summary.messages)


def test_greeting_text_reply_is_stored_with_its_quote(client, db):
    sid, _ = img._session(db)
    original = _seed_msg(db, sid, MessageRole.ASSISTANT, "Anything else?")
    r = _send(client, sid, "hi", original.id)
    assert r.status_code == 200, r.text
    row = _user_row(db, sid, "hi")
    assert row.reply_to_message_id == original.id
    assert row.reply_to_snapshot["snippet"] == "Anything else?"
