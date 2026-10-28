"""Outbound agent images: frame -> forward plan -> sender job -> sidecar `send {image}`."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.channels.linked_device import LinkedDeviceAdapter
from app.core.encryption import encrypt_field
from app.core.websocket import ConnectionManager
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentModeration,
    AttachmentStatus,
    ConversationSession,
    MessageAttachment,
    SessionStatus,
)
from app.services import whatsapp_dispatch
from app.services.whatsapp_outbound import (
    MAX_SEND_TRIES,
    RetrySend,
    media_url_allowed,
    send_image,
)
from app.workers.whatsapp import send_whatsapp_image
from tests.whatsapp.conftest import connect, make_tenant
from tests.whatsapp.test_outbound import _chat, _NoCloseSession

pytestmark = pytest.mark.asyncio

GOOD_URL = "https://res.cloudinary.com/demo/image/upload/luran/chat_images/a.jpg"


@pytest.fixture
def conn(db, fake_sidecar):
    return connect(db, fake_sidecar, make_tenant(db))


@pytest.fixture
def manager(monkeypatch):
    mgr = ConnectionManager()
    monkeypatch.setattr(mgr, "_publish_event", AsyncMock())
    return mgr


@pytest.fixture
def wa_session(db, conn, queue, monkeypatch):
    chat = _chat(db, conn, with_session=True)
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _NoCloseSession(db))
    return chat


def _att(db, *, tenant_id, session_id, url=GOOD_URL, **over) -> MessageAttachment:
    fields = dict(
        session_id=session_id,
        tenant_id=tenant_id,
        direction=AttachmentDirection.OUTBOUND,
        kind=AttachmentKind.IMAGE,
        status=AttachmentStatus.LINKED,
        moderation_status=AttachmentModeration.PASSED,
        cloudinary_public_id=f"pub_{uuid4().hex[:8]}",
        url=url,
        content_type="image/jpeg",
        bytes=1234,
    )
    fields.update(over)
    att = MessageAttachment(**fields)
    db.add(att)
    db.commit()
    return att


def _frame(content: str, *atts: MessageAttachment, mid: str = "m-img") -> dict:
    return {
        "type": "agent_message",
        "message_id": mid,
        "content": content,
        "agent_name": "Sam",
        "attachments": [{"id": str(a.id), "url": "https://evil.example/x.png"} for a in atts],
    }


# ------------------------------------------------------------------ forward plan
async def test_agent_image_with_text_goes_as_one_image_with_the_text_as_its_caption(
    db, conn, queue, manager, wa_session
):
    att = _att(db, tenant_id=conn.tenant_id, session_id=wa_session.session_id)
    await manager.fanout_event(
        wa_session.session_id, _frame("Here is your invoice", att), to_customer=True, to_agents=True
    )
    assert queue.sends == []  # the text rode along as the caption: no separate message
    assert queue.images == [
        {
            "connection_id": conn.id,
            "chat_id": wa_session.id,
            "attachment_id": att.id,
            "caption": "Here is your invoice",
            "key": f"agent_img:{att.id}",
        }
    ]


async def test_image_only_message_has_no_caption_and_no_text_notice(
    db, conn, queue, manager, wa_session
):
    att = _att(db, tenant_id=conn.tenant_id, session_id=wa_session.session_id)
    await manager.fanout_event(
        wa_session.session_id, _frame("[image]", att), to_customer=True, to_agents=True
    )
    assert queue.sends == [] and [i["caption"] for i in queue.images] == [None]
    assert not hasattr(whatsapp_dispatch, "IMAGE_NOTICE")


async def test_several_images_caption_only_the_first(db, conn, queue, manager, wa_session):
    atts = [_att(db, tenant_id=conn.tenant_id, session_id=wa_session.session_id) for _ in range(3)]
    await manager.fanout_event(
        wa_session.session_id, _frame("Three photos", *atts), to_customer=True, to_agents=True
    )
    assert [i["attachment_id"] for i in queue.images] == [a.id for a in atts]  # frame order
    assert [i["caption"] for i in queue.images] == ["Three photos", None, None]
    assert len({i["key"] for i in queue.images}) == 3


async def test_a_text_too_long_for_a_caption_is_sent_as_its_own_message(
    db, conn, queue, manager, wa_session
):
    att = _att(db, tenant_id=conn.tenant_id, session_id=wa_session.session_id)
    long_text = "x" * 1500
    await manager.fanout_event(
        wa_session.session_id, _frame(long_text, att), to_customer=True, to_agents=True
    )
    assert [i["caption"] for i in queue.images] == [None]
    assert [s["text"] for s in queue.sends] == [long_text]  # nothing the agent wrote is lost
    assert queue.sends[0]["key"] == "agent_message:m-img"


async def test_text_only_frames_are_unchanged(db, conn, queue, manager, wa_session):
    frame = {"type": "agent_message", "message_id": "m-2", "content": "Hi", "attachments": []}
    await manager.fanout_event(wa_session.session_id, frame, to_customer=True, to_agents=True)
    assert [s["text"] for s in queue.sends] == ["Hi"] and queue.images == []


async def test_a_frame_cannot_smuggle_a_url_or_a_foreign_attachment_into_the_forward(
    db, conn, fake_sidecar, queue, manager, wa_session
):
    other_tenant = connect(db, fake_sidecar, make_tenant(db), "2348000000031")
    foreign_session = ConversationSession(
        tenant_id=other_tenant.tenant_id, user_identifier="x", status=SessionStatus.ACTIVE
    )
    db.add(foreign_session)
    db.commit()
    sid, tid = wa_session.session_id, conn.tenant_id
    foreign = _att(db, tenant_id=other_tenant.tenant_id, session_id=foreign_session.id)
    bad = {
        "evil host": _att(db, tenant_id=tid, session_id=sid, url="https://evil.example/a.jpg"),
        "http": _att(db, tenant_id=tid, session_id=sid, url=GOOD_URL.replace("https", "http")),
        "userinfo": _att(
            db, tenant_id=tid, session_id=sid, url="https://res.cloudinary.com@evil.example/a.jpg"
        ),
        "lookalike": _att(
            db, tenant_id=tid, session_id=sid, url="https://res.cloudinary.com.evil.example/a.jpg"
        ),
        "customer's own image": _att(
            db, tenant_id=tid, session_id=sid, direction=AttachmentDirection.INBOUND
        ),
        "video": _att(db, tenant_id=tid, session_id=sid, kind=AttachmentKind.VIDEO),
        "unlinked": _att(db, tenant_id=tid, session_id=sid, status=AttachmentStatus.UPLOADED),
        "expired": _att(db, tenant_id=tid, session_id=sid, url=None),
    }
    frame = _frame("hello", foreign, *bad.values())
    frame["attachments"].append({"id": "not-a-uuid"})
    frame["attachments"].append({"id": str(uuid4())})  # unknown id
    await manager.fanout_event(sid, frame, to_customer=True, to_agents=True)
    assert queue.images == []
    # nothing could be sent as an image, so the agent's words still go out as text
    assert [s["text"] for s in queue.sends] == ["hello"]


async def test_owner_phone_and_agent_only_frames_never_send_images(
    db, conn, queue, manager, wa_session
):
    att = _att(db, tenant_id=conn.tenant_id, session_id=wa_session.session_id)
    await manager.fanout_event(wa_session.session_id, _frame("x", att), False, True)
    customer = {**_frame("x", att), "type": "customer_message"}
    await manager.fanout_event(wa_session.session_id, customer, True, True)
    assert queue.images == []


# ------------------------------------------------------------------ sender job
async def test_send_image_posts_the_stored_url_and_caption_to_the_sidecar(db, conn, fake_sidecar):
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    assert await send_image(db, conn.id, chat.id, att.id, "Your receipt", 1) == "sent"
    assert fake_sidecar.sent == [
        {
            "key": conn.session_key,
            "to_ref": "1234567890@lid",
            "image": {"url": GOOD_URL, "caption": "Your receipt"},
        }
    ]


async def test_send_image_without_caption_omits_the_field(db, conn, fake_sidecar):
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    await send_image(db, conn.id, chat.id, att.id, None, 1)
    assert fake_sidecar.sent[0]["image"] == {"url": GOOD_URL}


async def test_send_image_rereads_the_url_from_our_row_so_a_bad_row_is_never_sent(
    db, conn, fake_sidecar
):
    chat = _chat(db, conn, with_session=True)
    other_session = ConversationSession(
        tenant_id=conn.tenant_id, user_identifier="y", status=SessionStatus.ACTIVE
    )
    db.add(other_session)
    db.commit()
    cases = [
        _att(
            db, tenant_id=conn.tenant_id, session_id=chat.session_id, url="https://evil.example/a"
        ),
        _att(db, tenant_id=conn.tenant_id, session_id=other_session.id),  # another conversation
    ]
    for att in cases:
        assert (
            await send_image(db, conn.id, chat.id, att.id, None, 1) == "dropped_media_not_allowed"
        )
    assert await send_image(db, conn.id, chat.id, uuid4(), None, 1) == "dropped_media_not_allowed"
    assert fake_sidecar.sent == []


@pytest.mark.parametrize(
    "status,err",
    [(400, "invalid_image_url"), (413, "image_too_large"), (415, "unsupported_media_type")],
)
async def test_sidecar_media_refusals_are_permanent_never_retried(
    db, conn, fake_sidecar, status, err
):
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    fake_sidecar.image_send_error = (status, err)
    assert await send_image(db, conn.id, chat.id, att.id, None, 1) == f"dropped_{err}"
    assert await send_image(db, conn.id, chat.id, att.id, None, MAX_SEND_TRIES) == f"dropped_{err}"


async def test_rate_cap_and_outage_follow_the_text_retry_policy(db, conn, fake_sidecar):
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    fake_sidecar.image_send_error = (429, "rate_cap")
    with pytest.raises(RetrySend):
        await send_image(db, conn.id, chat.id, att.id, None, 1)
    fake_sidecar.image_send_error = (500, "boom")
    with pytest.raises(RetrySend):
        await send_image(db, conn.id, chat.id, att.id, None, 2)
    assert await send_image(db, conn.id, chat.id, att.id, None, MAX_SEND_TRIES) == "dropped_gave_up"


async def test_send_image_keeps_the_text_hard_stops(db, fake_sidecar):
    conn = connect(db, fake_sidecar, make_tenant(db), status="reconnecting")
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    assert await send_image(db, conn.id, chat.id, att.id, None, 1) == "dropped_not_connected"
    fake_sidecar.sessions[conn.session_key]["status"] = "connected"
    conn.status = "connected"
    chat.last_inbound_at = None
    db.commit()
    assert await send_image(db, conn.id, chat.id, att.id, None, 1) == "dropped_reply_only"
    chat.last_inbound_at = chat.created_at
    db.commit()
    fake_sidecar.image_send_error = None
    fake_sidecar.send_error = (409, "reply_only")
    assert await send_image(db, conn.id, chat.id, att.id, None, 1) == "dropped_reply_only"
    assert [c for c in fake_sidecar.calls if c[0] == "POST"].count(
        ("POST", "/sessions/{key}/send")
    ) == 1  # only the last one reached the sidecar


async def test_the_arq_job_unseals_the_caption(db, conn, fake_sidecar, monkeypatch):
    chat = _chat(db, conn, with_session=True)
    att = _att(db, tenant_id=conn.tenant_id, session_id=chat.session_id)
    monkeypatch.setattr("app.workers.whatsapp.SessionLocal", lambda: _NoCloseSession(db))
    out = await send_whatsapp_image(
        {"job_try": 1}, str(conn.id), str(chat.id), str(att.id), encrypt_field("Sealed caption")
    )
    assert out == "sent" and fake_sidecar.sent[0]["image"]["caption"] == "Sealed caption"


# ------------------------------------------------------------------ adapter + url rules
async def test_the_real_sidecar_allowlist_would_also_have_refused_a_bad_host(fake_sidecar):
    """The fake mirrors the sidecar's MEDIA_URL_ALLOWLIST: a 400 comes back as MediaRejected."""
    from app.channels.base import MediaRejected, OutboundMessage

    fake_sidecar.sessions["k"] = {"status": "connected", "replied_to": set()}
    adapter = LinkedDeviceAdapter(transport=__import__("httpx").ASGITransport(app=fake_sidecar.app))
    with pytest.raises(MediaRejected) as err:
        await adapter.send("k", OutboundMessage(chat_ref="r", image_url="https://evil.example/a"))
    assert err.value.code == "invalid_image_url"


@pytest.mark.parametrize(
    "url,ok",
    [
        (GOOD_URL, True),
        ("https://RES.CLOUDINARY.COM/x.jpg", True),
        ("https://res.cloudinary.com:443/x.jpg", True),
        ("https://res.cloudinary.com:8443/x.jpg", False),
        ("http://res.cloudinary.com/x.jpg", False),
        ("https://user:pw@res.cloudinary.com/x.jpg", False),
        ("https://res.cloudinary.com.evil.example/x.jpg", False),
        ("https://169.254.169.254/latest", False),
        ("https://localhost/x", False),
        ("", False),
        (None, False),
        ("not a url", False),
    ],
)
async def test_media_url_allowlist(wa_settings, url, ok):
    assert media_url_allowed(url) is ok


# ------------------------------------------------------------------ media fetch (adapter)
async def test_fetch_media_returns_bytes_and_a_normalised_content_type(conn, fake_sidecar):
    import httpx

    from app.channels.base import MediaNotFound, MediaTooLarge, SidecarUnavailable

    adapter = LinkedDeviceAdapter(transport=httpx.ASGITransport(app=fake_sidecar.app))
    media = fake_sidecar.add_media(conn.session_key, b"\x89PNG-bytes", "image/png")
    blob = await adapter.fetch_media(conn.session_key, media["id"], 1024)
    assert blob.content == b"\x89PNG-bytes" and blob.content_type == "image/png"
    assert "PNG-bytes" not in repr(blob)  # bytes never reach a log via repr
    with pytest.raises(MediaTooLarge):
        await adapter.fetch_media(conn.session_key, media["id"], 4)
    with pytest.raises(MediaNotFound):
        await adapter.fetch_media(conn.session_key, "m_unknown", 1024)
    with pytest.raises(MediaNotFound):  # a hostile id is refused before any request
        await adapter.fetch_media(conn.session_key, "../health", 1024)
    fake_sidecar.media_status = 503
    with pytest.raises(SidecarUnavailable):
        await adapter.fetch_media(conn.session_key, media["id"], 1024)
    fake_sidecar.media_status = None
    # a wrong key finds nothing: media is per session
    with pytest.raises(MediaNotFound):
        await adapter.fetch_media("some-other-session", media["id"], 1024)
