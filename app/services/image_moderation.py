"""Fail-closed image moderation via OpenAI's free omni-moderation endpoint."""

import base64

import openai
from loguru import logger

from app.core.config import settings

_TIMEOUT_SECONDS = 10.0
_REJECT = "This image can't be accepted. Please try a different one."


async def moderate_image(image_bytes: bytes, content_type: str) -> tuple[bool, str | None]:
    """Return (allowed, reason). Any flag or any error yields (False, reason)."""
    try:
        data_url = f"data:{content_type};base64," + base64.b64encode(image_bytes).decode()
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=_TIMEOUT_SECONDS)
        resp = await client.moderations.create(
            model="omni-moderation-latest",
            input=[{"type": "image_url", "image_url": {"url": data_url}}],
        )
        if not resp.results:
            logger.error("Image moderation returned no results (fail-closed)")
            return False, _REJECT
        # Allow ONLY on an explicit clean verdict; True, None or anything
        # unexpected is rejected.
        if resp.results[0].flagged is False:
            return True, None
        return False, _REJECT
    except Exception as e:  # noqa: BLE001 - fail-closed
        logger.error(f"Image moderation failed (fail-closed): {e}")
        return False, _REJECT
