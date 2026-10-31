"""arq enqueue helpers for the WhatsApp channel (fail-fast, deterministic job ids).

Tests monkeypatch these two functions to capture or run jobs inline.
"""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.encryption import encrypt_field, try_decrypt_field


def seal_steps(steps: list[dict]) -> str:
    """Follow-up steps hold the quote text (chat content): sealed like every queued message."""
    return encrypt_field(json.dumps(steps))


def unseal_steps(sealed: str | None) -> list[dict]:
    if not sealed:
        return []
    try:
        steps = json.loads(try_decrypt_field(sealed))
    except (ValueError, TypeError):
        return []
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


async def _pool():  # type: ignore[no-untyped-def]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    redis_settings.conn_retries = 0  # fail fast: a webhook must answer 200/5xx quickly
    return await create_pool(redis_settings)


async def enqueue_event(event: dict) -> None:
    """Queue a validated sidecar webhook event. The delivery id is the job id (replay-safe).

    The event carries customer message text, and arq keeps job arguments in Redis (24 h of
    results), so the whole event is Fernet-sealed: Redis never holds plaintext chat content.
    """
    redis = await _pool()
    try:
        await redis.enqueue_job(
            "process_whatsapp_event",
            encrypt_field(json.dumps(event)),
            _job_id=f"wa_evt:{event['id']}",
        )
    finally:
        await redis.close()


async def enqueue_send(
    connection_id: UUID,
    chat_id: UUID,
    text: str,
    key: str,
    guard: bool = False,
    rating_request_id: UUID | None = None,
    defer_s: float = 0,
    followups: list[dict] | None = None,
) -> None:
    """Queue one outbound text. `key` is the idempotency key (becomes the arq job id).

    `rating_request_id` marks a rating prompt: once it is actually sent the chat remembers it.
    `defer_s` delays the job. `followups` are the catalog quote text and photos that may be queued
    only once THIS text was sent (sealed: they hold chat content), so they cannot overtake it.
    """
    redis = await _pool()
    try:
        await redis.enqueue_job(
            "send_whatsapp_message",
            str(connection_id),
            str(chat_id),
            encrypt_field(text),  # reply text is chat content too: sealed at rest in Redis
            guard,  # True for automatic replies: re-check pause / ai_enabled at send time
            str(rating_request_id) if rating_request_id else None,
            seal_steps(followups) if followups else None,
            _job_id=f"wa_send:{key}",
            _defer_by=timedelta(seconds=defer_s) if defer_s else None,
        )
    finally:
        await redis.close()


async def enqueue_send_image(
    connection_id: UUID, chat_id: UUID, attachment_id: UUID, caption: str | None, key: str
) -> None:
    """Queue one outbound image. Only the attachment id travels: the sender job reads the stored
    URL from our own row (never a URL from a frame). `key` becomes the arq job id."""
    redis = await _pool()
    try:
        await redis.enqueue_job(
            "send_whatsapp_image",
            str(connection_id),
            str(chat_id),
            str(attachment_id),
            encrypt_field(caption) if caption else None,  # an agent's words: sealed at rest
            _job_id=f"wa_send:{key}",
        )
    finally:
        await redis.close()


async def enqueue_send_offering_image(
    connection_id: UUID, chat_id: UUID, offering_image_id: UUID, key: str, defer_s: float = 0
) -> None:
    """Queue one catalog photo. Only the row id travels: the sender job re-reads the item, its
    price and its URL from our own tables at send time (never a URL, caption or price from the
    model or a frame). A separate job from `send_whatsapp_image`: catalog photos are not chat
    attachments, so the retention job can never see (or destroy) them."""
    redis = await _pool()
    try:
        await redis.enqueue_job(
            "send_whatsapp_offering_image",
            str(connection_id),
            str(chat_id),
            str(offering_image_id),
            _job_id=f"wa_send:{key}",
            _defer_by=timedelta(seconds=defer_s) if defer_s else None,
        )
    finally:
        await redis.close()
