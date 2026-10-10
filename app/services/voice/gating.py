"""Voice access gating: tenant flag, tenant preference, monthly minute cap, concurrency ceiling.

Checked in that order at token-mint time (spec §5). Any failure yields a 409
with a machine-readable reason the widget uses to fall back to text.
"""

import time
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.tenant import TenantAIConfig, TenantCapabilities
from app.services.first_responder import FIRST_RESPONDER_HUMAN
from app.services.voice.metering import VoiceMeter

_CONCURRENCY_KEY = "voice:concurrent:{tenant_id}"
# Members are stored in a sorted set scored by acquire time (unix seconds),
# not a plain set, specifically so each slot ages out individually — see
# _CONCURRENCY_KEY_TTL_SECONDS below. `EXPIRE key ttl` on top is only a
# backstop for a key nothing has touched in `ttl` seconds; it is not what
# gives an individual leaked slot its expiry.
_CONCURRENCY_KEY_TTL_SECONDS = 60 * 60 * 2

# ZREMRANGEBYSCORE (evict stale members) + ZSCORE/ZCARD (idempotency + ceiling
# check) + ZADD + EXPIRE as one atomic unit, so two concurrent mint requests
# arriving at `ceiling - 1` can't both observe "under the ceiling" and both
# acquire (the race check() + acquire_slot() had as two separate round trips
# — see Task 3 review finding, closed in Task 4).
#
# A plain Redis SET (bare SADD) was tried first and rejected: `EXPIRE`
# resets the TTL on the *whole key*, so any tenant minting more than once
# per `ttl` seconds would keep the key alive forever, and a slot orphaned by
# a crashed/never-released session would never age out — accumulating until
# `SCARD == ceiling` locks that tenant out of voice permanently, the exact
# opposite of what the TTL is there to prevent. A sorted set scored by
# acquire time gives each member its own effective expiry via
# ZREMRANGEBYSCORE, independent of whether the key itself has been touched
# recently.
#
# TIME is read from Redis itself (not passed in from Python) so eviction is
# judged by one clock — the server's — and stays deterministic within the
# script regardless of app-server clock skew.
_TRY_ACQUIRE_SLOT_SCRIPT = """
local key = KEYS[1]
local member = ARGV[1]
local ceiling = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local now = tonumber(redis.call('TIME')[1])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)

if redis.call('ZSCORE', key, member) then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, ttl)
    return 1
end

if redis.call('ZCARD', key) >= ceiling then
    return 0
end

redis.call('ZADD', key, now, member)
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

    def _voice_enabled_by_tenant(self, tenant_id: UUID) -> bool:
        """The tenant's own on/off, independent of the platform grant.

        A missing row means 'never configured', which is NOT 'disabled' — the
        column defaults to enabled so that granting voice makes it work
        immediately. Returning False here would silently kill voice for every
        tenant without a TenantAIConfig row.

        A human-only tenant (first_responder == "human", N3) is never offered
        voice: the voice agent is an AI, and those tenants opted out of AI.
        """
        config = self.db.execute(
            select(TenantAIConfig).where(TenantAIConfig.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if config is None:
            return True
        return config.voice_enabled and config.first_responder != FIRST_RESPONDER_HUMAN

    async def check(self, tenant_id: UUID) -> VoiceGateResult:
        caps = self._capabilities(tenant_id)

        # 1. Feature flag — fail closed when capabilities are missing entirely.
        if caps is None or not caps.can_use_voice:
            return VoiceGateResult(False, VoiceDenialReason.FLAG_DISABLED)

        # 2. Tenant's own preference. AFTER the grant so an ungranted tenant is
        # never told anything about their preference, and BEFORE the cap so a
        # disabled tenant costs no Redis read. Reuses FLAG_DISABLED because the
        # reason crosses the API boundary and the shipped widget already handles
        # that value with the right copy.
        if not self._voice_enabled_by_tenant(tenant_id):
            return VoiceGateResult(False, VoiceDenialReason.FLAG_DISABLED)

        # 3. Monthly minute cap (null = unlimited).
        if caps.max_voice_minutes_per_month is not None:
            used = await self._meter.get_monthly_minutes(tenant_id)
            if used >= caps.max_voice_minutes_per_month:
                return VoiceGateResult(False, VoiceDenialReason.MINUTE_CAP_REACHED)

        # 4. Concurrency ceiling — non-atomic peek; try_acquire_slot() below
        # is the authoritative, race-free check used at actual mint time.
        # Counts only members scored within the TTL window rather than the
        # raw ZCARD, so a peek taken between two try_acquire_slot() calls
        # (which do the real eviction) doesn't over-count stale entries as
        # "active" and report a false concurrency_limit.
        redis = await self._get_redis()
        key = _CONCURRENCY_KEY.format(tenant_id=tenant_id)
        cutoff = time.time() - _CONCURRENCY_KEY_TTL_SECONDS
        active = await redis.zcount(key, cutoff, "+inf")
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
        """Non-atomic slot acquire — callers seeding state directly (tests) or
        that already hold the race-free guarantee from elsewhere. Scores the
        member by current time so it ages out the same way try_acquire_slot()
        entries do.
        """
        redis = await self._get_redis()
        key = _CONCURRENCY_KEY.format(tenant_id=tenant_id)
        await redis.zadd(key, {token: time.time()})
        await redis.expire(key, _CONCURRENCY_KEY_TTL_SECONDS)

    async def release_slot(self, tenant_id: UUID, token: str) -> None:
        redis = await self._get_redis()
        await redis.zrem(_CONCURRENCY_KEY.format(tenant_id=tenant_id), token)
