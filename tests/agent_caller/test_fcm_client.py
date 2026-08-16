"""Unit tests for the FCM HTTP v1 transport."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import fcm_client
from app.services.fcm_client import FCMConfigError

_FAKE_CREDS = json.dumps(
    {
        "client_email": "svc@luran.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----FAKE-----END PRIVATE KEY-----",
        "project_id": "luran-test",
    }
)


class _Resp:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)


@pytest.fixture(autouse=True)
def _clear_token_cache():
    fcm_client._token_cache.clear()
    yield
    fcm_client._token_cache.clear()


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", "")
    with pytest.raises(FCMConfigError):
        fcm_client._credentials()


def test_malformed_credentials_raises(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", "{not json")
    with pytest.raises(FCMConfigError):
        fcm_client._credentials()


@pytest.mark.asyncio
async def test_send_success(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(return_value="ya29.fake")),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=_Resp(200))),
    ):
        result = await fcm_client.send("tok-1", {"type": "escalation_alert"})
    assert result.ok is True
    assert result.unregistered is False


@pytest.mark.asyncio
async def test_send_unregistered_token_is_flagged(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    body = '{"error":{"status":"NOT_FOUND","details":[{"errorCode":"UNREGISTERED"}]}}'
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(return_value="ya29.fake")),
        patch("httpx.AsyncClient.post", AsyncMock(return_value=_Resp(404, body))),
    ):
        result = await fcm_client.send("tok-dead", {"type": "escalation_alert"})
    assert result.ok is False
    assert result.unregistered is True


@pytest.mark.asyncio
async def test_send_transport_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    with (
        patch.object(fcm_client, "_access_token", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        result = await fcm_client.send("tok-1", {"type": "escalation_alert"})
    assert result.ok is False
    assert result.unregistered is False
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_access_token_is_cached(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    post = AsyncMock(return_value=_Resp(200, '{"access_token":"ya29.a","expires_in":3600}'))
    with (
        patch("app.services.fcm_client.jwt.encode", return_value="signed-assertion"),
        patch("httpx.AsyncClient.post", post),
    ):
        first = await fcm_client._access_token()
        second = await fcm_client._access_token()
    assert first == second == "ya29.a"
    assert post.await_count == 1


def test_data_payload_values_are_all_strings():
    payload = fcm_client.stringify_data({"wait_minutes": 4, "reason": None, "type": "x"})
    assert payload == {"wait_minutes": "4", "type": "x"}
