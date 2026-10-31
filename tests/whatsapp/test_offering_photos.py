"""Offerings on WhatsApp: catalog photos (selection, caps, send-time re-check) and the quote text.

Spec 11/12 of the Offerings catalog: after the reply text is enqueued, the cover photo of each of
1-3 shown items follows as its own job; the caption is built at send time from the live row; a
photo can never fail or delay the text; the quote goes as a second plain-text message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models.conversation import MessageAttachment, MessageRole
from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingStatus,
)
from app.db.models.whatsapp import WhatsAppChat
from app.offerings.facts import facts_from_row
from app.offerings.quote_text import render_quote_text
from app.services import whatsapp_inbound, whatsapp_offering_photos as photos
from app.services.whatsapp_inbound import handle_event
from app.services.whatsapp_outbound import (
    MAX_SEND_TRIES,
    RetrySend,
    send_offering_image,
)
from app.workers.whatsapp import send_whatsapp_offering_image
from tests.offerings.helpers import add_items, add_photos
from tests.whatsapp.conftest import connect, inbound, inbound_image, make_tenant, png_bytes
from tests.whatsapp.test_outbound import _chat, _NoCloseSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
def conn(db, fake_sidecar, queue):
    return connect(db, fake_sidecar, make_tenant(db))


def _item(db, tenant_id, seq, *, photos_n=1, **over) -> Offering:
    tenant = SimpleNamespace(id=tenant_id)
    add_items(db, tenant, 1, start=seq, name=f"Item {seq}", price=12000, **over)
    row = db.execute(
        select(Offering).where(Offering.tenant_id == tenant_id, Offering.seq == seq)
    ).scalar_one()
    if photos_n:
        add_photos(db, tenant, row, photos_n)
    return row


def _cover(db, offering: Offering) -> OfferingImage:
    return (
        db.execute(
            select(OfferingImage)
            .where(OfferingImage.offering_id == offering.id)
            .order_by(OfferingImage.position)
        )
        .scalars()
        .first()
    )


def _meta(*offerings: Offering, quote: dict | None = None) -> dict:
    meta = {
        "offerings": {
            "mode": "browse" if len(offerings) > 1 else "detail",
            "items": [
                {
                    "ref": f"#{o.seq}",
                    "offering_id": str(o.id),
                    "name": o.name,
                    "price_text": "x",
                    "availability": "in_stock",
                    "cover_image_id": None,
                    "cover_url": None,
                    "photo_count": 1,
                }
                for o in offerings
            ],
        }
    }
    if quote is not None:
        meta["quote"] = quote
    return meta


def _msg(meta: dict | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), meta=meta)


def _wa_chat(db, conn) -> WhatsAppChat:
    return _chat(db, conn, with_session=True)


# ------------------------------------------------------------------ caption (send time)
async def test_caption_is_ref_name_price_availability_from_the_row(db, conn):
    row = _item(db, conn.tenant_id, 12, photos_n=0)
    row.name = "Canvas sneaker"
    db.commit()
    assert (
        photos.build_caption(facts_from_row(row), "NGN")
        == "#12 Canvas sneaker · ₦12,000 · In stock"
    )


async def test_caption_never_exceeds_the_whatsapp_limit(db, conn):
    row = _item(db, conn.tenant_id, 1, photos_n=0, price_note="x" * 60)
    facts = facts_from_row(row)
    assert len(photos.build_caption(facts, "NGN")) <= 1024
    long = SimpleNamespace(
        ref="#1",
        name="n" * 5000,
        price_text=lambda c: "₦1",
        availability_text=lambda **k: "in stock",
    )
    assert len(photos.build_caption(long, "NGN")) == 1024


# ------------------------------------------------------------------ sender job
async def _send(db, conn, chat, image, tries=1):
    return await send_offering_image(db, conn.id, chat.id, image.id, tries)


async def test_sender_sends_the_whatsapp_rendition_with_a_live_caption(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 12)
    image = _cover(db, row)
    row.price = 15000  # the owner changed the price after the reply was queued
    db.commit()
    assert await _send(db, conn, chat, image) == "sent"
    [sent] = fake_sidecar.sent
    assert (
        sent["image"]["url"]
        .replace("/upload/", "/upload/#")
        .split("#")[1]
        .startswith("w_1280,q_auto:good,f_jpg/")
    )
    assert sent["image"]["caption"] == "#12 Item 12 · ₦15,000 · In stock"  # NOT the old price
    assert sent["to_ref"] == chat.contact_ref


@pytest.mark.parametrize(
    "url",
    [
        "http://res.cloudinary.com/demo/image/upload/v1/a.jpg",  # not https
        "https://evil.example/a.jpg",  # host off the allowlist
        "https://res.cloudinary.com:8443/demo/image/upload/v1/a.jpg",  # odd port
        "https://user:pw@res.cloudinary.com/demo/image/upload/v1/a.jpg",  # credentials
    ],
)
async def test_sender_refuses_a_stored_url_off_the_allowlist(db, conn, fake_sidecar, url):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    image.url = url
    db.commit()
    assert await _send(db, conn, chat, image) == "dropped_media_not_allowed"
    assert fake_sidecar.sent == []


async def test_a_cross_tenant_image_id_is_refused(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    other = make_tenant(db)
    foreign = _cover(db, _item(db, other.id, 1))
    assert await _send(db, conn, chat, foreign) == "dropped_offering_unavailable"
    assert fake_sidecar.sent == []


async def test_an_image_row_claiming_this_tenant_on_another_tenants_item_is_refused(
    db, conn, fake_sidecar
):
    chat = _wa_chat(db, conn)
    other = make_tenant(db)
    foreign_item = _item(db, other.id, 1, photos_n=0)
    forged = OfferingImage(
        offering_id=foreign_item.id,
        tenant_id=conn.tenant_id,  # row says "ours", the item it hangs on is not
        url="https://res.cloudinary.com/demo/image/upload/v1/forged.jpg",
        cloudinary_public_id="p",
        position=0,
    )
    db.add(forged)
    db.commit()
    assert await _send(db, conn, chat, forged) == "dropped_offering_unavailable"
    assert fake_sidecar.sent == []


@pytest.mark.parametrize(
    "change",
    [
        {"status": OfferingStatus.ARCHIVED},
        {"availability": OfferingAvailability.OUT_OF_STOCK},
    ],
)
async def test_an_item_archived_or_sold_out_after_queueing_is_skipped(
    db, conn, fake_sidecar, change
):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    image = _cover(db, row)
    for k, v in change.items():
        setattr(row, k, v)
    db.commit()
    assert await _send(db, conn, chat, image) == "dropped_offering_unavailable"
    assert fake_sidecar.sent == []


async def test_a_deleted_photo_or_unknown_id_is_dropped(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    image_id = image.id
    db.delete(image)
    db.commit()
    assert (
        await send_offering_image(db, conn.id, chat.id, image_id, 1)
        == "dropped_offering_unavailable"
    )
    assert fake_sidecar.sent == []


async def test_sender_keeps_the_hard_stops_of_every_send(db, fake_sidecar):
    off = connect(db, fake_sidecar, make_tenant(db), status="logged_out")
    chat = _wa_chat(db, off)
    image = _cover(db, _item(db, off.tenant_id, 1))
    assert await _send(db, off, chat, image) == "dropped_not_connected"
    assert fake_sidecar.sent == []


async def test_sender_honours_a_pause_that_started_after_queueing(db, conn, fake_sidecar):
    from datetime import timedelta

    from app.utils.datetime import utcnow_aware

    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    chat.human_until = utcnow_aware() + timedelta(minutes=30)
    db.commit()
    assert await _send(db, conn, chat, image) == "dropped_paused"
    assert fake_sidecar.sent == []


async def test_sidecar_refusal_is_permanent_and_a_5xx_retries(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    fake_sidecar.image_send_error = (413, "media_too_large")
    assert (
        await _send(db, conn, chat, image) == "dropped_media_too_large"
    )  # the sidecar's own code, as send_image
    fake_sidecar.image_send_error = (500, "boom")
    with pytest.raises(RetrySend):
        await _send(db, conn, chat, image, tries=1)
    assert await _send(db, conn, chat, image, tries=MAX_SEND_TRIES) == "dropped_gave_up"


async def test_the_arq_job_maps_a_retry(db, conn, fake_sidecar, monkeypatch):
    from arq import Retry

    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    monkeypatch.setattr("app.workers.whatsapp.SessionLocal", lambda: _NoCloseSession(db))
    ok = await send_whatsapp_offering_image({}, str(conn.id), str(chat.id), str(image.id))
    assert ok == "sent"
    fake_sidecar.image_send_error = (500, "boom")
    with pytest.raises(Retry):
        await send_whatsapp_offering_image(
            {"job_try": 1}, str(conn.id), str(chat.id), str(image.id)
        )


async def test_the_job_is_registered_with_the_same_try_budget_as_image_sends():
    from app.workers.document_processor import WorkerSettings

    names = {getattr(f, "name", getattr(f, "__name__", None)): f for f in WorkerSettings.functions}
    assert "send_whatsapp_offering_image" in names
    assert names["send_whatsapp_offering_image"].max_tries == MAX_SEND_TRIES


# ------------------------------------------------------------------ selection
async def test_one_to_three_items_get_their_cover_photo_deferred_after_the_text(db, conn, queue):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n, photos_n=3) for n in (1, 2, 3)]
    msg = _msg(_meta(*rows))
    await photos.enqueue_catalog_followups(db, conn, chat, msg)
    assert [q["offering_image_id"] for q in queue.offering_images] == [
        _cover(db, r).id for r in rows
    ]
    assert all(
        q["connection_id"] == conn.id and q["chat_id"] == chat.id for q in queue.offering_images
    )
    assert all(q["defer_s"] > 0 for q in queue.offering_images)  # the text goes first
    assert [q["key"] for q in queue.offering_images] == [
        f"offimg:{msg.id}:{_cover(db, r).id}" for r in rows
    ]
    assert queue.sends == []  # photos are not texts


async def test_more_than_three_items_is_a_menu_and_gets_no_photos(db, conn, queue):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n) for n in range(1, 5)]
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(*rows)))
    assert queue.offering_images == []


async def test_an_item_without_photos_is_skipped_and_the_rest_still_go(db, conn, queue):
    chat = _wa_chat(db, conn)
    bare, shot = _item(db, conn.tenant_id, 1, photos_n=0), _item(db, conn.tenant_id, 2)
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(bare, shot)))
    assert [q["offering_image_id"] for q in queue.offering_images] == [_cover(db, shot).id]


async def test_archived_and_sold_out_items_are_skipped_at_selection(db, conn, queue):
    chat = _wa_chat(db, conn)
    gone = _item(db, conn.tenant_id, 1, status=OfferingStatus.ARCHIVED)
    out = _item(db, conn.tenant_id, 2, availability=OfferingAvailability.OUT_OF_STOCK)
    ok = _item(db, conn.tenant_id, 3)
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(gone, out, ok)))
    assert [q["offering_image_id"] for q in queue.offering_images] == [_cover(db, ok).id]


async def test_an_item_id_of_another_tenant_is_never_queued(db, conn, queue):
    chat = _wa_chat(db, conn)
    other = make_tenant(db)
    foreign = _item(db, other.id, 1)
    mine = _item(db, conn.tenant_id, 2)
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(foreign, mine)))
    assert [q["offering_image_id"] for q in queue.offering_images] == [_cover(db, mine).id]


async def test_the_photo_comes_from_our_rows_not_from_the_cover_id_in_the_meta(db, conn, queue):
    chat = _wa_chat(db, conn)
    other = make_tenant(db)
    foreign_image = _cover(db, _item(db, other.id, 1))
    mine = _item(db, conn.tenant_id, 2)
    meta = _meta(mine)
    meta["offerings"]["items"][0]["cover_image_id"] = str(foreign_image.id)
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(meta))
    assert [q["offering_image_id"] for q in queue.offering_images] == [_cover(db, mine).id]


async def test_a_reply_without_offerings_meta_enqueues_nothing(db, conn, queue):
    chat = _wa_chat(db, conn)
    for meta in (None, {}, {"offerings": None}, {"offerings": {"mode": "none", "items": []}}):
        await photos.enqueue_catalog_followups(db, conn, chat, _msg(meta))
    assert queue.offering_images == [] and queue.sends == []


# ------------------------------------------------------------------ per-chat hourly cap
async def test_six_catalog_photos_per_chat_per_hour_then_text_only(db, conn, queue):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n) for n in (1, 2, 3)]
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(*rows)))
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(*rows)))
    assert len(queue.offering_images) == 6
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(*rows)))
    assert len(queue.offering_images) == 6  # the third reply keeps its text, loses its photos


async def test_a_partly_used_budget_grants_only_what_is_left(db, conn, queue):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n) for n in (1, 2, 3)]
    assert await photos.take_photo_budget(chat.id, 5) == 5
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(*rows)))
    assert [q["offering_image_id"] for q in queue.offering_images] == [_cover(db, rows[0]).id]


async def test_the_cap_is_per_chat(db, conn, queue):
    a, b = _wa_chat(db, conn), _wa_chat(db, conn)
    assert await photos.take_photo_budget(a.id, 6) == 6
    assert await photos.take_photo_budget(a.id, 1) == 0
    assert await photos.take_photo_budget(b.id, 3) == 3


async def test_the_limiter_failing_closed_skips_photos_but_not_the_text(
    db, conn, queue, llm, monkeypatch
):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens here
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    assert await photos.take_photo_budget(chat.id, 3) == 0
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(row)))
    assert queue.offering_images == []


# ------------------------------------------------------------------ the inbound pipeline
def _reply(meta: dict | None, content: str = "Here is the Canvas sneaker (#1)."):
    message = SimpleNamespace(id=uuid4(), role=MessageRole.ASSISTANT, content=content, meta=meta)
    return SimpleNamespace(session_id=uuid4(), message=message)


def _patched_send_message(response):
    return patch("app.api.v1.endpoints.chat.send_message", new=AsyncMock(return_value=response))


async def _drive(db, conn, response):
    with (
        _patched_send_message(response),
        patch.object(whatsapp_inbound, "_usable_session", return_value=None),
        patch.object(whatsapp_inbound, "_plan_blocks_conversation", return_value=False),
    ):
        return await handle_event(db, inbound(conn, "show me sneakers"))


async def test_inbound_reply_text_is_enqueued_before_the_photos(db, conn, queue):
    row = _item(db, conn.tenant_id, 1)
    response = _reply(_meta(row))
    assert await _drive(db, conn, response) == "ai_reply"
    assert [s["key"] for s in queue.sends] == [f"ai:{response.message.id}"]
    assert len(queue.offering_images) == 1


async def test_a_photo_that_cannot_be_queued_never_fails_or_repeats_the_text(db, conn, queue):
    row = _item(db, conn.tenant_id, 1)
    queue.fail_offering_images = True
    response = _reply(_meta(row))
    assert await _drive(db, conn, response) == "ai_reply"  # no exception: arq must not retry
    assert [s["key"] for s in queue.sends] == [f"ai:{response.message.id}"]
    assert queue.offering_images == []


# ------------------------------------------------------------------ quote text
QUOTE = {
    "currency": "NGN",
    "line_items": [{"label": "Canvas sneaker", "detail": "2 × ₦12,000", "amount": 24000}],
    "total": 24000,
}


async def test_the_quote_goes_as_a_second_plain_text_message_after_the_reply(db, conn, queue):
    row = _item(db, conn.tenant_id, 1)
    response = _reply(_meta(row, quote=QUOTE))
    assert await _drive(db, conn, response) == "ai_reply"
    reply, quote = queue.sends
    assert reply["key"] == f"ai:{response.message.id}" and reply["text"] == response.message.content
    assert quote["key"] == f"quote:{response.message.id}"
    assert quote["text"] == render_quote_text(QUOTE)
    assert "Total: ₦24,000" in quote["text"] and quote["guard"] is True
    assert quote["defer_s"] > 0 and "defer_s" not in reply  # it cannot overtake the reply


async def test_no_quote_no_second_message(db, conn, queue):
    row = _item(db, conn.tenant_id, 1)
    assert await _drive(db, conn, _reply(_meta(row))) == "ai_reply"
    assert len(queue.sends) == 1


async def test_a_failing_quote_enqueue_does_not_break_the_reply(db, conn, queue, monkeypatch):
    from app.services import whatsapp_enqueue

    original = whatsapp_enqueue.enqueue_send

    async def boom(
        connection_id, chat_id, text, key, guard=False, rating_request_id=None, defer_s=0
    ):
        if key.startswith("quote:"):
            raise RuntimeError("redis down")
        await original(connection_id, chat_id, text, key, guard, rating_request_id, defer_s)

    monkeypatch.setattr(whatsapp_enqueue, "enqueue_send", boom)
    row = _item(db, conn.tenant_id, 1)
    assert await _drive(db, conn, _reply(_meta(row, quote=QUOTE))) == "ai_reply"
    assert [s["key"].split(":")[0] for s in queue.sends] == ["ai"]


async def test_photos_and_quote_text_follow_an_image_message_reply_too(
    db, conn, queue, fake_sidecar, media_stubs
):
    row = _item(db, conn.tenant_id, 1)
    response = _reply(_meta(row, quote=QUOTE))
    with _patched_send_message(response):
        out = await handle_event(
            db, inbound_image(conn, fake_sidecar, png_bytes(), caption="this?")
        )
    assert out == "ai_reply"
    assert [s["key"].split(":")[0] for s in queue.sends] == ["ai", "quote"]
    assert len(queue.offering_images) == 1


# ------------------------------------------------------------------ retention
async def test_a_catalog_photo_is_not_a_chat_attachment_and_retention_cannot_touch_it(
    db, conn, queue, fake_sidecar, monkeypatch
):
    from app.workers import attachment_retention as retention

    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    image = _cover(db, row)
    await photos.enqueue_catalog_followups(db, conn, chat, _msg(_meta(row)))
    assert await _send(db, conn, chat, image) == "sent"
    assert db.execute(select(MessageAttachment)).scalars().all() == []  # no attachment row ever

    destroyed: list[str] = []
    monkeypatch.setattr(
        retention.cloudinary_manager,
        "destroy",
        lambda pid, **k: destroyed.append(pid) or (True, None),
    )
    monkeypatch.setattr(retention.settings, "ATTACHMENT_RETENTION_ENABLED", True)
    monkeypatch.setattr(retention, "SessionLocal", lambda: _NoCloseSession(db))
    await retention.expire_attachments({})
    assert destroyed == []
    assert db.get(OfferingImage, image.id) is not None
