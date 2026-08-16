"""Verify that an inbound request really came from ElevenLabs.

TODO(spike-1): SWAP POINT. Today this is a constant-time shared-secret bearer
check, which is what the ElevenLabs custom-LLM config lets us set today. If
Spike 1 shows EL sends a verifiable HMAC signature instead (e.g. an
`xi-signature: t=…,v0=…` header), add that branch HERE, inside
verify_elevenlabs_request — every caller (Tasks 8 and 10) depends only on
this function's contract ("raises 401 unless the request is genuinely from
ElevenLabs"), never on how that's checked, so the swap touches nothing else
in the voice-chat backend.
"""

import hmac

from fastapi import HTTPException, Request, status
from loguru import logger

from app.core.config import settings

_BEARER_PREFIX = "bearer "


async def verify_elevenlabs_request(request: Request) -> None:
    """Raise 401 unless the request carries our shared secret.

    Fails closed: an unset (or empty-string) secret rejects everything,
    rather than degrading into an open endpoint.
    """
    expected = settings.VOICE_SHARED_SECRET
    if not expected:
        logger.error("VOICE_SHARED_SECRET is not configured — rejecting voice request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="voice auth not configured",
        )

    header = request.headers.get("authorization", "")
    presented = header[len(_BEARER_PREFIX) :] if header.lower().startswith(_BEARER_PREFIX) else ""

    if not hmac.compare_digest(presented, expected):
        logger.warning("Rejected voice request with invalid ElevenLabs credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
