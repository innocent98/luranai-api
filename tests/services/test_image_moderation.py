from unittest.mock import AsyncMock, patch

import pytest

from app.services.image_moderation import moderate_image


def _resp(flagged):
    class R:
        results = [type("x", (), {"flagged": flagged})()]

    return R()


@pytest.mark.asyncio
async def test_clean_image_allowed():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(return_value=_resp(False))
        allowed, reason = await moderate_image(b"\xff\xd8bytes", "image/jpeg")
    assert allowed is True and reason is None


@pytest.mark.asyncio
async def test_flagged_image_rejected():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(return_value=_resp(True))
        allowed, reason = await moderate_image(b"\xff\xd8bytes", "image/jpeg")
    assert allowed is False and reason


@pytest.mark.asyncio
async def test_moderation_error_fails_closed():
    with patch("app.services.image_moderation.openai.AsyncOpenAI") as c:
        c.return_value.moderations.create = AsyncMock(side_effect=RuntimeError("boom"))
        allowed, reason = await moderate_image(b"\xff\xd8bytes", "image/jpeg")
    assert allowed is False and reason  # fail-closed
