from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.voice.elevenlabs_client import ElevenLabsClient, ElevenLabsMintError

_URL = "https://el.example.com/v1/convai/conversation/token"


def _fake_response(**kwargs) -> httpx.Response:
    return httpx.Response(request=httpx.Request("GET", _URL), **kwargs)


@pytest.mark.asyncio
async def test_mint_conversation_token_success():
    client = ElevenLabsClient(api_key="test-key", api_base="https://el.example.com")
    response = _fake_response(status_code=200, json={"token": "abc123"})

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        token = await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})

    assert token == "abc123"


@pytest.mark.asyncio
async def test_mint_conversation_token_missing_api_key_raises_mint_error():
    client = ElevenLabsClient(api_key=None, api_base="https://el.example.com")
    with pytest.raises(ElevenLabsMintError):
        await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})


@pytest.mark.asyncio
async def test_mint_conversation_token_non_200_raises_mint_error():
    client = ElevenLabsClient(api_key="test-key", api_base="https://el.example.com")
    response = _fake_response(status_code=500, text="internal server error")

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        with pytest.raises(ElevenLabsMintError):
            await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})


@pytest.mark.asyncio
async def test_mint_conversation_token_non_json_body_raises_mint_error():
    """A 200 with a non-JSON body (CDN or proxy interstitial, gateway HTML)
    must surface as ElevenLabsMintError, not an unhandled
    json.JSONDecodeError -- the endpoint only knows how to recover from
    ElevenLabsMintError, and everything else risks an unhandled 500."""
    client = ElevenLabsClient(api_key="test-key", api_base="https://el.example.com")
    response = _fake_response(status_code=200, text="<html>not json</html>")

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        with pytest.raises(ElevenLabsMintError):
            await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})


@pytest.mark.asyncio
async def test_mint_conversation_token_non_dict_json_body_raises_mint_error():
    """A 200 with valid JSON that isn't the expected object shape (e.g. a
    bare list) must surface as ElevenLabsMintError, not an unhandled
    AttributeError from calling .get() on a list."""
    client = ElevenLabsClient(api_key="test-key", api_base="https://el.example.com")
    response = _fake_response(status_code=200, json=["unexpected", "list"])

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        with pytest.raises(ElevenLabsMintError):
            await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})


@pytest.mark.asyncio
async def test_mint_conversation_token_missing_token_field_raises_mint_error():
    client = ElevenLabsClient(api_key="test-key", api_base="https://el.example.com")
    response = _fake_response(status_code=200, json={"not_token": "abc123"})

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
        with pytest.raises(ElevenLabsMintError):
            await client.mint_conversation_token(agent_id="agent-1", dynamic_variables={})
