"""Outbound hook for `connection_manager.fanout_event`.

Agent / system frames addressed to the customer (`to_customer=True`) on a WhatsApp
conversation are forwarded to WhatsApp as an arq job. Called once from `fanout_event` at the
originating worker (NOT from `_deliver_event_local`, which re-fires on every worker via
Redis). AI replies are enqueued by the inbound job, and WS-only frames (typing, presence,
`agent_left`) never pass through `fanout_event`, so they are never forwarded.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db.models.conversation import (
    AttachmentDirection,
    AttachmentKind,
    AttachmentStatus,
    MessageAttachment,
)
from app.db.models.whatsapp import WhatsAppChat
from app.services.whatsapp_outbound import media_url_allowed
from app.utils.datetime import ensure_utc_aware, utcnow_aware

TICKET_RESOLVED_TEXT = (
    "Your request has been marked as resolved. Reply here any time if you need more help."
)
TICKET_CLOSED_TEXT = "This conversation has been closed. Reply here any time to start a new one."
RATING_INSTRUCTION = "Reply with a number from 1 (poor) to 5 (great)."
IMAGE_ONLY_PLACEHOLDER = "[image]"  # chat.py's stand-in text for an image-only message
CAPTION_MAX = 1024  # WhatsApp's caption limit; a longer text goes as its own message
MAX_IMAGES_PER_FRAME = 10  # MAX_ATTACHMENTS_PER_MESSAGE is far lower; belt and braces


def _rating_text(frame: dict) -> str:
    """The rating request prompt plus how to answer it. Never includes the token."""
    first = (frame.get("agent_name") or "").strip()
    prompt = f"How was your chat with {first}?" if first else (frame.get("content") or "").strip()
    return f"{prompt or 'How was your chat with our team?'} {RATING_INSTRUCTION}"


def _join_text(frame: dict) -> str:
    parts = (frame.get("agent_name") or "").split()
    return f"You're now chatting with {parts[0] if parts else 'a member of our team'}."


def frame_text(frame: dict, session_id: UUID | None = None) -> tuple[str, str] | None:
    """(idempotency key, text) for a forwardable frame, else None.

    `agent_joined` needs `session_id` for its key: one join line per (conversation, agent), so a
    retried or repeated takeover by the same agent sends once and a different agent sends again.

    An agent message that is only an image has no text to forward (the image itself is sent
    by `maybe_forward`), so the `[image]` placeholder maps to None.
    """
    ftype = frame.get("type")
    if ftype in ("agent_message", "ticket_missed"):
        content = (frame.get("content") or "").strip()
        if not content or (frame.get("attachments") and content == IMAGE_ONLY_PLACEHOLDER):
            return None
        return f"{ftype}:{frame.get('message_id')}", content
    if ftype == "ticket_resolved":
        return f"ticket_resolved:{frame.get('ticket_id')}", TICKET_RESOLVED_TEXT
    if ftype == "ticket_closed":
        return f"ticket_closed:{frame.get('ticket_id')}", TICKET_CLOSED_TEXT
    if ftype == "agent_joined" and session_id is not None and frame.get("agent_id"):
        return f"agent_joined:{session_id}:{frame['agent_id']}", _join_text(frame)
    if ftype == "rating_requested" and frame.get("request_id"):
        return f"rating_requested:{frame.get('request_id')}", _rating_text(frame)
    return None


def _frame_image_ids(frame: dict) -> list[UUID]:
    """Attachment ids named by an agent message frame. Only LOOKUP KEYS: the rows decide
    everything else (tenant, direction, kind, URL), never the frame."""
    if frame.get("type") != "agent_message":
        return []
    ids: list[UUID] = []
    for att in frame.get("attachments") or []:
        try:
            ids.append(UUID(str(att.get("id"))))
        except (ValueError, AttributeError, TypeError):
            continue
    return list(dict.fromkeys(ids))[:MAX_IMAGES_PER_FRAME]


def _find_chat(session_id: UUID, image_ids: list[UUID] | None = None):  # type: ignore[no-untyped-def]
    """(connection_id, chat_id, forwardable image ids in frame order, owner-paused) or None.

    An image is forwardable only if it is a LINKED, outbound image of THIS conversation (same
    session, so also the same tenant) whose stored URL is on the allowed media host.
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(
            select(WhatsAppChat.connection_id, WhatsAppChat.id, WhatsAppChat.human_until).where(
                WhatsAppChat.session_id == session_id
            )
        ).first()
        if not row:
            return None
        usable: list[UUID] = []
        if image_ids:
            rows = db.execute(
                select(MessageAttachment).where(
                    MessageAttachment.id.in_(image_ids),
                    MessageAttachment.session_id == session_id,
                    MessageAttachment.direction == AttachmentDirection.OUTBOUND,
                    MessageAttachment.kind == AttachmentKind.IMAGE,
                    MessageAttachment.status == AttachmentStatus.LINKED,
                )
            ).scalars()
            by_id = {a.id: a for a in rows if media_url_allowed(a.url)}
            usable = [i for i in image_ids if i in by_id]
        paused_until = ensure_utc_aware(row[2])
        return row[0], row[1], usable, paused_until is not None and paused_until > utcnow_aware()
    finally:
        db.close()


async def maybe_forward(session_id: UUID, message: dict, to_customer: bool) -> None:
    """Never raises: a WhatsApp problem must not break the web chat fanout."""
    if not to_customer or not settings.WHATSAPP_ENABLED:
        return
    mapped = frame_text(message, session_id)
    image_ids = _frame_image_ids(message)
    if mapped is None and not image_ids:
        return
    try:
        found = await asyncio.to_thread(_find_chat, session_id, image_ids)
        if found is None:
            return
        from app.services import whatsapp_enqueue

        connection_id, chat_id, usable, owner_paused = found
        if message.get("type") == "agent_joined" and owner_paused:
            return  # the owner is on the phone: no automatic notice into their conversation
        if image_ids and not usable:
            logger.warning("whatsapp forward: no image of this frame is forwardable")
        # The agent's text rides as the first image's caption when it fits; otherwise (or when
        # no image can be sent) it goes as its own text message so nothing the agent wrote is lost.
        text_alone = mapped
        if usable:
            caption = mapped[1] if mapped and len(mapped[1]) <= CAPTION_MAX else None
            for index, attachment_id in enumerate(usable):
                await whatsapp_enqueue.enqueue_send_image(
                    connection_id,
                    chat_id,
                    attachment_id,
                    caption if index == 0 else None,
                    f"agent_img:{attachment_id}",
                )
            if caption is not None:
                text_alone = None
        if text_alone is not None:
            key, text = text_alone
            rating_id = (
                message.get("request_id") if message.get("type") == "rating_requested" else None
            )
            await whatsapp_enqueue.enqueue_send(
                connection_id,
                chat_id,
                text,
                key,
                rating_request_id=UUID(str(rating_id)) if rating_id else None,
            )
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp outbound forward failed")
