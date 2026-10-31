"""Offerings on WhatsApp: the follow-ups to an AI reply: catalog quote text, then cover photos.

How it fits together (spec 11 and 12, hardened after an independent review):

* **Chained, never racing.** The reply text is queued first and carries the follow-up *steps* with
  it. The worker queues them only after the reply was actually sent, so nothing can overtake a
  reply that is backing off after a 429 or a 5xx. A quote text is queued first and carries the
  photos behind it the same way.
* **Photos have their own budgets**, taken when a photo is actually sent (a dropped photo costs
  nothing): 6 per chat per hour, and per CONNECTION a few per minute and a share of the sidecar's
  daily send cap. The sidecar shares one send budget per number between every chat's text and
  these photos, so a photo is skipped, never retried, and never queued while the connection has no
  headroom. One Redis Lua script keeps the three sliding windows atomic; fail-closed for photos
  only (Redis down means no photos, the text is unaffected).
* **Only ids travel.** A photo job carries a row id; the sender re-reads the photo and its item
  (same tenant, item active and not sold out, the catalog still on for the tenant, https host on
  the allowlist) and builds the caption from the live row.
* **Quote text is for catalog quotes only.** A plan-tier quote (software tenants) also lands in
  `meta.quote`; it must not get the catalog wording.
* **Nothing here raises into the inbound pipeline or the text job**: the reply text is already
  queued or sent, and an exception would make arq re-run it and answer the customer twice.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.conversation import Message, MessageRole
from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingStatus,
    TenantOfferingsSettings,
)
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection
from app.offerings.facts import NAME_MAX, OfferingFacts, clean_text
from app.offerings.quote_text import TEAM_LINE
from app.sales.pricing import _currency_symbol
from app.services import whatsapp_enqueue
from app.services.entitlements import tenant_has
from app.services.offering_media import rendition
from app.utils.datetime import ensure_utc_aware, utcnow_aware

MAX_ITEMS_WITH_PHOTOS = 3
MAX_PHOTOS_PER_REPLY = 3
CHAT_PHOTOS_PER_HOUR = 6
CHAT_WINDOW_S = 3600
MINUTE_WINDOW_S = 60
DAY_WINDOW_S = 86400
CAPTION_MAX = 1024  # WhatsApp's caption limit
PHOTO_DEFER_S = 2  # the photos go a little after the text / quote that precede them
PHOTO_DEFER_STEP_S = 2
CLAIM_TTL_S = 86400
QUOTE_MARK_TTL_S = 86400
QUOTE_RECOVERY_WINDOW = timedelta(minutes=15)
WHATSAPP_RENDITION = "c_limit,w_1280,h_1280,q_auto:good,f_jpg"  # at most 1,280 px, never upscaled

_QTY = re.compile(r"^\s*(\d+)\s*×")

# KEYS: chat-hour, connection-minute, connection-day. ARGV: now, token, take(1|0), want, then
# (window, cap) for each key. Grants min(want, the smallest remaining room) and, when `take`, records
# that many entries in all three windows. Without `take` it only looks (headroom).
_BUDGET_LUA = """
local now, token, take, want = tonumber(ARGV[1]), ARGV[2], tonumber(ARGV[3]), tonumber(ARGV[4])
local room = want
for i = 1, 3 do
  local window, cap = tonumber(ARGV[3 + 2 * i]), tonumber(ARGV[4 + 2 * i])
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - window)
  room = math.min(room, math.max(cap - redis.call('ZCARD', KEYS[i]), 0))
end
if take == 1 and room > 0 then
  for i = 1, 3 do
    local window = tonumber(ARGV[3 + 2 * i])
    for n = 1, room do redis.call('ZADD', KEYS[i], now, token .. ':' .. n) end
    redis.call('EXPIRE', KEYS[i], window + 10)
  end
end
return room
"""


# --------------------------------------------------------------------------- rendering
def build_caption(facts: OfferingFacts, currency: str) -> str:
    """`#12 Canvas sneaker · ₦12,000 · In stock`, built from the row read at send time."""
    availability = facts.availability_text()
    caption = f"{facts.ref} {facts.name} · {facts.price_text(currency)} · "
    caption += availability[:1].upper() + availability[1:]
    return caption if len(caption) <= CAPTION_MAX else caption[: CAPTION_MAX - 1].rstrip() + "…"


def whatsapp_rendition(url: str) -> str:
    """The WhatsApp copy of a stored photo: capped in both dimensions, JPEG, at most ~1 MB."""
    return rendition(url, WHATSAPP_RENDITION)


def _money(amount: int, currency: str) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}{_currency_symbol(currency)}{abs(amount):,}"


def quote_message_text(quote: dict) -> str:
    """The quote as a plain WhatsApp message (same wording as S2's `render_quote_text`, whose
    positive-amount output a test pins), with the sign before the symbol for a negative line."""
    currency = quote.get("currency") or "NGN"
    lines = []
    for item in quote.get("line_items") or []:
        m = _QTY.match(str(item.get("detail") or ""))
        prefix = f"{m.group(1)} × " if m else ""
        lines.append(
            f"{prefix}{item.get('label')} — {_money(int(item.get('amount') or 0), currency)}"
        )
    lines.append(f"Total: {_money(int(quote.get('total') or 0), currency)}")
    lines.append(TEAM_LINE)
    return "\n".join(lines)


# --------------------------------------------------------------------------- Redis helpers
def _client() -> aioredis.Redis:
    return aioredis.from_url(
        settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
    )


async def _close(client: aioredis.Redis) -> None:
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass


def daily_photo_budget() -> int:
    """Catalog photos a connection may send in a day: a share of the sidecar's daily send cap."""
    return max(
        0, int(settings.WHATSAPP_DAILY_SEND_CAP * settings.WHATSAPP_CATALOG_PHOTO_DAILY_PCT / 100)
    )


async def _budget(chat_id: UUID, connection_id: UUID, want: int, *, take: bool) -> int:
    if want <= 0:
        return 0
    client = _client()
    try:
        granted = await client.eval(
            _BUDGET_LUA,
            3,
            f"wa:catphotos:chat:{chat_id}",
            f"wa:catphotos:min:{connection_id}",
            f"wa:catphotos:day:{connection_id}",
            time.time(),
            uuid4().hex,
            1 if take else 0,
            want,
            CHAT_WINDOW_S,
            CHAT_PHOTOS_PER_HOUR,
            MINUTE_WINDOW_S,
            settings.WHATSAPP_CATALOG_PHOTOS_PER_MIN,
            DAY_WINDOW_S,
            daily_photo_budget(),
        )
        return int(granted)
    except Exception as exc:  # noqa: BLE001 - fail closed for photos only
        logger.warning("whatsapp catalog photo limiter unavailable: {}", type(exc).__name__)
        return 0
    finally:
        await _close(client)


async def take_photo_budget(chat_id: UUID, connection_id: UUID, want: int) -> int:
    """Spend up to `want` photo sends (chat per hour, connection per minute and per day)."""
    return await _budget(chat_id, connection_id, want, take=True)


async def photo_headroom(chat_id: UUID, connection_id: UUID, want: int) -> int:
    """How many of `want` photos the budgets would allow right now; spends nothing."""
    return await _budget(chat_id, connection_id, want, take=False)


async def claim_photo_send(claim_key: str) -> bool | None:
    """Once per job id: True the first time, False for a redelivery, None if Redis is down.

    A transport timeout is never retried for a photo, and this stops a redelivered job (a worker
    crash or a lost ack) from sending the same photo, and spending the daily cap, twice.
    """
    client = _client()
    try:
        return bool(await client.set(f"wa:catphoto:sent:{claim_key}", "1", nx=True, ex=CLAIM_TTL_S))
    except Exception as exc:  # noqa: BLE001
        logger.warning("whatsapp catalog photo claim unavailable: {}", type(exc).__name__)
        return None
    finally:
        await _close(client)


async def mark_quote_queued(message_id: str | None) -> None:
    if not message_id:
        return
    client = _client()
    try:
        await client.set(f"wa:catquote:{message_id}", "1", ex=QUOTE_MARK_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("whatsapp quote marker unavailable: {}", type(exc).__name__)
    finally:
        await _close(client)


async def quote_already_queued(message_id: str) -> bool:
    """True when its quote text was queued; True too when Redis cannot say (never resend blind)."""
    client = _client()
    try:
        return bool(await client.exists(f"wa:catquote:{message_id}"))
    except Exception:  # noqa: BLE001
        return True
    finally:
        await _close(client)


# --------------------------------------------------------------------------- send-time re-checks
def live_catalog_photo(
    db: Session, tenant_id: UUID, image_id: UUID
) -> tuple[Offering, OfferingImage] | None:
    """The photo and its item, only if both belong to this tenant and the item may be shown now."""
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


def offerings_allowed(db: Session, tenant_id: UUID) -> bool:
    """The catalog is on for this tenant right now: the owner's switch and the plan."""
    row = db.execute(
        select(TenantOfferingsSettings.enabled).where(
            TenantOfferingsSettings.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if row is not None and not row:
        return False
    return bool(tenant_has(db, tenant_id, "offerings"))


# --------------------------------------------------------------------------- planning
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


def _is_catalog_quote(db: Session, tenant_id: UUID, meta: dict | None) -> bool:
    """A quote built from catalog items, not a plan-tier quote.

    Both kinds land in `meta.quote`. A catalog quote only exists on a turn that has `meta.offerings`
    and its line labels are item names of this tenant; a tier quote's labels are plan names.
    """
    quote = (meta or {}).get("quote")
    if not isinstance(quote, dict) or not isinstance((meta or {}).get("offerings"), dict):
        return False
    labels = {str(i.get("label")) for i in quote.get("line_items") or []}
    if not labels:
        return False
    names = {
        clean_text(name, NAME_MAX)
        for (name,) in db.execute(select(Offering.name).where(Offering.tenant_id == tenant_id))
    }
    return labels <= names


async def _quote_step(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, message_id: UUID, meta: dict | None
) -> dict | None:
    owner_id, quote = message_id, (meta or {}).get("quote")
    if quote is None:
        recovered = await _recover_unsent_quote(db, conn, chat, message_id)
        if recovered is None:
            return None
        owner_id, quote = recovered
    elif not _is_catalog_quote(db, conn.tenant_id, meta):
        return None
    return {
        "kind": "text",
        "text": quote_message_text(quote),
        "key": f"quote:{owner_id}",
        "mark": str(owner_id),
    }


async def _recover_unsent_quote(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, message_id: UUID
) -> tuple[UUID, dict] | None:
    """After a failed reply enqueue the event runs again and S2 suppresses the duplicate quote
    (same totals), so this reply carries none: take the quote the first attempt stored, if its
    text was never queued. Only a recent catalog quote of this very conversation qualifies."""
    if chat.session_id is None:
        return None
    cutoff = utcnow_aware() - QUOTE_RECOVERY_WINDOW
    rows = db.execute(
        select(Message)
        .where(
            Message.session_id == chat.session_id,
            Message.role == MessageRole.ASSISTANT,
            Message.id != message_id,
        )
        .order_by(Message.created_at.desc())
        .limit(5)
    ).scalars()
    for row in rows:
        if ensure_utc_aware(row.created_at) < cutoff:
            return None
        if not (row.meta or {}).get("quote"):
            continue
        if not _is_catalog_quote(db, conn.tenant_id, row.meta):
            return None
        if await quote_already_queued(str(row.id)):
            return None
        return row.id, row.meta["quote"]
    return None


async def plan_followups(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, message: Any
) -> list[dict]:
    """The steps that follow an AI reply: the quote text (catalog quotes only), then the cover
    photos of 1-3 shown items. Pure planning: nothing is queued or spent here. Never raises."""
    meta = getattr(message, "meta", None)
    steps: list[dict] = []
    try:
        if (step := await _quote_step(db, conn, chat, message.id, meta)) is not None:
            steps.append(step)
    except Exception as exc:  # noqa: BLE001 - the reply text must still go
        logger.error("whatsapp quote text skipped: {}", type(exc).__name__)
    try:
        covers = _cover_image_ids(db, conn.tenant_id, _shown_offering_ids(meta))
        if covers[:MAX_PHOTOS_PER_REPLY]:
            steps.append(
                {
                    "kind": "photos",
                    "message_id": str(message.id),
                    "image_ids": [str(i) for i in covers[:MAX_PHOTOS_PER_REPLY]],
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("whatsapp catalog photos skipped: {}", type(exc).__name__)
    return steps


# --------------------------------------------------------------------------- running the chain
async def run_followups(connection_id: UUID, chat_id: UUID, steps: list[dict]) -> None:
    """Queue the first step now (the text job calls this once the reply was SENT); a text step
    carries the remaining steps with it. Photos are trimmed to the connection's headroom, spent
    only when each is sent. Never raises."""
    if not steps:
        return
    step, rest = steps[0], steps[1:]
    try:
        if step.get("kind") == "text":
            await whatsapp_enqueue.enqueue_send(
                connection_id,
                chat_id,
                step["text"],
                step["key"],
                guard=True,
                followups=rest or None,
            )
            await mark_quote_queued(step.get("mark"))
        elif step.get("kind") == "photos":
            ids = [UUID(i) for i in step.get("image_ids") or []][:MAX_PHOTOS_PER_REPLY]
            allowed = await photo_headroom(chat_id, connection_id, len(ids))
            for position, image_id in enumerate(ids[:allowed]):
                await whatsapp_enqueue.enqueue_send_offering_image(
                    connection_id,
                    chat_id,
                    image_id,
                    f"offimg:{step.get('message_id')}:{image_id}",
                    defer_s=PHOTO_DEFER_S + PHOTO_DEFER_STEP_S * position,
                )
    except Exception as exc:  # noqa: BLE001 - the reply is already sent
        logger.error("whatsapp catalog follow-up skipped: {}", type(exc).__name__)
