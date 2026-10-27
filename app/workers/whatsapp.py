"""arq jobs for the WhatsApp channel (registered in WorkerSettings.functions)."""

from __future__ import annotations

from uuid import UUID

from arq import Retry
from loguru import logger

from app.db.session import SessionLocal
from app.services import whatsapp_inbound
from app.services.whatsapp_outbound import RetrySend, send_text

_CHAT_LOCK_TTL_S = 120


async def _lock_chat(ctx: dict, event: dict):  # type: ignore[no-untyped-def]
    """Serialise events of one chat so two quick messages cannot race on session creation."""
    redis = ctx.get("redis")
    if redis is None:
        return None
    ref = event.get("from_ref") or event.get("to_ref")
    if not ref:
        return None
    key = f"wa:chatlock:{event['session_key']}:{ref}"
    got = await redis.set(key, "1", ex=_CHAT_LOCK_TTL_S, nx=True)
    if not got:
        raise Retry(defer=2)
    return key


async def process_whatsapp_event(ctx: dict, event: dict) -> str:
    key = await _lock_chat(ctx, event)
    db = SessionLocal()
    try:
        outcome = await whatsapp_inbound.handle_event(db, event)
        logger.info("whatsapp event {} -> {}", event.get("type"), outcome)
        return outcome
    finally:
        db.close()
        if key:
            await ctx["redis"].delete(key)


async def send_whatsapp_message(ctx: dict, connection_id: str, chat_id: str, text: str) -> str:
    db = SessionLocal()
    try:
        return await send_text(
            db, UUID(connection_id), UUID(chat_id), text, int(ctx.get("job_try", 1))
        )
    except RetrySend as exc:
        raise Retry(defer=exc.defer_s) from None
    finally:
        db.close()
