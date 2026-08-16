"""Context-aware ARQ enqueue for notification jobs (Stage 3 Slice 5a)."""

import asyncio
import uuid
from datetime import timedelta

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings


async def _enqueue(jobs: list[tuple[uuid.UUID, str]]) -> None:
    if not jobs:
        return
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        for log_id, job_name in jobs:
            await redis.enqueue_job(job_name, str(log_id), _job_id=f"{job_name}:{log_id}")
    finally:
        await redis.close()


async def enqueue_jobs_async(jobs: list[tuple[uuid.UUID, str]]) -> None:
    await _enqueue(jobs)


def enqueue_jobs_sync(jobs: list[tuple[uuid.UUID, str]]) -> None:
    """Enqueue from a synchronous endpoint (no running event loop in the threadpool)."""
    if not jobs:
        return
    asyncio.run(_enqueue(jobs))


async def enqueue_agent_alerts_async(ticket_id: uuid.UUID, wait_seconds: int) -> None:
    """Enqueue the escalation ring plus its deferred supervisor-fallback timer.

    Both jobs carry a deterministic `_job_id`, so a duplicate escalation for the
    same ticket cannot double-ring: ARQ rejects the second enqueue outright.
    """
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        await redis.enqueue_job(
            "alert_escalation",
            str(ticket_id),
            _job_id=f"alert_escalation:{ticket_id}",
        )
        await redis.enqueue_job(
            "alert_escalation_fallback",
            str(ticket_id),
            _job_id=f"alert_escalation_fallback:{ticket_id}",
            _defer_by=timedelta(seconds=wait_seconds),
        )
    finally:
        await redis.close()


async def enqueue_clear_alert_async(ticket_id: uuid.UUID, winner_user_id: uuid.UUID) -> None:
    """Enqueue the silent clear that stops the ring on every losing device."""
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        await redis.enqueue_job(
            "clear_agent_alert",
            str(ticket_id),
            str(winner_user_id),
            _job_id=f"clear_agent_alert:{ticket_id}",
        )
    finally:
        await redis.close()
