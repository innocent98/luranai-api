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

        TODO(spike-1): `dynamic_variables` is accepted to match the published
        interface — it's meant to be the mechanism by which the custom-LLM
        endpoint and tool webhooks receive our opaque voice_token on every
        call — but it is NOT currently transmitted to ElevenLabs. The real
        wire shape for attaching dynamic variables to a WebRTC
        conversation-token mint is unresolved pending Spike 1 (still with
        the user). Until that lands and this method is updated, the
        voice_token does not reach ElevenLabs through this call. Do not
        assume otherwise elsewhere in the codebase.

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

        try:
            payload = response.json()
        except ValueError as exc:
            # response.json() raises json.JSONDecodeError (a ValueError
            # subclass) on a non-JSON 200 body — e.g. a CDN or proxy
            # interstitial. Must not escape as a bare ValueError: the
            # endpoint only knows how to recover from ElevenLabsMintError.
            logger.error(f"ElevenLabs token mint returned a non-JSON body: {exc}")
            raise ElevenLabsMintError("ElevenLabs response was not valid JSON") from exc

        # Guard against a JSON body that parses but isn't the object shape
        # we expect (e.g. a bare list or scalar) — .get() on those would
        # raise AttributeError instead of the ElevenLabsMintError callers
        # know how to handle.
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise ElevenLabsMintError("ElevenLabs response contained no token")
        return token
