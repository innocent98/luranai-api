"""Opaque, server-minted voice-session tokens.

ElevenLabs carries only the opaque string; the authoritative tenant/session
binding lives here in Redis. This deliberately avoids trusting any metadata
ElevenLabs passes back to us (see spec §9 and Spike 1).

This module also owns per-turn voice-minute metering (see `_meter_turn()`,
used by `resolve()` and `resolve_and_revoke()` below). Metering rides along
with token resolution rather than living solely in `/voice/session/end`,
because a widget that crashes, loses network, or is simply closed never
calls that route at all — `resolve()` is called on every conversational
turn regardless, and is the only reliable proof-of-life this system has.
See Task 11 review round 2 for the full reasoning (a purely client-triggered
`/session/end` metered nothing for any call that didn't end cleanly, so the
minute cap enforced nothing).

The metering tick lives in its OWN Redis key (`_TICK_KEY_PREFIX`), not
embedded in the token's payload. Two things forced that split, both found in
Task 11 review round 3:

1. Two overlapping `resolve()` calls on the same token are a real scenario
   (a rate-limit race the mint endpoint's own code explicitly contemplates,
   plus the escalate-to-human tool resolving independently of an in-flight
   turn). A read-then-write of a "last metered at" field embedded in the
   token's JSON payload lets both calls read the same tick and double-bill
   the delta before either write lands. `_meter_turn()` closes this with a
   single Lua script — see its docstring.
2. Persisting that tick required rewriting the token's payload on every
   `resolve()` (a full `SET`, not the previous bare `EXPIRE`), and a plain
   `SET` unconditionally *recreates* a key that a concurrent
   `/voice/session/end` (or a terminal-rejection `revoke()`) just deleted —
   resurrecting a binding that was supposed to be gone, letting a call keep
   answering (and metering) after its concurrency slot was released, or
   letting a caller loop on exactly the unfixable rejection `revoke()`
   exists to stop. Moving the tick out means `resolve()`'s TTL-slide can go
   back to a bare `EXPIRE`, which is a no-op on a missing key and can never
   recreate one — restoring the property the original (pre-metering) code
   had for free.
"""

import json
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

import redis.asyncio as aioredis
from loguru import logger

from app.core.config import settings
from app.services.voice.metering import VoiceMeter

_KEY_PREFIX = "voice:session_token:"
_TICK_KEY_PREFIX = "voice:session_tick:"

# Atomically bills the wall-clock delta since this token's last metering
# tick and advances the tick, as a single Redis-side operation spanning TWO
# keys (the tick itself and the tenant's monthly-minutes counter). Lua
# scripts execute atomically in Redis (single-threaded), so two concurrent
# EVALs of this script for the same token can never interleave -- one runs
# to completion, then the other, so the second one's `last_tick` read always
# reflects the first one's write. That is what a Python-level
# read-tick / meter / write-tick sequence cannot guarantee: two overlapping
# resolve() calls would both read the same tick and both bill the same
# delta, over-billing the tenant (see module docstring).
#
# TIME is read from Redis itself (not passed in from Python), mirroring
# gating.py's `_TRY_ACQUIRE_SLOT_SCRIPT` -- elapsed-time math is judged by
# one clock, deterministic regardless of app-server clock skew or a GC
# pause between a Python-side read and write.
#
# A missing tick key (a brand-new call whose mint() write hasn't landed yet
# in some pathological race, or a legacy token from before this tick existed
# at all) bills zero for this call rather than the gap since the Unix epoch
# -- consistent with `_context_from`'s own "never bill epoch-to-now" rule.
_METER_TURN_SCRIPT = """
local tick_key = KEYS[1]
local meter_key = KEYS[2]
local tick_ttl = tonumber(ARGV[1])
local meter_ttl = tonumber(ARGV[2])
local now = tonumber(redis.call('TIME')[1])

local last_tick = tonumber(redis.call('GET', tick_key))
if not last_tick then
    last_tick = now
end

local elapsed_minutes = (now - last_tick) / 60.0
if elapsed_minutes < 0 then
    elapsed_minutes = 0
end

if elapsed_minutes > 0 then
    redis.call('INCRBYFLOAT', meter_key, elapsed_minutes)
    redis.call('EXPIRE', meter_key, meter_ttl)
end

redis.call('SET', tick_key, now, 'EX', tick_ttl)
return tostring(elapsed_minutes)
"""


@dataclass(frozen=True)
class VoiceSessionContext:
    tenant_id: UUID
    luran_session_id: UUID
    text_out: bool
    # Unix timestamp captured once, at mint(): when POST /session/token
    # minted the token, NOT when the call actually started talking. WebRTC
    # connect time and mic-permission delay sit between this and the first
    # real turn, so treat this as diagnostic/informational only. Billing
    # does NOT derive duration from this field — see the per-turn metering
    # in `_meter_turn()` below, which bills the wall-clock delta since the
    # token's own last metering tick instead of the gap since mint.
    minted_at: float


class VoiceSessionTokenStore:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None
        self._meter = VoiceMeter(redis_url=self.redis_url)

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def mint(self, *, tenant_id: UUID, luran_session_id: UUID, text_out: bool) -> str:
        now = time.time()
        token = secrets.token_urlsafe(32)
        payload = json.dumps(
            {
                "tenant_id": str(tenant_id),
                "luran_session_id": str(luran_session_id),
                "text_out": text_out,
                "minted_at": now,
            }
        )
        redis = await self._get_redis()
        await redis.set(
            _KEY_PREFIX + token,
            payload,
            ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
        )
        # Seed the metering tick at mint time so the first resolve()'s delta
        # is "since mint" (WebRTC-connect + mic-permission lag included,
        # same as the payload-embedded design this replaced) rather than
        # "since the Unix epoch" if this were left unset.
        await redis.set(
            _TICK_KEY_PREFIX + token,
            now,
            ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
        )
        return token

    def _context_from(self, data: dict) -> VoiceSessionContext:
        return VoiceSessionContext(
            tenant_id=UUID(data["tenant_id"]),
            luran_session_id=UUID(data["luran_session_id"]),
            text_out=bool(data["text_out"]),
            # Tokens minted before this field existed (a one-time window
            # during rollout) fall back to "now" rather than a KeyError --
            # see test_resolve_tolerates_a_payload_minted_before_minted_at_existed.
            # Deliberately NOT 0.0: that would read as "minted at the Unix
            # epoch", which -- before turn-time metering existed -- would
            # have billed ~30 million minutes for the elapsed-since-mint gap
            # on a single call. `_METER_TURN_SCRIPT`'s own "no tick yet"
            # branch applies the same rule independently for the metering
            # tick itself.
            minted_at=float(data["minted_at"]) if data.get("minted_at") else time.time(),
        )

    async def _meter_turn(self, tenant_id: UUID, token: str) -> None:
        """Bill this turn's elapsed delta and advance the metering tick, as
        one atomic Redis-side operation (see `_METER_TURN_SCRIPT` above for
        why this can't be a Python read-then-write).

        Never raises: an EVAL failure (e.g. a Redis outage) is caught,
        logged, and simply means this call's delta is not billed. Because
        the whole script is one atomic unit, a failure here never advances
        the tick either -- unlike a split read/meter/write sequence, there
        is no way for this to bill without recording the tick, or vice
        versa. The next successful call's delta naturally includes
        whatever was lost during the outage rather than dropping it.
        """
        tick_key = _TICK_KEY_PREFIX + token
        meter_key, meter_ttl = self._meter.key_and_ttl(tenant_id)
        redis = await self._get_redis()
        try:
            await redis.eval(
                _METER_TURN_SCRIPT,
                2,
                tick_key,
                meter_key,
                settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
                meter_ttl,
            )
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.error(f"Failed to meter voice minutes for tenant {tenant_id}: {exc}")

    async def resolve(self, token: str) -> VoiceSessionContext | None:
        if not token:
            return None
        redis = await self._get_redis()
        key = _KEY_PREFIX + token
        raw = await redis.get(key)
        if not raw:
            return None
        # Parse BEFORE metering or renewing the TTL. A corrupt payload
        # (fails json.loads or UUID() below) must be allowed to age out on
        # its original countdown — renewing it here would mean every
        # failing turn on a broken token revives its lifetime forever,
        # trapping the caller in an infinite `_FALLBACK_REPLY` loop instead
        # of the binding eventually expiring.
        data = json.loads(raw)
        context = self._context_from(data)

        await self._meter_turn(context.tenant_id, token)

        # Slide the TTL forward on every successful resolve. Without this, a
        # call running longer than the mint TTL goes silent mid-conversation
        # the moment a later turn's resolve() 401s — a caller has no way to
        # know that happened, and ElevenLabs has no way to render a 401 as
        # audio. Only a full TTL window of *inactivity* should end the
        # binding, not wall-clock time since mint.
        #
        # EXPIRE, not a payload-rewriting SET: EXPIRE on a missing key is a
        # documented Redis no-op that can never recreate it. The metering
        # tick used to live inside this payload, which forced a `SET ...`
        # here to persist it -- and a plain SET unconditionally *recreates*
        # a key, so a concurrent /voice/session/end (GETDEL) or a
        # terminal-rejection revoke() landing between the GET above and
        # that SET would have its delete silently undone. See module
        # docstring for the full incident (Task 11 review round 3) and
        # `test_resolve_does_not_resurrect_a_key_deleted_mid_flight`.
        await redis.expire(key, settings.VOICE_SESSION_TOKEN_TTL_SECONDS)
        return context

    async def resolve_and_revoke(self, token: str) -> VoiceSessionContext | None:
        """Atomically fetch-and-delete the token's binding in one Redis round
        trip, billing the final partial delta since its last metering tick
        before the binding is gone.

        For callers that need the payload consumed exactly once (today,
        only `POST /voice/session/end`). `resolve()` followed by `revoke()`
        as two separate calls would reopen a race: two near-simultaneous
        end-of-call requests for the same token (a double-tap hangup, a
        client retry racing the original request) could both GET the
        payload before either DELETEs it, and both would go on to meter the
        same trailing delta. GETDEL is a single round trip, so exactly one
        of two racing callers can ever get the payload back.

        This is an optimisation, not the sole enforcement path: normal
        per-turn metering in `resolve()` already bills the call as it
        happens (see `_meter_turn`'s docstring), so a client that never
        calls `/session/end` at all still gets metered for every turn it
        completed — only the trailing silence after its last turn goes
        unbilled, not the whole call.
        """
        if not token:
            return None
        redis = await self._get_redis()
        raw = await redis.getdel(_KEY_PREFIX + token)
        if not raw:
            return None
        data = json.loads(raw)
        context = self._context_from(data)
        # Bill the final partial delta the same atomic way resolve() does.
        await self._meter_turn(context.tenant_id, token)
        # Best-effort cleanup of the now-orphaned tick key. The main token
        # key is already gone (GETDEL above), so there's nothing left for a
        # future resolve() to resurrect even if this fails -- the tick key
        # simply self-heals via its own TTL.
        try:
            await redis.delete(_TICK_KEY_PREFIX + token)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"Failed to clean up voice metering tick for tenant {context.tenant_id}: {exc}"
            )
        return context

    async def revoke(self, token: str) -> None:
        redis = await self._get_redis()
        await redis.delete(_KEY_PREFIX + token)
