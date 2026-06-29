"""Context-aware ARQ enqueue for notification jobs (Stage 3 Slice 5a)."""

import asyncio
import uuid

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings


async def _enqueue(jobs: list[tuple[uuid.UUID, str]]) -> None:
    if not jobs:
        return
    redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        for log_id, job_name in jobs:
            await redis.enqueue_job(job_name, str(log_id))
    finally:
        await redis.close()


async def enqueue_jobs_async(jobs: list[tuple[uuid.UUID, str]]) -> None:
    await _enqueue(jobs)


def enqueue_jobs_sync(jobs: list[tuple[uuid.UUID, str]]) -> None:
    """Enqueue from a synchronous endpoint (no running event loop in the threadpool)."""
    if not jobs:
        return
    asyncio.run(_enqueue(jobs))
