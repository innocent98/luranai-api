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

    async def mint_conversation_token(self, *, agent_id: str, participant_name: str) -> str:
        """Mint a short-lived WebRTC conversation token for the browser.

        `participant_name` is how our opaque `voice_token` actually reaches
        the custom-LLM endpoint — confirmed live against ElevenLabs in
        Spike 1 (2026-08-19, 28 captured requests). The chain is non-obvious
        end to end, so it's worth spelling out in full for whoever next
        looks at this parameter and is tempted to call it unused:

        1. This mint call sends `participant_name` as a query parameter.
        2. ElevenLabs binds it into the session JWT as both `name` and `sub`.
        3. The Agent's system prompt (operator-configured) contains the
           literal template text `Session: {{voice_token}}`.
        4. ElevenLabs substitutes `{{voice_token}}` with the JWT's bound
           value when it builds the prompt for the call.
        5. That substituted prompt arrives at our custom-LLM endpoint as
           `messages[0].content` — see `_extract_voice_token` in
           `app/api/v1/endpoints/voice.py`, which parses it back out.

        Verified negative control: minting without `participant_name` left
        `{{voice_token}}` unsubstituted in `messages[0].content` — proving
        the substitution is genuinely driven by this parameter, not
        coincidence.

        The response also carries `conversation_id` (e.g. `conv_...`),
        currently unused here — a natural future correlation key (e.g. for
        joining ElevenLabs-side conversation logs/analytics to our own
        session), not built on yet.
        """
        if not self.api_key:
            raise ElevenLabsMintError("ELEVENLABS_API_KEY is not configured")

        url = f"{self.api_base}/v1/convai/conversation/token"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    url,
                    params={"agent_id": agent_id, "participant_name": participant_name},
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
