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
from app.db.models.whatsapp import WhatsAppChat

TICKET_RESOLVED_TEXT = (
    "Your request has been marked as resolved. Reply here any time if you need more help."
)
TICKET_CLOSED_TEXT = "This conversation has been closed. Reply here any time to start a new one."
IMAGE_NOTICE = "Your support agent sent an image, which can't be shown in WhatsApp yet."


def frame_text(frame: dict) -> tuple[str, str] | None:
    """(idempotency key, text) for a forwardable frame, else None."""
    ftype = frame.get("type")
    if ftype in ("agent_message", "ticket_missed"):
        content = (frame.get("content") or "").strip()
        if frame.get("attachments") and (not content or content == "[image]"):
            content = IMAGE_NOTICE
        if not content:
            return None
        return f"{ftype}:{frame.get('message_id')}", content
    if ftype == "ticket_resolved":
        return f"ticket_resolved:{frame.get('ticket_id')}", TICKET_RESOLVED_TEXT
    if ftype == "ticket_closed":
        return f"ticket_closed:{frame.get('ticket_id')}", TICKET_CLOSED_TEXT
    return None


def _find_chat(session_id: UUID) -> tuple[UUID, UUID] | None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(
            select(WhatsAppChat.connection_id, WhatsAppChat.id).where(
                WhatsAppChat.session_id == session_id
            )
        ).first()
        return (row[0], row[1]) if row else None
    finally:
        db.close()


async def maybe_forward(session_id: UUID, message: dict, to_customer: bool) -> None:
    """Never raises: a WhatsApp problem must not break the web chat fanout."""
    if not to_customer or not settings.WHATSAPP_ENABLED:
        return
    mapped = frame_text(message)
    if mapped is None:
        return
    try:
        found = await asyncio.to_thread(_find_chat, session_id)
        if found is None:
            return
        from app.services import whatsapp_enqueue

        key, text = mapped
        await whatsapp_enqueue.enqueue_send(found[0], found[1], text, key)
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp outbound forward failed")
