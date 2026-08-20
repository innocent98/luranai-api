import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.config import settings
from app.services.voice.auth import verify_elevenlabs_request


def _request(headers: dict) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/voice/llm/chat/completions",
        "headers": Headers(headers).raw,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_accepts_matching_shared_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    await verify_elevenlabs_request(_request({"authorization": "Bearer s3cret"}))


@pytest.mark.asyncio
async def test_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "Bearer WRONG"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_fails_closed_when_secret_unset(monkeypatch):
    """An unconfigured secret must reject everything, not accept everything."""
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", None)
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "Bearer anything"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_malformed_authorization_header(monkeypatch):
    """A header present but without the 'Bearer ' scheme must not be treated
    as a credential — regression guard against a naive header-value == secret
    comparison that skips the scheme check entirely."""
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "s3cret"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_empty_secret_string(monkeypatch):
    """An empty-string secret is as unconfigured as None — must fail closed,
    not accept an empty bearer credential."""
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        await verify_elevenlabs_request(_request({"authorization": "Bearer "}))
    assert exc.value.status_code == 401
