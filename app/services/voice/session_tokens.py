"""Opaque, server-minted voice-session tokens.

ElevenLabs carries only the opaque string; the authoritative tenant/session
binding lives here in Redis. This deliberately avoids trusting any metadata
ElevenLabs passes back to us (see spec §9 and Spike 1).

This module also owns per-turn voice-minute metering (see `resolve()` and
`resolve_and_revoke()` below). Metering rides along with token resolution
rather than living solely in `/voice/session/end`, because a widget that
crashes, loses network, or is simply closed never calls that route at all —
`resolve()` is called on every conversational turn regardless, and is the
only reliable proof-of-life this system has. See Task 11 review round 2 for
the full reasoning (a purely client-triggered `/session/end` metered nothing
for any call that didn't end cleanly, so the minute cap enforced nothing).
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
    # in `resolve()`/`resolve_and_revoke()` below, which bills the wall-
    # clock delta since the token's own last metering tick instead of the
    # gap since mint.
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
                # The token's first metering tick. resolve()/resolve_and_revoke()
                # bill the wall-clock delta since this timestamp on every turn,
                # so it starts equal to `minted_at` -- the first turn's delta
                # therefore still includes WebRTC-connect + mic-permission lag,
                # same as before, but that lag no longer compounds across the
                # rest of the call the way billing off `minted_at` alone did.
                "last_metered_at": now,
            }
        )
        redis = await self._get_redis()
        await redis.set(
            _KEY_PREFIX + token,
            payload,
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
            # epoch" and, before turn-time metering existed, would have
            # billed ~30 million minutes for the elapsed-since-mint gap on
            # a single call. Turn-time metering below makes that specific
            # failure mode impossible either way (see the same fallback
            # inside `_last_metering_tick`), but the field itself stays
            # safe on its own.
            minted_at=float(data["minted_at"]) if data.get("minted_at") else time.time(),
        )

    def _last_metering_tick(self, data: dict, minted_at: float) -> float:
        """The timestamp metering last billed up to for this token: its own
        `last_metered_at` if present, else `minted_at` (pre-metering legacy
        payload), else `minted_at` again as the ultimate fallback -- never
        0.0, for the same "don't bill the Unix epoch" reason `_context_from`
        avoids it."""
        return float(data.get("last_metered_at") or minted_at or time.time())

    async def _meter_elapsed_since_last_tick(
        self, tenant_id: UUID, last_tick: float
    ) -> float | None:
        """Bill the wall-clock time since `last_tick` and, only if the write
        succeeds, return the new tick timestamp for the caller to persist.

        Runs on every `resolve()` (i.e. every conversational turn — proof
        the caller is still on the call) and on `resolve_and_revoke()` (the
        final partial delta at hang-up). This is what gives the minute cap
        teeth mid-call and closes the crash/disconnect gap: a widget that
        never calls `/session/end` still gets billed for every turn it
        completed, because metering no longer depends on that route firing
        at all — it becomes a pure optimisation (billing the last, trailing
        partial minute a hair earlier) rather than the sole enforcement
        path.

        Returns `None` on failure so the caller does NOT advance the stored
        tick — a lost delta gets folded into the next successful tick
        instead of silently vanishing (at the cost of one lump catch-up
        write instead of smooth per-turn ones, if Redis has a sustained
        outage). Never raises: a metering hiccup must not break the live
        conversational turn this rides along with — see `resolve()`, whose
        entire contract (return a context or None) must survive this
        failing.
        """
        now = time.time()
        elapsed_minutes = max(0.0, now - last_tick) / 60.0
        if elapsed_minutes == 0.0:
            return now
        try:
            await self._meter.add_minutes(tenant_id, elapsed_minutes)
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.error(f"Failed to meter voice minutes for tenant {tenant_id}: {exc}")
            return None
        return now

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

        last_tick = self._last_metering_tick(data, context.minted_at)
        new_tick = await self._meter_elapsed_since_last_tick(context.tenant_id, last_tick)
        if new_tick is not None:
            data["last_metered_at"] = new_tick

        # Slide the TTL forward on every successful resolve. Without this, a
        # call running longer than the mint TTL goes silent mid-conversation
        # the moment a later turn's resolve() 401s — a caller has no way to
        # know that happened, and ElevenLabs has no way to render a 401 as
        # audio. Only a full TTL window of *inactivity* should end the
        # binding, not wall-clock time since mint. Rewriting the whole value
        # (rather than a bare EXPIRE) is what lets the metering tick above
        # persist in the same round trip.
        await redis.set(key, json.dumps(data), ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS)
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

        This is now an optimisation, not the sole enforcement path: normal
        per-turn metering in `resolve()` already bills the call as it
        happens (see that method's docstring), so a client that never calls
        `/session/end` at all still gets metered for every turn it
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
        last_tick = self._last_metering_tick(data, context.minted_at)
        # Best-effort: the token is already gone from Redis either way by
        # this point, so unlike resolve() there is no tick to persist
        # afterward regardless of whether this succeeds.
        await self._meter_elapsed_since_last_tick(context.tenant_id, last_tick)
        return context

    async def revoke(self, token: str) -> None:
        redis = await self._get_redis()
        await redis.delete(_KEY_PREFIX + token)
