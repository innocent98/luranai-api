"""Offerings on WhatsApp: which catalog photos follow a reply, and the quote text (spec 11 and 12).

After the AI reply text is enqueued, the cover photo of each of 1-3 shown items goes out as its own
job, and a catalog quote goes as a second plain-text message. Rules, in the order they bite:

* more than 3 shown items is a menu: no photos (no photo spam for a 12-line list);
* at most 3 photos per reply and 6 per chat per hour (Redis sliding window, fail-closed: if the
  limiter is down the photos are skipped, the text is unaffected);
* the item and its photo are re-read from OUR tables (same tenant, item active, not sold out);
  nothing but a row id travels to the sender, and the sender re-checks everything at send time;
* nothing here may raise into the inbound pipeline: the text is already queued, and an exception
  would make arq retry the whole event and answer the customer twice.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingStatus,
)
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection
from app.offerings.facts import OfferingFacts
from app.offerings.quote_text import render_quote_text
from app.services import whatsapp_enqueue

MAX_ITEMS_WITH_PHOTOS = 3
MAX_PHOTOS_PER_REPLY = 3
MAX_PHOTOS_PER_CHAT_PER_HOUR = 6
PHOTO_WINDOW_S = 3600
CAPTION_MAX = 1024  # WhatsApp's caption limit
# The text job is enqueued first; these delays keep the quote text and the photos behind it even
# when the worker has several slots free (the sidecar then paces each send on its own).
QUOTE_DEFER_S = 1
PHOTO_DEFER_S = 2
PHOTO_DEFER_STEP_S = 2

_BUDGET_LUA = """
local key, now, window, cap, want, token = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]),
  tonumber(ARGV[3]), tonumber(ARGV[4]), ARGV[5]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local used = redis.call('ZCARD', key)
local grant = math.min(want, math.max(cap - used, 0))
for i = 1, grant do redis.call('ZADD', key, now, token .. ':' .. i) end
if grant > 0 then redis.call('EXPIRE', key, window + 10) end
return grant
"""


def build_caption(facts: OfferingFacts, currency: str) -> str:
    """`#12 Canvas sneaker · ₦12,000 · In stock`, built from the row read at send time."""
    availability = facts.availability_text()
    caption = f"{facts.ref} {facts.name} · {facts.price_text(currency)} · "
    caption += availability[:1].upper() + availability[1:]
    return caption if len(caption) <= CAPTION_MAX else caption[: CAPTION_MAX - 1].rstrip() + "…"


async def take_photo_budget(chat_id: UUID, want: int) -> int:
    """Reserve up to `want` catalog photos for this chat; returns how many it may send now.

    Fail-closed: any Redis problem grants 0 (photos are skipped, the text is not affected).
    """
    if want <= 0:
        return 0
    client = aioredis.from_url(
        settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
    )
    try:
        granted = await client.eval(
            _BUDGET_LUA,
            1,
            f"wa:catphotos:{chat_id}",
            time.time(),
            PHOTO_WINDOW_S,
            MAX_PHOTOS_PER_CHAT_PER_HOUR,
            want,
            uuid4().hex,
        )
        return int(granted)
    except Exception as exc:  # noqa: BLE001 - fail closed for photos only
        logger.warning("whatsapp catalog photo limiter unavailable: {}", type(exc).__name__)
        return 0
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


def live_catalog_photo(
    db: Session, tenant_id: UUID, image_id: UUID
) -> tuple[Offering, OfferingImage] | None:
    """The photo and its item, only if both belong to this tenant and the item may be shown now.

    Used at selection and again at send time: a photo whose item was archived, sold out, deleted
    or moved to another tenant in between is simply not sent.
    """
    image = db.get(OfferingImage, image_id)
    if image is None or image.tenant_id != tenant_id:
        return None
    item = db.get(Offering, image.offering_id)
    if (
        item is None
        or item.tenant_id != tenant_id
        or item.status != OfferingStatus.ACTIVE
        or item.availability == OfferingAvailability.OUT_OF_STOCK
    ):
        return None
    return item, image


def _shown_offering_ids(meta: dict | None) -> list[UUID]:
    shown = ((meta or {}).get("offerings") or {}).get("items") or []
    if not 1 <= len(shown) <= MAX_ITEMS_WITH_PHOTOS:
        return []
    ids: list[UUID] = []
    for entry in shown:
        try:
            oid = UUID(str(entry.get("offering_id")))
        except (ValueError, AttributeError):
            continue
        if oid not in ids:
            ids.append(oid)
    return ids


def _cover_image_ids(db: Session, tenant_id: UUID, offering_ids: list[UUID]) -> list[UUID]:
    """The first photo (by position) of each shown item that may be shown, in reply order."""
    covers: list[UUID] = []
    for oid in offering_ids:
        image_id = db.execute(
            select(OfferingImage.id)
            .where(OfferingImage.offering_id == oid, OfferingImage.tenant_id == tenant_id)
            .order_by(OfferingImage.position, OfferingImage.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if image_id is not None and live_catalog_photo(db, tenant_id, image_id) is not None:
            covers.append(image_id)
    return covers


async def _enqueue_photos(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, message_id: UUID, meta: dict | None
) -> None:
    covers = _cover_image_ids(db, conn.tenant_id, _shown_offering_ids(meta))[:MAX_PHOTOS_PER_REPLY]
    granted = await take_photo_budget(chat.id, len(covers))
    for position, image_id in enumerate(covers[:granted]):
        await whatsapp_enqueue.enqueue_send_offering_image(
            conn.id,
            chat.id,
            image_id,
            f"offimg:{message_id}:{image_id}",
            defer_s=PHOTO_DEFER_S + PHOTO_DEFER_STEP_S * position,
        )


async def _enqueue_quote_text(
    conn: WhatsAppConnection, chat: WhatsAppChat, message_id: UUID, meta: dict | None
) -> None:
    quote = (meta or {}).get("quote")
    if not isinstance(quote, dict) or not quote.get("line_items"):
        return
    await whatsapp_enqueue.enqueue_send(
        conn.id,
        chat.id,
        render_quote_text(quote),  # every figure comes from the quote row S2 stored
        f"quote:{message_id}",
        guard=True,
        defer_s=QUOTE_DEFER_S,
    )


async def enqueue_catalog_followups(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, message: Any
) -> None:
    """Queue the quote text and the cover photos that follow an AI reply. Never raises."""
    meta = getattr(message, "meta", None)
    if not meta:
        return
    for step in (
        lambda: _enqueue_quote_text(conn, chat, message.id, meta),
        lambda: _enqueue_photos(db, conn, chat, message.id, meta),
    ):
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 - the reply text is already queued
            logger.error("whatsapp catalog follow-up skipped: {}", type(exc).__name__)
