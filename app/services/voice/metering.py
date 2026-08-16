"""Voice-minute metering.

Minimal for now: a Redis counter of monthly minutes per tenant, keyed by
calendar month so rollover needs no scheduled job. `VoiceGate.check` (Task 3)
needs this to evaluate the monthly minute cap at token-mint time.

Task 9 extends this same class with session-lifecycle metering (start/stop
accounting) and a periodic Postgres flush for durable reporting — keep this
minimal until then.
"""

from datetime import UTC, datetime
from uuid import UUID

import redis.asyncio as aioredis

from app.core.config import settings

_KEY_PREFIX = "voice:minutes:"
# Safety net so a tenant's monthly counter key doesn't live in Redis forever.
# Comfortably longer than any calendar month so it survives until Task 9's
# periodic Postgres flush picks it up; expiry of a past month's key is
# otherwise harmless since nothing reads it once the month has rolled over.
_MINUTE_KEY_TTL_SECONDS = 45 * 24 * 60 * 60


class VoiceMeter:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _key(self, tenant_id: UUID) -> str:
        month = datetime.now(UTC).strftime("%Y-%m")
        return f"{_KEY_PREFIX}{tenant_id}:{month}"

    async def add_minutes(self, tenant_id: UUID, minutes: float) -> float:
        redis = await self._get_redis()
        key = self._key(tenant_id)
        total = await redis.incrbyfloat(key, minutes)
        await redis.expire(key, _MINUTE_KEY_TTL_SECONDS)
        return float(total)

    async def get_monthly_minutes(self, tenant_id: UUID) -> float:
        redis = await self._get_redis()
        raw = await redis.get(self._key(tenant_id))
        return float(raw) if raw is not None else 0.0
