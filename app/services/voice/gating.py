"""Voice access gating: tenant flag, monthly minute cap, concurrency ceiling.

Checked in that order at token-mint time (spec §5). Any failure yields a 409
with a machine-readable reason the widget uses to fall back to text.
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.tenant import TenantCapabilities
from app.services.voice.metering import VoiceMeter

_CONCURRENCY_KEY = "voice:concurrent:{tenant_id}"
# Safety net so a dropped call that never releases its slot doesn't hold it
# forever. Comfortably above any real call duration.
_CONCURRENCY_KEY_TTL_SECONDS = 60 * 60 * 2

# SCARD + conditional SADD + EXPIRE as one atomic unit, so two concurrent
# mint requests arriving at `ceiling - 1` can't both observe "under the
# ceiling" and both acquire (the race check() + acquire_slot() had as two
# separate round trips — see Task 3 review finding, closed in Task 4).
# SISMEMBER-first makes a retry with the same token idempotent instead of
# needlessly consuming ceiling headroom twice.
_TRY_ACQUIRE_SLOT_SCRIPT = """
local key = KEYS[1]
local member = ARGV[1]
local ceiling = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

if redis.call('SISMEMBER', key, member) == 1 then
    redis.call('EXPIRE', key, ttl)
    return 1
end

if redis.call('SCARD', key) >= ceiling then
    return 0
end

redis.call('SADD', key, member)
redis.call('EXPIRE', key, ttl)
return 1
"""


class VoiceDenialReason(str, Enum):
    FLAG_DISABLED = "voice_disabled"
    MINUTE_CAP_REACHED = "minute_cap_reached"
    CONCURRENCY_LIMIT = "concurrency_limit"


@dataclass(frozen=True)
class VoiceGateResult:
    allowed: bool
    reason: VoiceDenialReason | None = None


class VoiceGate:
    def __init__(self, db: Session, redis_url: str | None = None) -> None:
        self.db = db
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._meter = VoiceMeter(redis_url=self.redis_url)

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _capabilities(self, tenant_id: UUID) -> TenantCapabilities | None:
        return self.db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        ).scalar_one_or_none()

    async def check(self, tenant_id: UUID) -> VoiceGateResult:
        caps = self._capabilities(tenant_id)

        # 1. Feature flag — fail closed when capabilities are missing entirely.
        if caps is None or not caps.can_use_voice:
            return VoiceGateResult(False, VoiceDenialReason.FLAG_DISABLED)

        # 2. Monthly minute cap (null = unlimited).
        if caps.max_voice_minutes_per_month is not None:
            used = await self._meter.get_monthly_minutes(tenant_id)
            if used >= caps.max_voice_minutes_per_month:
                return VoiceGateResult(False, VoiceDenialReason.MINUTE_CAP_REACHED)

        # 3. Concurrency ceiling.
        redis = await self._get_redis()
        active = await redis.scard(_CONCURRENCY_KEY.format(tenant_id=tenant_id))
        if active >= settings.VOICE_MAX_CONCURRENT_SESSIONS:
            return VoiceGateResult(False, VoiceDenialReason.CONCURRENCY_LIMIT)

        return VoiceGateResult(True)

    async def try_acquire_slot(self, tenant_id: UUID, token: str) -> bool:
        """Atomically reserve a concurrency slot for `tenant_id`, if under the ceiling.

        Callers that need the race-free guarantee (i.e. anything gating a
        real mint) must use this instead of the check()-then-acquire_slot()
        sequence. `acquire_slot`/`release_slot` remain for callers that
        don't need atomicity (e.g. tests seeding state, or releasing a slot
        that was already atomically reserved).
        """
        redis = await self._get_redis()
        key = _CONCURRENCY_KEY.format(tenant_id=tenant_id)
        acquired = await redis.eval(
            _TRY_ACQUIRE_SLOT_SCRIPT,
            1,
            key,
            token,
            settings.VOICE_MAX_CONCURRENT_SESSIONS,
            _CONCURRENCY_KEY_TTL_SECONDS,
        )
        return bool(acquired)

    async def acquire_slot(self, tenant_id: UUID, token: str) -> None:
        redis = await self._get_redis()
        key = _CONCURRENCY_KEY.format(tenant_id=tenant_id)
        await redis.sadd(key, token)
        await redis.expire(key, _CONCURRENCY_KEY_TTL_SECONDS)

    async def release_slot(self, tenant_id: UUID, token: str) -> None:
        redis = await self._get_redis()
        await redis.srem(_CONCURRENCY_KEY.format(tenant_id=tenant_id), token)
