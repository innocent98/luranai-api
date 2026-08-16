"""Opaque, server-minted voice-session tokens.

ElevenLabs carries only the opaque string; the authoritative tenant/session
binding lives here in Redis. This deliberately avoids trusting any metadata
ElevenLabs passes back to us (see spec §9 and Spike 1).
"""

import json
import secrets
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
            }
        )
        redis = await self._get_redis()
        await redis.set(
            _KEY_PREFIX + token,
            payload,
            ex=settings.VOICE_SESSION_TOKEN_TTL_SECONDS,
        )
        return token

    async def resolve(self, token: str) -> VoiceSessionContext | None:
        if not token:
            return None
        redis = await self._get_redis()
        raw = await redis.get(_KEY_PREFIX + token)
        if not raw:
            return None
        data = json.loads(raw)
        return VoiceSessionContext(
            tenant_id=UUID(data["tenant_id"]),
            luran_session_id=UUID(data["luran_session_id"]),
            text_out=bool(data["text_out"]),
        )

    async def revoke(self, token: str) -> None:
        redis = await self._get_redis()
        await redis.delete(_KEY_PREFIX + token)
