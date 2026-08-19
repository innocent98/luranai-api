"""Verify that an inbound request really came from ElevenLabs.

CONFIRMED (Spike 1, 2026-08-19, 28 captured live requests): ElevenLabs sends
a static `Authorization: Bearer <secret>` on every custom-LLM call — no HMAC
signature, no timestamp. A constant-time shared-secret bearer check is
therefore the real, final mechanism here, not a placeholder pending a spike
result. Every caller (Tasks 8 and 10) still depends only on this function's
contract ("raises 401 unless the request is genuinely from ElevenLabs"),
never on how that's checked — so if ElevenLabs ever adds a verifiable
signature on top of the static secret, the swap is still isolated to this
one function.
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
