"""arq enqueue helpers for the WhatsApp channel (fail-fast, deterministic job ids).

Tests monkeypatch these two functions to capture or run jobs inline.
"""

from __future__ import annotations

from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings


async def _pool():  # type: ignore[no-untyped-def]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    redis_settings.conn_retries = 0  # fail fast: a webhook must answer 200/5xx quickly
    return await create_pool(redis_settings)


async def enqueue_event(event: dict) -> None:
    """Queue a validated sidecar webhook event. The delivery id is the job id (replay-safe)."""
    redis = await _pool()
    try:
        await redis.enqueue_job("process_whatsapp_event", event, _job_id=f"wa_evt:{event['id']}")
    finally:
        await redis.close()


async def enqueue_send(connection_id: UUID, chat_id: UUID, text: str, key: str) -> None:
    """Queue one outbound text. `key` is the idempotency key (becomes the arq job id)."""
    redis = await _pool()
    try:
        await redis.enqueue_job(
            "send_whatsapp_message",
            str(connection_id),
            str(chat_id),
            text,
            _job_id=f"wa_send:{key}",
        )
    finally:
        await redis.close()
