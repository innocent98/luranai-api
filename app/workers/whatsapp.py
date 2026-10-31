"""arq jobs for the WhatsApp channel (registered in WorkerSettings.functions)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from uuid import UUID

from arq import Retry
from loguru import logger

from app.channels.base import SidecarUnavailable
from app.core.encryption import try_decrypt_field
from app.db.session import SessionLocal
from app.core.config import settings
from app.services import whatsapp_capacity, whatsapp_inbound
from app.services.whatsapp_outbound import (
    RetrySend,
    send_image,
    send_offering_image,
    send_text,
)

_CHAT_LOCK_TTL_S = 120
# A busy chat (the previous message's AI turn is still running) is waited for in-job for up to
# ~20 s, by at most _MAX_WAITERS_PER_CHAT jobs per chat (a spammer must not pin the worker
# slots); everything else falls back to an arq retry (the event job has a 30-try budget).
_LOCK_WAIT_TRIES = 10
_MAX_WAITERS_PER_CHAT = 2
_LOCK_POLL_S = 2.0


async def _lock_chat(ctx: dict, event: dict):  # type: ignore[no-untyped-def]
    """Serialise events of one chat so two quick messages cannot race on session creation."""
    redis = ctx.get("redis")
    if redis is None:
        return None
    ref = event.get("from_ref") or event.get("to_ref")
    if not ref:
        return None
    # Hash the chat ref: it can be a phone-number JID and must not sit in a Redis key in clear.
    digest = hashlib.sha256(str(ref).encode()).hexdigest()[:32]
    key = f"wa:chatlock:{event['session_key']}:{digest}"
    token = secrets.token_hex(8)
    if await redis.set(key, token, ex=_CHAT_LOCK_TTL_S, nx=True):
        return key, token
    waiters_key = f"{key}:waiters"
    if await redis.incr(waiters_key) > _MAX_WAITERS_PER_CHAT:
        await redis.decr(waiters_key)
        raise Retry(defer=10)
    await redis.expire(waiters_key, _CHAT_LOCK_TTL_S)
    try:
        for _ in range(_LOCK_WAIT_TRIES):
            await asyncio.sleep(_LOCK_POLL_S)
            if await redis.set(key, token, ex=_CHAT_LOCK_TTL_S, nx=True):
                return key, token
    finally:
        await redis.decr(waiters_key)
    raise Retry(defer=10)


async def process_whatsapp_event(ctx: dict, payload: dict | str) -> str:
    """`payload` is the Fernet-sealed JSON event (a plain dict is accepted for tests)."""
    event = payload if isinstance(payload, dict) else json.loads(try_decrypt_field(payload))
    key = await _lock_chat(ctx, event)
    db = SessionLocal()
    try:
        outcome = await whatsapp_inbound.handle_event(db, event)
        logger.info("whatsapp event {} -> {}", event.get("type"), outcome)
        return outcome
    except SidecarUnavailable:
        # e.g. the media endpoint is down: the image stays in the sidecar for 10 minutes, retry.
        raise Retry(defer=15) from None
    finally:
        db.close()
        if key:
            # Compare-and-delete: after a TTL expiry the lock may belong to another job now.
            redis = ctx["redis"]
            lock_key, token = key
            current = await redis.get(lock_key)
            if isinstance(current, bytes):
                current = current.decode()
            if current == token:
                await redis.delete(lock_key)


async def send_whatsapp_message(
    ctx: dict,
    connection_id: str,
    chat_id: str,
    text: str,
    guard: bool = False,
    rating_request_id: str | None = None,
) -> str:
    db = SessionLocal()
    try:
        return await send_text(
            db,
            UUID(connection_id),
            UUID(chat_id),
            try_decrypt_field(text),  # sealed by whatsapp_enqueue.enqueue_send
            int(ctx.get("job_try", 1)),
            guard,
            UUID(rating_request_id) if rating_request_id else None,
        )
    except RetrySend as exc:
        raise Retry(defer=exc.defer_s) from None
    finally:
        db.close()


async def send_whatsapp_image(
    ctx: dict, connection_id: str, chat_id: str, attachment_id: str, caption: str | None = None
) -> str:
    db = SessionLocal()
    try:
        return await send_image(
            db,
            UUID(connection_id),
            UUID(chat_id),
            UUID(attachment_id),
            try_decrypt_field(caption) if caption else None,
            int(ctx.get("job_try", 1)),
        )
    except RetrySend as exc:
        raise Retry(defer=exc.defer_s) from None
    finally:
        db.close()


async def send_whatsapp_offering_image(
    ctx: dict, connection_id: str, chat_id: str, offering_image_id: str
) -> str:
    db = SessionLocal()
    try:
        return await send_offering_image(
            db,
            UUID(connection_id),
            UUID(chat_id),
            UUID(offering_image_id),
            int(ctx.get("job_try", 1)),
        )
    except RetrySend as exc:
        raise Retry(defer=exc.defer_s) from None
    finally:
        db.close()


async def whatsapp_capacity_check(ctx: dict) -> dict:
    """Cron (every 5 minutes): email the operator when live connections or the sidecar's memory
    near their limits. Does nothing while WhatsApp is switched off. Never raises."""
    if not settings.WHATSAPP_ENABLED or not (
        settings.WHATSAPP_SIDECAR_URL and settings.WHATSAPP_SIDECAR_TOKEN
    ):
        return {"skipped": True, "alerts": []}
    try:
        db = SessionLocal()
        try:
            alerts = await whatsapp_capacity.check_and_alert(db, include_health=True)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("whatsapp capacity cron failed: {}", type(exc).__name__)
        return {"skipped": False, "alerts": [], "error": type(exc).__name__}
    return {"skipped": False, "alerts": alerts}
