"""WhatsApp outbound sender (arq job body): hard stop unless connected, retry transient errors.

Pacing / jitter / read receipts / composing live in the sidecar. This layer adds:
idempotency (the enqueue key is the arq job id), a backend-side reply-only guard, a hard
stop when the connection is not `connected`, and retry-with-backoff for transient errors.
"""

from __future__ import annotations

from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.channels.base import (
    NotConnected,
    OutboundMessage,
    RateCapped,
    ReplyOnly,
    SidecarUnavailable,
)
from app.channels.registry import get_adapter
from app.core.config import settings
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppStatus

MAX_SEND_TRIES = 5
_BACKOFF_S = (5, 15, 45, 120)


class RetrySend(Exception):  # noqa: N818 - control-flow signal, mapped to arq.Retry by the job
    def __init__(self, defer_s: int) -> None:
        super().__init__(f"retry in {defer_s}s")
        self.defer_s = defer_s


def backoff_seconds(job_try: int) -> int:
    return _BACKOFF_S[min(max(job_try, 1) - 1, len(_BACKOFF_S) - 1)]


async def send_text(
    db: Session, connection_id: UUID, chat_id: UUID, text: str, job_try: int
) -> str:
    """Returns an outcome label; raises RetrySend for transient failures."""
    conn = db.get(WhatsAppConnection, connection_id)
    chat = db.get(WhatsAppChat, chat_id)
    if conn is None or chat is None or chat.connection_id != conn.id:
        return "dropped_missing"
    if not settings.WHATSAPP_ENABLED:
        return "dropped_disabled"
    if conn.status != WhatsAppStatus.CONNECTED.value:
        # Hard stop: sending on a half-linked session risks the number. Never retried.
        logger.warning("whatsapp send skipped: connection {} is {}", conn.id, conn.status)
        return "dropped_not_connected"
    if chat.last_inbound_at is None:
        return "dropped_reply_only"  # linked-device numbers only reply to chats that wrote first
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
        if job_try >= MAX_SEND_TRIES:
            logger.error("whatsapp send gave up after {} tries: {}", job_try, exc.code)
            return "dropped_gave_up"
        raise RetrySend(backoff_seconds(job_try)) from None
    return "sent"
