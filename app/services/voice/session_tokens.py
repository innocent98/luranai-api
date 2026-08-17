"""Opaque, server-minted voice-session tokens.

ElevenLabs carries only the opaque string; the authoritative tenant/session
binding lives here in Redis. This deliberately avoids trusting any metadata
ElevenLabs passes back to us (see spec §9 and Spike 1).
"""

import json
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

import redis.asyncio as aioredis

from app.core.config import settings

_KEY_PREFIX = "voice:session_token:"


@dataclass(frozen=True)
class VoiceSessionContext:
    tenant_id: UUID
    luran_session_id: UUID
    text_out: bool
    # Unix timestamp captured once, at mint(). Never moved forward by
    # resolve()'s TTL slide -- it is the call's start-of-session marker,
    # which is what `/voice/session/end` derives elapsed duration from. See
    # module docstring: minute metering must not trust a client-supplied
    # duration for a paid resource, so the server has to own this number.
    minted_at: float


class VoiceSessionTokenStore:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def mint(self, *, tenant_id: UUID, luran_session_id: UUID, text_out: bool) -> str:
        token = secrets.token_urlsafe(32)
        payload = json.dumps(
            {
                "tenant_id": str(tenant_id),
                "luran_session_id": str(luran_session_id),
                "text_out": text_out,
                "minted_at": time.time(),
            }
        )
        redis = await self._get_redis()
        await redis.set(
            _KEY_PREFIX + token,
            payload,
            ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
        )
        return token

    def _parse(self, raw: str) -> VoiceSessionContext:
        data = json.loads(raw)
        return VoiceSessionContext(
            tenant_id=UUID(data["tenant_id"]),
            luran_session_id=UUID(data["luran_session_id"]),
            text_out=bool(data["text_out"]),
            # Tokens minted before this field existed (a one-time window
            # during rollout) fall back to "now" rather than a KeyError --
            # elapsed-duration metering on those specific in-flight calls
            # undercounts by at most one mint-TTL window, which is strictly
            # safer than crashing resolve() or fabricating a duration.
            minted_at=float(data["minted_at"]) if data.get("minted_at") else time.time(),
        )

    async def resolve(self, token: str) -> VoiceSessionContext | None:
        if not token:
            return None
        redis = await self._get_redis()
        key = _KEY_PREFIX + token
        raw = await redis.get(key)
        if not raw:
            return None
        # Parse BEFORE renewing the TTL. A corrupt payload (fails json.loads
        # or UUID() below) must be allowed to age out on its original
        # countdown — renewing it here would mean every failing turn on a
        # broken token revives its lifetime forever, trapping the caller in
        # an infinite `_FALLBACK_REPLY` loop instead of the binding
        # eventually expiring.
        context = self._parse(raw)
        # Slide the TTL forward on every successful resolve. Without this, a
        # call running longer than the mint TTL goes silent mid-conversation
        # the moment a later turn's resolve() 401s — a caller has no way to
        # know that happened, and ElevenLabs has no way to render a 401 as
        # audio. Only a full TTL window of *inactivity* should end the
        # binding, not wall-clock time since mint. Note this does NOT touch
        # `minted_at` -- see the field's docstring on VoiceSessionContext.
        await redis.expire(key, settings.VOICE_SESSION_TOKEN_TTL_SECONDS)
        return context

    async def resolve_and_revoke(self, token: str) -> VoiceSessionContext | None:
        """Atomically fetch-and-delete the token's binding in one Redis round trip.

        For callers that meter a paid resource off the result (today, only
        `POST /voice/session/end`) and therefore must guarantee the payload
        can be consumed exactly once. `resolve()` followed by `revoke()` as
        two separate calls would reopen a race: two near-simultaneous
        end-of-call requests for the same token (a double-tap hangup, a
        client retry racing the original request) could both GET the
        payload before either DELETEs it, and both would go on to meter the
        same call. GETDEL is a single round trip, so exactly one of two
        racing callers can ever get the payload back.
        """
        if not token:
            return None
        redis = await self._get_redis()
        raw = await redis.getdel(_KEY_PREFIX + token)
        if not raw:
            return None
        return self._parse(raw)

    async def revoke(self, token: str) -> None:
        redis = await self._get_redis()
        await redis.delete(_KEY_PREFIX + token)
