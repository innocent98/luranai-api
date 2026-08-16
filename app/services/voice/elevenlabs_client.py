"""Minimal ElevenLabs REST client — only what voice chat needs."""

import httpx
from loguru import logger

from app.core.config import settings


class ElevenLabsMintError(RuntimeError):
    """Raised when ElevenLabs refuses to mint a conversation token."""


class ElevenLabsClient:
    def __init__(self, api_key: str | None = None, api_base: str | None = None) -> None:
        self.api_key = api_key or settings.ELEVENLABS_API_KEY
        self.api_base = (api_base or settings.ELEVENLABS_API_BASE).rstrip("/")

    async def mint_conversation_token(self, *, agent_id: str, dynamic_variables: dict) -> str:
        """Mint a short-lived WebRTC conversation token for the browser.

        The dynamic variables ride along so the custom-LLM endpoint and tool
        webhooks receive our opaque voice_token on every call.

        NOTE: the exact mint path/param shape here is unverified against the
        live ElevenLabs API (pending Spike 1). If the spike shows a different
        endpoint or payload, change only this method — nothing else in the
        voice-chat backend depends on its internals.
        """
        if not self.api_key:
            raise ElevenLabsMintError("ELEVENLABS_API_KEY is not configured")

        url = f"{self.api_base}/v1/convai/conversation/token"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    url,
                    params={"agent_id": agent_id},
                    headers={"xi-api-key": self.api_key},
                )
        except httpx.HTTPError as exc:
            logger.error(f"ElevenLabs token mint request failed: {exc}")
            raise ElevenLabsMintError("ElevenLabs request failed") from exc

        if response.status_code != 200:
            logger.error(f"ElevenLabs token mint failed: {response.status_code} {response.text}")
            raise ElevenLabsMintError(f"ElevenLabs returned {response.status_code}")

        payload = response.json()
        token = payload.get("token")
        if not token:
            raise ElevenLabsMintError("ElevenLabs response contained no token")
        return token
