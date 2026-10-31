"""Offerings on WhatsApp: catalog photos (chained after the reply, budgets, send-time re-check) and
the quote text message.

Spec 11/12 of the Offerings catalog. The reply text is queued first and carries the follow-ups
(quote text, then photos) with it: they are queued only after the reply was actually sent, so they
can never overtake it. Photos have their own per-chat and per-connection budgets (enforced at send
time, fail-closed), are dropped and never retried, and the caption is built from the live row.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from arq import Retry
from sqlalchemy import select, update

from app.channels.linked_device import LinkedDeviceAdapter
from app.core.config import settings
from app.core.encryption import encrypt_field
from app.db.models.conversation import (
    ConversationSession,
    Message,
    MessageAttachment,
    MessageRole,
    SessionStatus,
)
from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.whatsapp import WhatsAppChat
from app.offerings.facts import facts_from_row
from app.offerings.quote_text import render_quote_text
from app.services import whatsapp_inbound, whatsapp_offering_photos as photos
from app.services.whatsapp_inbound import handle_event
from app.services.whatsapp_outbound import MAX_SEND_TRIES, send_offering_image
from app.utils.datetime import utcnow_aware
from app.workers.whatsapp import send_whatsapp_message, send_whatsapp_offering_image
from tests.offerings.helpers import add_items, add_photos
from tests.whatsapp.conftest import connect, inbound, inbound_image, make_tenant
from tests.whatsapp.test_outbound import _chat, _NoCloseSession

pytestmark = pytest.mark.asyncio

RUN = uuid4().hex  # claims persist in Redis for a day: make them unique per test run
DEAD_REDIS = "redis://127.0.0.1:1/0"  # nothing listens here: every Redis call fails fast


@pytest.fixture
def conn(db, fake_sidecar, queue):
    return connect(db, fake_sidecar, make_tenant(db))


@pytest.fixture
def roomy_budget(monkeypatch):
    """Connection budgets out of the way, to test the per-chat cap on its own."""
    monkeypatch.setattr(settings, "WHATSAPP_CATALOG_PHOTOS_PER_MIN", 1000)
    monkeypatch.setattr(settings, "WHATSAPP_CATALOG_PHOTO_DAILY_PCT", 100)
    monkeypatch.setattr(settings, "WHATSAPP_DAILY_SEND_CAP", 100000)


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


QUOTE_LABEL = "Item 1"
CATALOG_QUOTE = {
    "currency": "NGN",
    "line_items": [{"label": QUOTE_LABEL, "detail": "2 × ₦12,000", "amount": 24000}],
    "total": 24000,
}
# The plan-tier quote of a software tenant (chat.py `compute_quote`): never a catalog quote.
TIER_QUOTE = {
    "currency": "NGN",
    "line_items": [
        {"label": "Team plan", "detail": "5 seats × ₦10,000", "amount": 50000},
        {"label": "Annual discount", "detail": "-10%", "amount": -5000},
    ],
    "total": 45000,
}


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


def _photo_step(msg_id, *image_ids) -> dict:
    return {"kind": "photos", "message_id": str(msg_id), "image_ids": [str(i) for i in image_ids]}


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
    assert len(photos.build_caption(facts_from_row(row), "NGN")) <= 1024
    long = SimpleNamespace(
        ref="#1",
        name="n" * 5000,
        price_text=lambda c: "₦1",
        availability_text=lambda **k: "in stock",
    )
    assert len(photos.build_caption(long, "NGN")) == 1024


# ------------------------------------------------------------------ quote text rendering
async def test_quote_message_matches_the_shared_renderer_for_a_catalog_quote():
    assert photos.quote_message_text(CATALOG_QUOTE) == render_quote_text(CATALOG_QUOTE)


async def test_a_negative_line_puts_the_sign_before_the_currency_symbol():
    text = photos.quote_message_text(TIER_QUOTE)
    assert "Annual discount — -₦5,000" in text
    assert "₦-" not in text and "Total: ₦45,000" in text


# ------------------------------------------------------------------ the rendition
async def test_the_whatsapp_rendition_is_capped_in_both_dimensions_never_upscaled():
    url = "https://res.cloudinary.com/demo/image/upload/v1700000000/luran/offerings/t1/abc.jpg"
    out = photos.whatsapp_rendition(url)
    assert "/upload/c_limit,w_1280,h_1280,q_auto:good,f_jpg/v1700000000/" in out
    assert out.startswith("https://res.cloudinary.com/")
    assert photos.whatsapp_rendition("https://example.com/a.jpg") == "https://example.com/a.jpg"


# ------------------------------------------------------------------ the sender
async def _send(db, conn, chat, image, claim=None):
    # every call is its own job unless a test says otherwise (claims live in Redis for a day)
    key = f"{RUN}:{claim}" if claim else uuid4().hex
    return await send_offering_image(db, conn.id, chat.id, image.id, key)


async def test_sender_sends_the_rendition_with_a_live_caption(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 12)
    image = _cover(db, row)
    row.price = 15000  # the owner changed the price after the reply was queued
    db.commit()
    assert await _send(db, conn, chat, image) == "sent"
    [sent] = fake_sidecar.sent
    assert "/upload/c_limit,w_1280,h_1280,q_auto:good,f_jpg/" in sent["image"]["url"]
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
    foreign = _cover(db, _item(db, make_tenant(db).id, 1))
    assert await _send(db, conn, chat, foreign) == "dropped_offering_unavailable"
    assert fake_sidecar.sent == []


async def test_an_image_row_claiming_this_tenant_on_another_tenants_item_is_refused(
    db, conn, fake_sidecar
):
    chat = _wa_chat(db, conn)
    foreign_item = _item(db, make_tenant(db).id, 1, photos_n=0)
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
    [{"status": OfferingStatus.ARCHIVED}, {"availability": OfferingAvailability.OUT_OF_STOCK}],
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


async def test_a_deleted_photo_is_dropped(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    image_id = image.id
    db.delete(image)
    db.commit()
    assert (
        await send_offering_image(db, conn.id, chat.id, image_id, uuid4().hex)
        == "dropped_offering_unavailable"
    )
    assert fake_sidecar.sent == []


async def test_the_owner_switching_the_catalog_off_after_queueing_stops_the_photo(
    db, conn, fake_sidecar
):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    db.execute(
        update(TenantOfferingsSettings)
        .where(TenantOfferingsSettings.tenant_id == conn.tenant_id)
        .values(enabled=False)
    )
    db.commit()
    assert await _send(db, conn, chat, image) == "dropped_offerings_off"
    assert fake_sidecar.sent == []


async def test_a_plan_that_no_longer_includes_the_catalog_stops_the_photo(
    db, conn, fake_sidecar, monkeypatch
):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    monkeypatch.setattr(photos, "tenant_has", lambda db_, tenant_id, key: False)
    assert await _send(db, conn, chat, image) == "dropped_offerings_off"
    assert fake_sidecar.sent == []


async def test_sender_keeps_the_hard_stops_of_every_send(db, fake_sidecar):
    off = connect(db, fake_sidecar, make_tenant(db), status="logged_out")
    chat = _wa_chat(db, off)
    image = _cover(db, _item(db, off.tenant_id, 1))
    assert await _send(db, off, chat, image) == "dropped_not_connected"
    assert fake_sidecar.sent == []


async def test_sender_honours_a_pause_that_started_after_queueing(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    chat.human_until = utcnow_aware() + timedelta(minutes=30)
    db.commit()
    assert await _send(db, conn, chat, image) == "dropped_paused"
    assert fake_sidecar.sent == []


# --- never retried: a lost photo is cheap, a duplicate is a ban risk
async def test_a_rate_capped_photo_is_dropped_not_retried(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    fake_sidecar.image_send_error = (429, "rate_cap")
    assert await _send(db, conn, chat, image) == "dropped_rate_capped"  # no RetrySend raised


async def test_sidecar_refusal_is_permanent(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    fake_sidecar.image_send_error = (413, "media_too_large")
    assert await _send(db, conn, chat, image) == "dropped_media_too_large"


async def test_a_transport_timeout_or_5xx_drops_the_photo_and_a_redelivery_cannot_resend_it(
    db, conn, fake_sidecar
):
    chat = _wa_chat(db, conn)
    claim_a = uuid4().hex
    image = _cover(db, _item(db, conn.tenant_id, 1))
    fake_sidecar.image_send_error = (500, "boom")
    assert await _send(db, conn, chat, image, claim=claim_a) == "dropped_sidecar_unavailable"
    fake_sidecar.image_send_error = None  # the sidecar is fine now
    sent_before = len(fake_sidecar.sent)
    # the same job delivered again (arq redelivery after a worker crash or a lost ack)
    assert await _send(db, conn, chat, image, claim=claim_a) == "dropped_duplicate"
    assert len(fake_sidecar.sent) == sent_before == 0
    # a different job (a later reply about the same item) is a new send
    assert await _send(db, conn, chat, image, claim=uuid4().hex) == "sent"


async def test_an_image_send_gets_the_longer_timeout_and_text_keeps_the_short_one():
    adapter = LinkedDeviceAdapter()
    with (
        patch.object(settings, "WHATSAPP_SIDECAR_URL", "http://x"),
        patch.object(settings, "WHATSAPP_SIDECAR_TOKEN", "t"),
    ):
        assert adapter._client(image=False).timeout.read == settings.WHATSAPP_SIDECAR_TIMEOUT_S
        assert adapter._client(image=True).timeout.read == settings.WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S
    assert settings.WHATSAPP_SIDECAR_IMAGE_TIMEOUT_S >= 60


# --- budgets, enforced when the photo is actually sent
async def test_two_photos_per_minute_per_connection_then_dropped(db, conn, fake_sidecar):
    chat = _wa_chat(db, conn)
    images = [_cover(db, _item(db, conn.tenant_id, n)) for n in (1, 2, 3)]
    got = [await _send(db, conn, chat, im, claim=f"j{n}") for n, im in enumerate(images)]
    assert got == ["sent", "sent", "dropped_photo_budget"]
    assert len(fake_sidecar.sent) == 2


async def test_two_chats_in_one_minute_the_second_customers_text_is_never_starved(
    db, conn, fake_sidecar
):
    """One customer's catalog photos use up the connection's photo budget; another customer's
    text reply is unaffected (photos are what is dropped) and its own photo is dropped, not queued
    behind and not retried."""
    from app.services.whatsapp_outbound import send_text

    a, b = _wa_chat(db, conn), _wa_chat(db, conn)
    shots = [_cover(db, _item(db, conn.tenant_id, n)) for n in (1, 2, 3)]
    assert await _send(db, conn, a, shots[0], claim="a1") == "sent"
    assert await _send(db, conn, a, shots[1], claim="a2") == "sent"
    assert await send_text(db, conn.id, b.id, "Hello, yes we have that.", 1, True) == "sent"
    assert await _send(db, conn, b, shots[2], claim="b1") == "dropped_photo_budget"
    texts = [m for m in fake_sidecar.sent if "text" in m]
    assert [m["text"] for m in texts] == ["Hello, yes we have that."]
    assert len(fake_sidecar.sent) == 3  # 2 photos + 1 text, nothing else


async def test_the_connection_daily_photo_budget_is_a_share_of_the_send_cap(
    db, conn, fake_sidecar, monkeypatch
):
    monkeypatch.setattr(settings, "WHATSAPP_CATALOG_PHOTOS_PER_MIN", 1000)
    monkeypatch.setattr(settings, "WHATSAPP_DAILY_SEND_CAP", 20)
    monkeypatch.setattr(settings, "WHATSAPP_CATALOG_PHOTO_DAILY_PCT", 15)  # 15% of 20 = 3 a day
    assert photos.daily_photo_budget() == 3
    got = []
    for n in range(1, 6):  # five different chats: the per-chat cap is not what stops it
        chat = _wa_chat(db, conn)
        image = _cover(db, _item(db, conn.tenant_id, n))
        got.append(await _send(db, conn, chat, image, claim=f"d{n}"))
    assert got == ["sent", "sent", "sent", "dropped_photo_budget", "dropped_photo_budget"]


async def test_six_catalog_photos_per_chat_per_hour(db, conn, fake_sidecar, roomy_budget):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    got = [await _send(db, conn, chat, image, claim=f"c{n}") for n in range(8)]
    assert got == ["sent"] * 6 + ["dropped_photo_budget"] * 2


async def test_the_cap_is_per_chat_and_a_dropped_photo_costs_nothing(
    db, conn, fake_sidecar, roomy_budget
):
    a, b = _wa_chat(db, conn), _wa_chat(db, conn)
    assert await photos.take_photo_budget(a.id, conn.id, 6) == 6
    assert await photos.take_photo_budget(a.id, conn.id, 1) == 0
    assert await photos.take_photo_budget(b.id, conn.id, 3) == 3
    # a photo dropped for another reason never reached the limiter: budgets are spent on sends
    chat = _wa_chat(db, conn)
    gone = _item(db, conn.tenant_id, 9, status=OfferingStatus.ARCHIVED)
    for n in range(10):
        await _send(db, conn, chat, _cover(db, gone), claim=f"g{n}")
    assert await photos.photo_headroom(chat.id, conn.id, 6) == 6


async def test_the_limiter_down_means_no_photo_but_the_text_still_goes(
    db, conn, fake_sidecar, monkeypatch
):
    from app.services.whatsapp_outbound import send_text

    monkeypatch.setattr(settings, "REDIS_URL", DEAD_REDIS)
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    assert await _send(db, conn, chat, image) in {
        "dropped_limiter_unavailable",
        "dropped_photo_budget",
    }
    assert await send_text(db, conn.id, chat.id, "still here", 1, True) == "sent"
    assert [m["text"] for m in fake_sidecar.sent if "text" in m] == ["still here"]


async def test_the_arq_job_runs_the_sender_with_its_job_id_as_the_claim(
    db, conn, fake_sidecar, monkeypatch
):
    chat = _wa_chat(db, conn)
    image = _cover(db, _item(db, conn.tenant_id, 1))
    monkeypatch.setattr("app.workers.whatsapp.SessionLocal", lambda: _NoCloseSession(db))
    ctx = {"job_id": f"wa_send:offimg:{uuid4().hex}"}
    args = (str(conn.id), str(chat.id), str(image.id))
    assert await send_whatsapp_offering_image(ctx, *args) == "sent"
    assert await send_whatsapp_offering_image(ctx, *args) == "dropped_duplicate"  # redelivery


async def test_the_job_is_registered():
    from app.workers.document_processor import WorkerSettings

    names = {getattr(f, "name", getattr(f, "__name__", None)): f for f in WorkerSettings.functions}
    assert "send_whatsapp_offering_image" in names
    assert names["send_whatsapp_offering_image"].max_tries == MAX_SEND_TRIES


# ------------------------------------------------------------------ planning the follow-ups
async def test_one_to_three_items_plan_one_cover_photo_each(db, conn):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n, photos_n=3) for n in (1, 2, 3)]
    msg = _msg(_meta(*rows))
    steps = await photos.plan_followups(db, conn, chat, msg)
    assert steps == [_photo_step(msg.id, *[_cover(db, r).id for r in rows])]


async def test_more_than_three_items_is_a_menu_and_plans_no_photos(db, conn):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n) for n in range(1, 5)]
    assert await photos.plan_followups(db, conn, chat, _msg(_meta(*rows))) == []


async def test_photoless_archived_sold_out_and_foreign_items_are_not_planned(db, conn):
    chat = _wa_chat(db, conn)
    bare = _item(db, conn.tenant_id, 1, photos_n=0)
    gone = _item(db, conn.tenant_id, 2, status=OfferingStatus.ARCHIVED)
    out = _item(db, conn.tenant_id, 3, availability=OfferingAvailability.OUT_OF_STOCK)
    foreign = _item(db, make_tenant(db).id, 4)
    ok = _item(db, conn.tenant_id, 5)
    msg = _msg(_meta(bare, gone, out))
    assert await photos.plan_followups(db, conn, chat, msg) == []
    msg = _msg(_meta(foreign, ok))
    assert await photos.plan_followups(db, conn, chat, msg) == [
        _photo_step(msg.id, _cover(db, ok).id)
    ]


async def test_the_photo_comes_from_our_rows_not_from_the_cover_id_in_the_meta(db, conn):
    chat = _wa_chat(db, conn)
    foreign_image = _cover(db, _item(db, make_tenant(db).id, 1))
    mine = _item(db, conn.tenant_id, 2)
    meta = _meta(mine)
    meta["offerings"]["items"][0]["cover_image_id"] = str(foreign_image.id)
    msg = _msg(meta)
    assert await photos.plan_followups(db, conn, chat, msg) == [
        _photo_step(msg.id, _cover(db, mine).id)
    ]


async def test_a_reply_without_offerings_meta_plans_nothing(db, conn):
    chat = _wa_chat(db, conn)
    for meta in (None, {}, {"offerings": None}, {"offerings": {"mode": "none", "items": []}}):
        assert await photos.plan_followups(db, conn, chat, _msg(meta)) == []


async def test_a_planning_failure_yields_no_followups_never_an_exception(db, conn, monkeypatch):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    monkeypatch.setattr(photos, "_cover_image_ids", lambda *a: 1 / 0)
    assert await photos.plan_followups(db, conn, chat, _msg(_meta(row))) == []


# --- the quote text: catalog quotes only
async def test_a_catalog_quote_is_planned_as_text_before_the_photos(db, conn):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    msg = _msg(_meta(row, quote=CATALOG_QUOTE))
    steps = await photos.plan_followups(db, conn, chat, msg)
    assert steps == [
        {
            "kind": "text",
            "text": render_quote_text(CATALOG_QUOTE),
            "key": f"quote:{msg.id}",
            "mark": str(msg.id),
        },
        _photo_step(msg.id, _cover(db, row).id),
    ]


@pytest.mark.parametrize(
    "meta_of",
    [
        lambda row: {"quote": TIER_QUOTE},  # tenant without a catalog turn
        lambda row: {"quote": TIER_QUOTE, "offerings": {"mode": "none", "items": []}},
        lambda row: {**_meta(row), "quote": TIER_QUOTE},  # labels are plans, not items
    ],
)
async def test_a_plan_tier_quote_is_never_sent_as_a_whatsapp_quote_text(db, conn, meta_of):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    steps = await photos.plan_followups(db, conn, chat, _msg(meta_of(row)))
    assert [s for s in steps if s["kind"] == "text"] == []


async def test_a_catalog_quote_on_a_turn_that_showed_no_card_is_still_sent(db, conn):
    """'yes, the two' after the items were shown earlier: mode is none but the cart is real."""
    chat = _wa_chat(db, conn)
    _item(db, conn.tenant_id, 1, photos_n=0)
    msg = _msg({"offerings": {"mode": "none", "items": []}, "quote": CATALOG_QUOTE})
    steps = await photos.plan_followups(db, conn, chat, msg)
    assert [s["kind"] for s in steps] == ["text"]


# --- redelivery: the quote text is derived from the stored quote
def _stored_quote_message(db, conn, chat, *, meta) -> Message:
    session = ConversationSession(
        tenant_id=conn.tenant_id,
        user_identifier=f"wa_{uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
        source="whatsapp",
    )
    db.add(session)
    db.flush()
    chat.session_id = session.id
    message = Message(session_id=session.id, role=MessageRole.ASSISTANT, content="ok", meta=meta)
    db.add(message)
    db.commit()
    return message


async def test_a_redelivered_event_recovers_the_quote_text_the_first_attempt_never_queued(db, conn):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1, photos_n=0)
    first = _stored_quote_message(db, conn, chat, meta=_meta(row, quote=CATALOG_QUOTE))
    # attempt 2: S2 suppressed the duplicate quote (same totals), so the new reply carries none
    retry = _msg(_meta(row))
    steps = await photos.plan_followups(db, conn, chat, retry)
    [text] = [s for s in steps if s["kind"] == "text"]
    assert text["text"] == render_quote_text(CATALOG_QUOTE)
    assert text["key"] == f"quote:{first.id}" and text["mark"] == str(first.id)


async def test_a_quote_text_already_queued_is_not_recovered_again(db, conn):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1, photos_n=0)
    first = _stored_quote_message(db, conn, chat, meta=_meta(row, quote=CATALOG_QUOTE))
    await photos.mark_quote_queued(str(first.id))
    assert await photos.plan_followups(db, conn, chat, _msg(_meta(row))) == []


async def test_an_old_quote_is_not_recovered(db, conn):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1, photos_n=0)
    first = _stored_quote_message(db, conn, chat, meta=_meta(row, quote=CATALOG_QUOTE))
    first.created_at = utcnow_aware() - timedelta(hours=2)
    db.commit()
    assert await photos.plan_followups(db, conn, chat, _msg(_meta(row))) == []


# ------------------------------------------------------------------ running the chain
async def test_run_followups_queues_the_quote_text_carrying_the_photos_behind_it(db, conn, queue):
    chat = _wa_chat(db, conn)
    mid = uuid4()
    text = {"kind": "text", "text": "Total: ₦1", "key": f"quote:{mid}", "mark": str(mid)}
    photo = _photo_step(mid, uuid4())
    await photos.run_followups(conn.id, chat.id, [text, photo])
    [sent] = queue.sends
    assert sent["key"] == f"quote:{mid}" and sent["text"] == "Total: ₦1" and sent["guard"] is True
    assert sent["followups"] == [photo]  # the photos wait for THIS text to be sent
    assert queue.offering_images == []
    assert await photos.quote_already_queued(str(mid)) is True


async def test_run_followups_queues_photos_staggered_with_headroom_trim(db, conn, queue):
    chat = _wa_chat(db, conn)
    rows = [_item(db, conn.tenant_id, n) for n in (1, 2, 3)]
    ids = [_cover(db, r).id for r in rows]
    mid = uuid4()
    await photos.run_followups(conn.id, chat.id, [_photo_step(mid, *ids)])
    # the per-minute connection budget is 2: the third photo is not even queued
    assert [q["offering_image_id"] for q in queue.offering_images] == ids[:2]
    assert [q["key"] for q in queue.offering_images] == [f"offimg:{mid}:{i}" for i in ids[:2]]
    delays = [q["defer_s"] for q in queue.offering_images]
    assert delays == sorted(delays) and delays[0] > 0 and len(set(delays)) == 2
    # looking is not spending: the budget is only used when a photo is actually sent
    assert await photos.photo_headroom(chat.id, conn.id, 3) == 2


async def test_run_followups_queues_no_photo_when_the_connection_has_no_headroom(db, conn, queue):
    chat = _wa_chat(db, conn)
    other = _wa_chat(db, conn)
    assert await photos.take_photo_budget(other.id, conn.id, 2) == 2  # the minute budget is gone
    row = _item(db, conn.tenant_id, 1)
    await photos.run_followups(conn.id, chat.id, [_photo_step(uuid4(), _cover(db, row).id)])
    assert queue.offering_images == [] and queue.sends == []


async def test_run_followups_never_raises(db, conn, queue, monkeypatch):
    chat = _wa_chat(db, conn)
    queue.fail_offering_images = True
    row = _item(db, conn.tenant_id, 1)
    await photos.run_followups(conn.id, chat.id, [_photo_step(uuid4(), _cover(db, row).id)])
    monkeypatch.setattr(settings, "REDIS_URL", DEAD_REDIS)
    await photos.run_followups(conn.id, chat.id, [_photo_step(uuid4(), uuid4())])  # no raise


# ------------------------------------------------------------------ ordering: the text job's success
def _sealed(steps):
    return encrypt_field(json.dumps(steps))


async def _text_job(db, conn, chat, steps, monkeypatch, ctx=None):
    monkeypatch.setattr("app.workers.whatsapp.SessionLocal", lambda: _NoCloseSession(db))
    return await send_whatsapp_message(
        ctx or {},
        str(conn.id),
        str(chat.id),
        encrypt_field("Here it is"),
        True,
        None,
        _sealed(steps),
    )


async def test_the_followups_are_queued_only_after_the_reply_was_sent(
    db, conn, fake_sidecar, queue, monkeypatch
):
    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    mid = uuid4()
    steps = [
        {"kind": "text", "text": "Total: ₦1", "key": f"quote:{mid}", "mark": str(mid)},
        _photo_step(mid, _cover(db, row).id),
    ]
    assert await _text_job(db, conn, chat, steps, monkeypatch) == "sent"
    assert [m["text"] for m in fake_sidecar.sent] == ["Here it is"]
    [quote] = queue.sends
    assert quote["key"] == f"quote:{mid}" and quote["followups"] == [steps[1]]


async def test_nothing_follows_a_reply_that_was_not_sent(
    db, conn, fake_sidecar, queue, monkeypatch
):
    chat = _wa_chat(db, conn)
    chat.human_until = utcnow_aware() + timedelta(minutes=30)  # the owner took over
    db.commit()
    steps = [{"kind": "text", "text": "Total: ₦1", "key": "quote:x", "mark": "x"}]
    assert await _text_job(db, conn, chat, steps, monkeypatch) == "dropped_paused"
    assert queue.sends == [] and queue.offering_images == [] and fake_sidecar.sent == []


async def test_a_reply_that_is_backing_off_releases_nothing_so_nothing_overtakes_it(
    db, conn, fake_sidecar, queue, monkeypatch
):
    chat = _wa_chat(db, conn)
    fake_sidecar.send_error = (429, "rate_cap")
    steps = [{"kind": "text", "text": "Total: ₦1", "key": "quote:x", "mark": "x"}]
    with pytest.raises(Retry):
        await _text_job(db, conn, chat, steps, monkeypatch)
    assert queue.sends == [] and queue.offering_images == []


async def test_a_failing_chain_never_fails_or_repeats_the_reply(
    db, conn, fake_sidecar, queue, monkeypatch
):
    chat = _wa_chat(db, conn)
    queue.fail_offering_images = True
    row = _item(db, conn.tenant_id, 1)
    steps = [_photo_step(uuid4(), _cover(db, row).id)]
    assert await _text_job(db, conn, chat, steps, monkeypatch) == "sent"  # no exception: no retry


# ------------------------------------------------------------------ the inbound pipeline
def _reply(meta: dict | None, content: str = "Here is the Canvas sneaker (#1).", session_id=None):
    message = SimpleNamespace(id=uuid4(), role=MessageRole.ASSISTANT, content=content, meta=meta)
    return SimpleNamespace(session_id=session_id or uuid4(), message=message)


def _patched_send_message(response):
    return patch("app.api.v1.endpoints.chat.send_message", new=AsyncMock(return_value=response))


async def _drive(db, conn, response, event=None):
    with (
        _patched_send_message(response),
        patch.object(whatsapp_inbound, "_usable_session", return_value=None),
        patch.object(whatsapp_inbound, "_plan_blocks_conversation", return_value=False),
    ):
        return await handle_event(db, event or inbound(conn, "show me sneakers"))


async def test_inbound_queues_only_the_reply_and_it_carries_the_followups(db, conn, queue):
    row = _item(db, conn.tenant_id, 1)
    response = _reply(_meta(row, quote=CATALOG_QUOTE))
    assert await _drive(db, conn, response) == "ai_reply"
    [reply] = queue.sends  # no quote text and no photo queued before the reply is sent
    assert reply["key"] == f"ai:{response.message.id}" and reply["text"] == response.message.content
    assert [s["kind"] for s in reply["followups"]] == ["text", "photos"]
    assert queue.offering_images == []


async def test_a_plain_reply_has_no_followups_key(db, conn, queue):
    response = _reply(None)
    assert await _drive(db, conn, response) == "ai_reply"
    [reply] = queue.sends
    assert "followups" not in reply


async def test_the_reply_is_queued_even_when_planning_the_followups_breaks(
    db, conn, queue, monkeypatch
):
    row = _item(db, conn.tenant_id, 1)
    monkeypatch.setattr(photos, "_cover_image_ids", lambda *a: 1 / 0)
    assert await _drive(db, conn, _reply(_meta(row))) == "ai_reply"
    assert [s["key"].split(":")[0] for s in queue.sends] == ["ai"]


async def test_followups_ride_on_the_image_message_reply_too(
    db, conn, queue, fake_sidecar, media_stubs
):
    from tests.whatsapp.conftest import png_bytes

    row = _item(db, conn.tenant_id, 1)
    response = _reply(_meta(row, quote=CATALOG_QUOTE))
    with _patched_send_message(response):
        out = await handle_event(
            db, inbound_image(conn, fake_sidecar, png_bytes(), caption="this?")
        )
    assert out == "ai_reply"
    [reply] = queue.sends
    assert [s["kind"] for s in reply["followups"]] == ["text", "photos"]


async def test_with_redis_down_the_ai_text_is_still_queued(db, conn, queue, monkeypatch):
    """Dead Redis: the limiter and the quote marker fail, the reply text must not."""
    row = _item(db, conn.tenant_id, 1)
    monkeypatch.setattr(settings, "REDIS_URL", DEAD_REDIS)
    response = _reply(_meta(row, quote=CATALOG_QUOTE))
    assert await _drive(db, conn, response) == "ai_reply"
    assert [s["key"] for s in queue.sends] == [f"ai:{response.message.id}"]


async def test_a_retried_event_ends_with_the_quote_text_of_the_first_attempt(db, conn, queue):
    """The reply could not be queued, so arq runs the event again; S2 then suppresses the duplicate
    quote (same totals) and the new reply carries none. The quote text is recovered from the quote
    the first attempt stored, because its text was never queued."""
    row = _item(db, conn.tenant_id, 1, photos_n=0)
    session = ConversationSession(
        tenant_id=conn.tenant_id,
        user_identifier=f"wa_{uuid4().hex[:8]}",
        status=SessionStatus.ACTIVE,
        source="whatsapp",
    )
    db.add(session)
    db.flush()
    stored = Message(  # what attempt 1 persisted before its reply enqueue failed
        session_id=session.id,
        role=MessageRole.ASSISTANT,
        content="ok",
        meta=_meta(row, quote=CATALOG_QUOTE),
    )
    db.add(stored)
    db.commit()
    retry = _reply(_meta(row), session_id=session.id)  # no quote: suppressed by S2
    assert await _drive(db, conn, retry) == "ai_reply"
    [reply] = queue.sends
    [text] = [s for s in reply["followups"] if s["kind"] == "text"]
    assert text["text"] == render_quote_text(CATALOG_QUOTE) and text["mark"] == str(stored.id)


# ------------------------------------------------------------------ retention
async def test_a_catalog_photo_is_not_a_chat_attachment_and_retention_cannot_touch_it(
    db, conn, queue, fake_sidecar, monkeypatch
):
    from app.workers import attachment_retention as retention

    chat = _wa_chat(db, conn)
    row = _item(db, conn.tenant_id, 1)
    image = _cover(db, row)
    steps = await photos.plan_followups(db, conn, chat, _msg(_meta(row)))
    assert steps and await _send(db, conn, chat, image) == "sent"
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
