"""Inbound images: sidecar fetch -> web-chat image pipeline -> AI (vision) or store-only.

Everything external is a stand-in: the sidecar is `tests/fakes/fake_sidecar.py`, moderation and
Cloudinary are `media_stubs`, the model is the `llm` stub (it records the vision call's images).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentStatus,
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppInboundMessage
from app.services.whatsapp_inbound import (
    IMAGE_BLOCKED_REPLY,
    IMAGE_TOO_LARGE_REPLY,
    IMAGE_UNREADABLE_PLACEHOLDER,
    IMAGE_UNREADABLE_REPLY,
    OTHER_KIND_REPLY,
    UNSUPPORTED_MEDIA_PLACEHOLDER,
    handle_event,
)
from app.utils.datetime import ensure_utc_aware, utcnow_aware
from tests.whatsapp.conftest import (
    MediaStubs,
    connect,
    inbound,
    inbound_image,
    make_tenant,
    png_bytes,
)

pytestmark = pytest.mark.asyncio


def MediaStubs_url(n: int) -> str:  # noqa: N802 - reads like the constant it formats
    return MediaStubs.CLOUDINARY_URL.format(n=n)


@pytest.fixture
def conn(db, fake_sidecar, queue):
    return connect(db, fake_sidecar, make_tenant(db))


@pytest.fixture
def fanout():
    """Capture what agents are shown (the websocket fanout), without Redis."""
    with patch(
        "app.api.v1.endpoints.chat.connection_manager.fanout_event", new=AsyncMock()
    ) as mock:
        yield mock


def _chat(db, conn) -> WhatsAppChat:
    db.expire_all()
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


def _attachments(db):
    db.expire_all()
    return list(db.execute(select(MessageAttachment)).scalars())


# ------------------------------------------------------------------ happy path (AI sees it)
async def test_image_goes_through_the_web_chat_pipeline_and_the_ai_sees_it(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    content = png_bytes()
    out = await handle_event(
        db, inbound_image(conn, fake_sidecar, content, caption="Is this the right model?")
    )
    assert out == "ai_reply"
    # moderation ran on the exact bytes BEFORE the upload; one of each
    assert media_stubs.moderated == [content] and len(media_stubs.uploads) == 1
    # the vision call received the stored attachment's URL
    assert llm.calls == 1 and llm.image_urls == [[MediaStubs_url(1)]]
    chat = _chat(db, conn)
    (att,) = _attachments(db)
    assert att.tenant_id == conn.tenant_id and att.session_id == chat.session_id
    assert att.direction == AttachmentDirection.INBOUND and att.status == AttachmentStatus.LINKED
    assert att.bytes == len(content) and att.moderation_status.value == "passed"
    # 30-day retention from the model default
    assert (
        timedelta(days=29) < ensure_utc_aware(att.expires_at) - utcnow_aware() <= timedelta(days=30)
    )
    msgs = _messages(db, chat.session_id)
    assert [(m.role, m.content) for m in msgs] == [
        (MessageRole.USER, "Is this the right model?"),
        (MessageRole.ASSISTANT, "We open 9am to 5pm, Monday to Friday."),
    ]
    assert msgs[0].id == att.message_id
    assert [s["text"] for s in queue.sends] == ["We open 9am to 5pm, Monday to Friday."]
    assert queue.sends[0]["key"].startswith("ai:")
    # the sidecar was asked for exactly this media on exactly this connection's session
    assert ("GET", "/sessions/{key}/media/{media_id}") in fake_sidecar.calls


async def test_image_without_caption_uses_the_image_placeholder(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    assert await handle_event(db, inbound_image(conn, fake_sidecar)) == "ai_reply"
    msgs = _messages(db, _chat(db, conn).session_id)
    assert msgs[0].content == "[image]" and llm.image_urls[0] == [MediaStubs_url(1)]


async def test_the_session_is_whatsapp_sourced_and_the_message_is_channel_tagged(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    await handle_event(db, inbound_image(conn, fake_sidecar))
    session = db.get(ConversationSession, _chat(db, conn).session_id)
    assert session.source == "whatsapp" and session.tenant_id == conn.tenant_id
    assert _messages(db, session.id)[0].meta.get("channel") == "whatsapp"


# ------------------------------------------------------------------ store-only (no AI)
@pytest.mark.parametrize("mode", ["owner_paused", "ai_off", "escalated", "agent_assigned"])
async def test_a_human_owned_or_paused_chat_stores_and_fans_out_without_the_ai(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout, mode
):
    await handle_event(db, inbound(conn, text="hello"))  # opens the conversation (AI answers)
    queue.sends.clear()
    llm.calls = 0
    fanout.reset_mock()
    chat = _chat(db, conn)
    if mode == "owner_paused":
        chat.human_until = utcnow_aware() + timedelta(minutes=30)
    elif mode == "ai_off":
        db.get(WhatsAppConnection, conn.id).ai_enabled = False
    elif mode == "escalated":
        db.get(ConversationSession, chat.session_id).status = SessionStatus.ESCALATED
    else:
        import uuid

        from app.db.models.user import User, UserRole

        agent = User(
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Sam Agent",
            role=UserRole.SUPPORT_AGENT,
            tenant_id=conn.tenant_id,
            is_active=True,
        )
        db.add(agent)
        db.flush()
        db.get(ConversationSession, chat.session_id).agent_id = agent.id
    db.commit()

    out = await handle_event(db, inbound_image(conn, fake_sidecar, caption="see the damage"))
    assert out.startswith("stored_image") and llm.calls == 0 and queue.sends == []
    (att,) = _attachments(db)
    msgs = _messages(db, _chat(db, conn).session_id)
    assert msgs[-1].content == "see the damage" and msgs[-1].role == MessageRole.USER
    assert att.message_id == msgs[-1].id and att.status == AttachmentStatus.LINKED
    # agents were shown the customer's image message
    frame = fanout.await_args.kwargs["message"]
    assert frame["type"] == "customer_message" and frame["message_id"] == str(msgs[-1].id)
    assert [a["id"] for a in frame["attachments"]] == [str(att.id)]
    assert fanout.await_args.kwargs["to_agents"] is True
    assert fanout.await_args.kwargs["to_customer"] is False


async def test_a_session_already_escalated_never_reaches_the_ai_even_if_the_chat_is_open(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    await handle_event(db, inbound(conn, text="hello"))
    queue.sends.clear()
    llm.calls = 0
    db.get(ConversationSession, _chat(db, conn).session_id).status = SessionStatus.ESCALATED
    db.commit()
    await handle_event(db, inbound_image(conn, fake_sidecar))
    assert llm.calls == 0 and queue.sends == []


# ------------------------------------------------------------------ moderation (fail-closed)
async def test_a_moderated_image_stores_nothing_and_the_customer_hears_one_fixed_sentence(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    media_stubs.verdict = (False, "sexual/minors: this is the SECRET moderation reason")
    out = await handle_event(db, inbound_image(conn, fake_sidecar, caption="pic"))
    assert out == "image_blocked"
    assert media_stubs.uploads == [] and _attachments(db) == []  # never uploaded, never stored
    assert db.execute(select(Message)).scalars().all() == []  # not even the caption
    assert db.execute(select(ConversationSession)).scalars().all() == []  # no empty conversation
    assert llm.calls == 0
    assert [s["text"] for s in queue.sends] == [IMAGE_BLOCKED_REPLY]
    assert "SECRET" not in queue.sends[0]["text"] and "sexual" not in queue.sends[0]["text"]
    fanout.assert_not_awaited()


async def test_moderation_outage_fails_closed(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    media_stubs.moderation_raises = True
    out = await handle_event(db, inbound_image(conn, fake_sidecar))
    assert out == "image_blocked" and media_stubs.uploads == [] and _attachments(db) == []
    assert [s["text"] for s in queue.sends] == [IMAGE_BLOCKED_REPLY]


async def test_a_blocked_image_in_a_paused_chat_is_dropped_silently(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    media_stubs.verdict = (False, "nope")
    db.get(WhatsAppConnection, conn.id).ai_enabled = False
    db.commit()
    assert await handle_event(db, inbound_image(conn, fake_sidecar)) == "image_blocked"
    assert queue.sends == [] and _attachments(db) == []


# ------------------------------------------------------------------ integrity of the bytes
async def test_size_mismatch_is_unreadable_and_never_reaches_moderation_or_storage(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    ev = inbound_image(conn, fake_sidecar)
    ev["media"]["bytes"] += 1
    out = await handle_event(db, ev)
    assert out == "image_unreadable" and media_stubs.moderated == [] and media_stubs.uploads == []
    assert [s["text"] for s in queue.sends] == [IMAGE_UNREADABLE_REPLY]
    msgs = _messages(db, _chat(db, conn).session_id)
    assert [(m.role, m.content) for m in msgs][0] == (
        MessageRole.USER,
        IMAGE_UNREADABLE_PLACEHOLDER,
    )
    assert _attachments(db) == [] and llm.calls == 0


async def test_sha256_mismatch_is_unreadable(db, conn, fake_sidecar, queue, llm, media_stubs):
    ev = inbound_image(conn, fake_sidecar)
    ev["media"]["sha256"] = "0" * 64
    assert await handle_event(db, ev) == "image_unreadable"
    assert media_stubs.moderated == [] and _attachments(db) == []


async def test_sidecar_serving_more_bytes_than_the_cap_is_cut_off(
    db, conn, fake_sidecar, queue, llm, media_stubs, wa_settings, monkeypatch
):
    monkeypatch.setattr(wa_settings, "WHATSAPP_MEDIA_MAX_BYTES", 100)
    ev = inbound_image(conn, fake_sidecar, png_bytes((64, 64)))
    assert len(fake_sidecar.media[(conn.session_key, ev["media"]["id"])][0]) > 100
    assert await handle_event(db, ev) == "image_unreadable"
    assert [s["text"] for s in queue.sends] == [IMAGE_TOO_LARGE_REPLY]
    assert media_stubs.moderated == []


async def test_a_file_that_is_not_an_image_is_rejected_by_the_real_format_check(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    ev = inbound_image(conn, fake_sidecar, b"MZ\x90\x00 definitely not a png", mime="image/png")
    assert await handle_event(db, ev) == "image_unreadable"
    assert media_stubs.moderated == [] and media_stubs.uploads == [] and _attachments(db) == []


async def test_a_non_image_mime_is_unreadable_without_calling_the_sidecar(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    ev = inbound_image(conn, fake_sidecar, mime="image/svg+xml")
    assert await handle_event(db, ev) == "image_unreadable"
    assert ("GET", "/sessions/{key}/media/{media_id}") not in fake_sidecar.calls


async def test_media_that_expired_in_the_sidecar_is_a_polite_miss_not_a_crash(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    ev = inbound_image(conn, fake_sidecar)
    fake_sidecar.media.clear()  # TTL passed
    assert await handle_event(db, ev) == "image_unreadable"
    assert [s["text"] for s in queue.sends] == [IMAGE_UNREADABLE_REPLY]
    assert media_stubs.moderated == [] and llm.calls == 0


async def test_media_endpoint_outage_is_retried_not_dropped(
    db, conn, fake_sidecar, queue, llm, media_stubs, monkeypatch
):
    from app.channels.base import SidecarUnavailable

    monkeypatch.setattr(db, "rollback", lambda: None)  # the harness shares one outer transaction
    ev = inbound_image(conn, fake_sidecar)
    fake_sidecar.media_status = 500
    with pytest.raises(SidecarUnavailable):
        await handle_event(db, ev)
    # the dedupe row was forgotten, so the retry is processed (and succeeds once it is back)
    assert db.execute(select(WhatsAppInboundMessage)).first() is None
    fake_sidecar.media_status = None
    assert await handle_event(db, ev) == "ai_reply" and len(media_stubs.uploads) == 1


async def test_a_storage_failure_is_retried_and_leaves_no_orphan(
    db, conn, fake_sidecar, queue, llm, media_stubs, monkeypatch
):
    monkeypatch.setattr(db, "rollback", lambda: None)
    media_stubs.upload_fails = True
    ev = inbound_image(conn, fake_sidecar)
    with pytest.raises(RuntimeError):
        await handle_event(db, ev)
    assert _attachments(db) == [] and queue.sends == []
    media_stubs.upload_fails = False
    assert await handle_event(db, ev) == "ai_reply"
    assert len(_attachments(db)) == 1


async def test_a_failed_turn_after_the_upload_discards_the_unlinked_attachment(
    db, conn, fake_sidecar, queue, llm, media_stubs, monkeypatch
):
    monkeypatch.setattr(db, "rollback", lambda: None)
    with patch("app.api.v1.endpoints.chat.send_message", new=AsyncMock(side_effect=ValueError)):
        with pytest.raises(ValueError):
            await handle_event(db, inbound_image(conn, fake_sidecar))
    assert _attachments(db) == [] and media_stubs.destroyed == ["luran/chat_images/pub1"]


# ------------------------------------------------------------------ idempotency
async def test_a_redelivered_image_is_not_fetched_uploaded_or_stored_twice(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    ev = inbound_image(conn, fake_sidecar, mid="IMG-1")
    assert await handle_event(db, ev) == "ai_reply"
    gets = fake_sidecar.calls.count(("GET", "/sessions/{key}/media/{media_id}"))
    again = dict(ev, id="another-delivery-id")
    assert await handle_event(db, again) == "duplicate"
    assert fake_sidecar.calls.count(("GET", "/sessions/{key}/media/{media_id}")) == gets
    assert len(media_stubs.uploads) == 1 and len(media_stubs.moderated) == 1
    assert len(_attachments(db)) == 1 and llm.calls == 1 and len(queue.sends) == 1


# ------------------------------------------------------------------ tenant isolation
async def test_one_tenants_event_cannot_read_or_attach_another_tenants_media(
    db, fake_sidecar, queue, llm, media_stubs, fanout
):
    a = connect(db, fake_sidecar, make_tenant(db), "2348000000021")
    b = connect(db, fake_sidecar, make_tenant(db), "2348000000022")
    secret = png_bytes((9, 9), "blue")
    b_media = fake_sidecar.add_media(b.session_key, secret)
    # A's session_key + B's media id: the media is looked up under A's own key only
    ev = inbound(a, kind="image", media=b_media)
    assert await handle_event(db, ev) == "image_unreadable"
    assert media_stubs.moderated == [] and _attachments(db) == []
    # B's own event works and the stored attachment belongs to B alone
    assert await handle_event(db, inbound(b, kind="image", media=b_media)) == "ai_reply"
    (att,) = _attachments(db)
    assert att.tenant_id == b.tenant_id != a.tenant_id
    assert media_stubs.uploads[0]["tenant_id"] == b.tenant_id


async def test_the_media_id_cannot_climb_out_of_the_media_path(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    ev = inbound_image(conn, fake_sidecar)
    ev["media"]["id"] = "../../health"
    # (a hostile id never leaves the backend: the adapter refuses it before any request)
    assert await handle_event(db, ev) == "image_unreadable"
    assert fake_sidecar.calls.count(("GET", "/sessions/{key}/media/{media_id}")) == 0


# ------------------------------------------------------------------ plan / grant guards
async def test_expired_subscription_fetches_stores_and_says_nothing(
    db, fake_sidecar, queue, llm, media_stubs
):
    tenant = make_tenant(db)
    conn = connect(db, fake_sidecar, tenant)
    await handle_event(db, inbound(conn))
    queue.sends.clear()
    db.get(Tenant, tenant.id).subscription_status = "canceled"
    db.commit()
    before = len(_messages(db, _chat(db, conn).session_id))
    out = await handle_event(db, inbound_image(conn, fake_sidecar))
    assert out == "plan_limit" and queue.sends == []
    assert media_stubs.moderated == [] and media_stubs.uploads == [] and _attachments(db) == []
    assert ("GET", "/sessions/{key}/media/{media_id}") not in fake_sidecar.calls
    assert len(_messages(db, _chat(db, conn).session_id)) == before + 1  # placeholder text only


async def test_an_image_cannot_open_a_conversation_past_the_monthly_cap(
    db, fake_sidecar, queue, llm, media_stubs
):
    conn = connect(db, fake_sidecar, make_tenant(db, max_conversations=0))
    assert await handle_event(db, inbound_image(conn, fake_sidecar)) == "plan_limit"
    assert db.execute(select(ConversationSession)).scalars().all() == []
    assert queue.sends == [] and media_stubs.uploads == [] and llm.calls == 0


async def test_a_revoked_grant_drops_the_image_before_any_work(
    db, fake_sidecar, queue, llm, media_stubs
):
    conn = connect(db, fake_sidecar, make_tenant(db, can_use=False))
    assert await handle_event(db, inbound_image(conn, fake_sidecar)) == "dropped_disabled"
    assert fake_sidecar.calls.count(("GET", "/sessions/{key}/media/{media_id}")) == 0


# ------------------------------------------------------------------ the false fallback
async def test_other_kind_still_gets_the_fixed_reply_when_nobody_owns_the_chat(
    db, conn, queue, llm
):
    out = await handle_event(db, inbound(conn, kind="other", text=""))
    assert out == "other_kind_reply"
    assert [s["text"] for s in queue.sends] == [OTHER_KIND_REPLY]


@pytest.mark.parametrize("owner", ["escalated", "agent_assigned"])
async def test_other_kind_is_stored_but_not_answered_while_a_human_owns_the_chat(
    db, conn, queue, llm, fanout, owner
):
    await handle_event(db, inbound(conn, text="hello"))
    queue.sends.clear()
    chat = _chat(db, conn)
    session = db.get(ConversationSession, chat.session_id)
    if owner == "escalated":
        session.status = SessionStatus.ESCALATED
    else:
        import uuid

        from app.db.models.user import User, UserRole

        agent = User(
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Sam Agent",
            role=UserRole.SUPPORT_AGENT,
            tenant_id=conn.tenant_id,
            is_active=True,
        )
        db.add(agent)
        db.flush()
        session.agent_id = agent.id
    db.commit()
    out = await handle_event(db, inbound(conn, kind="other", text=""))
    assert out == "stored_other_while_paused" and queue.sends == []
    msgs = _messages(db, session.id)
    assert msgs[-1].content == UNSUPPORTED_MEDIA_PLACEHOLDER and msgs[-1].role == MessageRole.USER
    assert all(m.content != OTHER_KIND_REPLY for m in msgs)
    assert fanout.await_args.kwargs["message"]["type"] == "customer_message"


async def test_an_image_the_sidecar_skipped_as_too_large_gets_the_size_hint_not_the_text_only_line(
    db, conn, queue, llm
):
    ev = inbound(conn, kind="other", text="", skipped="media_too_large")
    assert await handle_event(db, ev) == "other_kind_reply"
    assert [s["text"] for s in queue.sends] == [IMAGE_TOO_LARGE_REPLY]


# ------------------------------------------------------------------ web-chat parity gates
async def test_a_tenant_that_switched_uploads_off_gets_no_images_stored(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    from app.services.whatsapp_inbound import IMAGE_DISABLED_REPLY

    db.get(Tenant, conn.tenant_id).widget_allow_file_upload = False
    db.commit()
    out = await handle_event(db, inbound_image(conn, fake_sidecar))
    assert out == "image_unreadable"
    assert [s["text"] for s in queue.sends] == [IMAGE_DISABLED_REPLY]
    assert media_stubs.moderated == [] and media_stubs.uploads == [] and _attachments(db) == []
    assert ("GET", "/sessions/{key}/media/{media_id}") not in fake_sidecar.calls


async def test_one_conversation_cannot_run_up_unlimited_image_cost(
    db, conn, fake_sidecar, queue, llm, media_stubs, fanout
):
    from app.services.whatsapp_inbound import IMAGE_RATE_MAX

    await handle_event(db, inbound(conn, text="hello"))
    queue.sends.clear()
    session_id = _chat(db, conn).session_id
    for n in range(IMAGE_RATE_MAX):
        _att = MessageAttachment(
            session_id=session_id,
            tenant_id=conn.tenant_id,
            direction=AttachmentDirection.INBOUND,
            status=AttachmentStatus.LINKED,
            moderation_status=__import__(
                "app.db.models.conversation", fromlist=["x"]
            ).AttachmentModeration.PASSED,
            cloudinary_public_id=f"p{n}",
            url="https://res.cloudinary.com/demo/x.jpg",
            content_type="image/png",
            bytes=1,
        )
        db.add(_att)
    db.commit()
    llm.calls = 0
    assert await handle_event(db, inbound_image(conn, fake_sidecar)) == "image_unreadable_silent"
    assert queue.sends == [] and media_stubs.moderated == [] and llm.calls == 0


async def test_a_permanent_pipeline_rejection_is_not_retried(
    db, conn, fake_sidecar, queue, llm, media_stubs, monkeypatch
):
    from fastapi import HTTPException

    monkeypatch.setattr(db, "rollback", lambda: None)
    with patch(
        "app.api.v1.endpoints.chat.send_message",
        new=AsyncMock(side_effect=HTTPException(400, "Invalid or already-used attachment")),
    ):
        out = await handle_event(db, inbound_image(conn, fake_sidecar))
    assert out == "image_unreadable"  # answered once, ledger kept: no 30-try retry storm
    assert db.execute(select(WhatsAppInboundMessage)).first() is not None
    assert _attachments(db) == []


async def test_a_blocked_image_creates_no_visitor_or_conversation(
    db, conn, fake_sidecar, queue, llm, media_stubs
):
    from app.db.models.visitor import Visitor

    media_stubs.verdict = (False, "nope")
    await handle_event(db, inbound_image(conn, fake_sidecar))
    assert db.execute(select(Visitor)).scalars().all() == []
