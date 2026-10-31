"""WhatsApp outbound sender (arq job body): hard stop unless connected, retry transient errors.

Pacing / jitter / read receipts / composing live in the sidecar. This layer adds:
idempotency (the enqueue key is the arq job id), a backend-side reply-only guard, a hard
stop when the connection is not `connected`, and retry-with-backoff for transient errors.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.channels import identity
from app.channels.base import (
    MediaRejected,
    NotConnected,
    OutboundMessage,
    RateCapped,
    ReplyOnly,
    SidecarUnavailable,
)
from app.channels.registry import get_adapter
from app.core.config import settings
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
)
from app.db.models.review import RatingRequest
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppStatus
from app.utils.datetime import ensure_utc_aware, utcnow_aware
from app.offerings.alerts import tenant_currency
from app.offerings.facts import facts_from_row
from app.services.whatsapp_exclusion import is_excluded
from app.services.whatsapp_offering_photos import (
    build_caption,
    claim_photo_send,
    live_catalog_photo,
    offerings_allowed,
    take_photo_budget,
    whatsapp_rendition,
)
from app.services.whatsapp_inbound import _tenant_may_use

MAX_SEND_TRIES = 5
IMAGE_CAPTION_MAX = 1024  # WhatsApp's caption limit
_BACKOFF_S = (5, 15, 45, 120)


class RetrySend(Exception):  # noqa: N818 - control-flow signal, mapped to arq.Retry by the job
    def __init__(self, defer_s: int) -> None:
        super().__init__(f"retry in {defer_s}s")
        self.defer_s = defer_s


def backoff_seconds(job_try: int) -> int:
    return _BACKOFF_S[min(max(job_try, 1) - 1, len(_BACKOFF_S) - 1)]


def _preflight(
    db: Session, connection_id: UUID, chat_id: UUID, guard: bool
) -> tuple[WhatsAppConnection, WhatsAppChat] | str:
    """The checks every outbound send shares. Returns (conn, chat) or the dropped_* label."""
    conn = db.get(WhatsAppConnection, connection_id)
    chat = db.get(WhatsAppChat, chat_id)
    if conn is None or chat is None or chat.connection_id != conn.id:
        return "dropped_missing"
    if not _tenant_may_use(db, conn.tenant_id):  # kill switch or revoked grant
        return "dropped_disabled"
    try:
        excluded = is_excluded(db, conn.id, ref=chat.contact_ref)
    except identity.PepperMissing:
        logger.error("WHATSAPP_CONTACT_PEPPER is not configured; send will be retried")
        raise RetrySend(backoff_seconds(1)) from None
    if excluded:
        # Listed after this send was queued (or an agent typed into an old conversation): the
        # contact is ignored entirely, so nothing, automatic or human, goes out to them.
        return "dropped_excluded"
    if guard:
        # Automatic replies (AI / fixed fallback) were queued earlier: re-check the conditions
        # that may have changed meanwhile (the owner took over, or AI was switched off).
        human_until = ensure_utc_aware(chat.human_until)
        if human_until is not None and human_until > utcnow_aware():
            return "dropped_paused"
        if not conn.ai_enabled:
            return "dropped_ai_disabled"
    if conn.status != WhatsAppStatus.CONNECTED.value:
        # Hard stop: sending on a half-linked session risks the number. Never retried.
        logger.warning("whatsapp send skipped: connection {} is {}", conn.id, conn.status)
        return "dropped_not_connected"
    if chat.last_inbound_at is None:
        return "dropped_reply_only"  # linked-device numbers only reply to chats that wrote first
    return conn, chat


def media_url_allowed(url: str | None) -> bool:
    """https, no credentials / odd port, and the host is on the stored-media allowlist."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    hosts = {h.strip().lower() for h in settings.WHATSAPP_MEDIA_URL_HOSTS.split(",") if h.strip()}
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
        and (parts.hostname or "").lower() in hosts
    )


def outbound_image_url(
    db: Session, conn: WhatsAppConnection, chat: WhatsAppChat, attachment_id: UUID
) -> str | None:
    """The URL of an image WE stored for this chat's conversation, else None.

    The job carries only the attachment id: the URL is read from our own row here, so nothing an
    agent typed or a frame carried can become a fetch target for the sidecar. The row must be a
    linked, outbound IMAGE of this tenant's conversation with this chat, on an allowed host.
    """
    att = db.get(MessageAttachment, attachment_id)
    if (
        att is None
        or att.tenant_id != conn.tenant_id
        or chat.session_id is None
        or att.session_id != chat.session_id
        or att.direction != AttachmentDirection.OUTBOUND
        or att.kind != AttachmentKind.IMAGE
        or att.status != AttachmentStatus.LINKED
        or not (att.content_type or "").startswith("image/")
    ):
        return None
    return att.url if media_url_allowed(att.url) else None


def _remember_rating_request(db: Session, chat: WhatsAppChat, request_id: UUID) -> None:
    """The prompt reached the customer: a 1-5 reply may now answer this request.

    Only a request of THIS chat's conversation counts (same tenant, session and not yet
    fulfilled); anything else is ignored so a stray id can never open a rating window.
    """
    request = db.get(RatingRequest, request_id)
    if (
        request is None
        or request.tenant_id != chat.tenant_id
        or request.session_id != chat.session_id
        or request.fulfilled_at is not None
    ):
        logger.warning("whatsapp rating prompt sent for an unusable request; not remembered")
        return
    chat.rating_request_id = request.id
    chat.rating_request_expires_at = request.expires_at
    db.commit()


async def send_text(
    db: Session,
    connection_id: UUID,
    chat_id: UUID,
    text: str,
    job_try: int,
    guard: bool = False,
    rating_request_id: UUID | None = None,
) -> str:
    """Returns an outcome label; raises RetrySend for transient failures."""
    checked = _preflight(db, connection_id, chat_id, guard)
    if isinstance(checked, str):
        return checked
    conn, chat = checked
    text = text.strip()
    if not text:
        return "dropped_empty"
    max_len = get_adapter(conn.provider).capabilities.max_text_len
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    try:
        await get_adapter(conn.provider).send(
            conn.session_key, OutboundMessage(chat_ref=chat.contact_ref, text=text)
        )
    except (NotConnected, ReplyOnly) as exc:
        logger.warning("whatsapp send refused by sidecar: {}", exc.code)
        return f"dropped_{exc.code}"
    except (RateCapped, SidecarUnavailable) as exc:
        return _retry_or_give_up(job_try, exc.code)
    if rating_request_id is not None:
        _remember_rating_request(db, chat, rating_request_id)
    return "sent"


async def send_image(
    db: Session,
    connection_id: UUID,
    chat_id: UUID,
    attachment_id: UUID,
    caption: str | None,
    job_try: int,
) -> str:
    """One agent image to the customer. Same hard stops / retry policy as text; the sidecar's
    400 / 413 / 415 (bad URL, too large, wrong type) are permanent and never retried."""
    checked = _preflight(db, connection_id, chat_id, guard=False)
    if isinstance(checked, str):
        return checked
    conn, chat = checked
    url = outbound_image_url(db, conn, chat, attachment_id)
    if url is None:
        logger.warning("whatsapp image send dropped: attachment is not forwardable")
        return "dropped_media_not_allowed"
    caption = (caption or "").strip()[:IMAGE_CAPTION_MAX] or None
    try:
        await get_adapter(conn.provider).send(
            conn.session_key,
            OutboundMessage(chat_ref=chat.contact_ref, image_url=url, image_caption=caption),
        )
    except (NotConnected, ReplyOnly, MediaRejected) as exc:
        logger.warning("whatsapp image refused by sidecar: {}", exc.code)
        return f"dropped_{exc.code}"
    except (RateCapped, SidecarUnavailable) as exc:
        return _retry_or_give_up(job_try, exc.code)
    return "sent"


async def send_offering_image(
    db: Session, connection_id: UUID, chat_id: UUID, offering_image_id: UUID, claim_key: str
) -> str:
    """One catalog photo to the customer, an automatic reply (pause / AI-off are re-checked).

    Everything is re-read here, never trusted from the job: the photo and its item must belong to
    this connection's tenant, the item must still be active and not sold out, the catalog must
    still be on for the tenant (owner switch and plan), and the stored URL must be https on the
    media allowlist. The caption is built now from the live row, so a price or availability changed
    after the reply was queued is the one the customer sees. The photo is the WhatsApp rendition
    (at most 1,280 px, JPEG, about 1 MB).

    Never retried: the sidecar shares one send budget per number with every chat's text, so a photo
    that is rate-capped, times out or fails is dropped (a lost photo is cheap; a repeat is a ban
    risk and a second spend of the daily cap). `claim_key` (the arq job id) makes a redelivered job
    a no-op, and the per-chat and per-connection photo budgets are spent here, only on a real send.
    """
    checked = _preflight(db, connection_id, chat_id, guard=True)
    if isinstance(checked, str):
        return checked
    conn, chat = checked
    live = live_catalog_photo(db, conn.tenant_id, offering_image_id)
    if live is None:
        logger.info("whatsapp catalog photo dropped: item or photo is not showable now")
        return "dropped_offering_unavailable"
    if not offerings_allowed(db, conn.tenant_id):
        return "dropped_offerings_off"
    item, image = live
    url = whatsapp_rendition(image.url)
    if not media_url_allowed(image.url) or not media_url_allowed(url):
        logger.warning("whatsapp catalog photo dropped: stored url is not on the allowlist")
        return "dropped_media_not_allowed"
    claimed = await claim_photo_send(claim_key)
    if claimed is None:
        return "dropped_limiter_unavailable"
    if not claimed:
        return "dropped_duplicate"
    if await take_photo_budget(chat.id, conn.id, 1) < 1:
        logger.info("whatsapp catalog photo dropped: photo budget used up")
        return "dropped_photo_budget"
    caption = build_caption(facts_from_row(item), tenant_currency(db, conn.tenant_id))
    try:
        await get_adapter(conn.provider).send(
            conn.session_key,
            OutboundMessage(chat_ref=chat.contact_ref, image_url=url, image_caption=caption),
        )
    except (NotConnected, ReplyOnly, MediaRejected) as exc:
        logger.warning("whatsapp catalog photo refused by sidecar: {}", exc.code)
        return f"dropped_{exc.code}"
    except RateCapped:
        return "dropped_rate_capped"
    except SidecarUnavailable:
        return "dropped_sidecar_unavailable"
    return "sent"


def _retry_or_give_up(job_try: int, code: str) -> str:
    if job_try >= MAX_SEND_TRIES:
        logger.error("whatsapp send gave up after {} tries: {}", job_try, code)
        return "dropped_gave_up"
    raise RetrySend(backoff_seconds(job_try))
