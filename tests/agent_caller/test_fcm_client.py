"""Unit tests for the FCM HTTP v1 transport."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models.agent_device import DevicePlatform
from app.services import fcm_client
from app.services.fcm_client import FCMConfigError

_RING_NOTIFICATION = {
    "title": "Customer needs you",
    "body": "A conversation was escalated to a human agent.",
}

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
        result = await fcm_client.send(
            "tok-1", {"type": "escalation_alert"}, platform=DevicePlatform.ANDROID
        )
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
        result = await fcm_client.send(
            "tok-dead", {"type": "escalation_alert"}, platform=DevicePlatform.ANDROID
        )
    assert result.ok is False
    assert result.unregistered is True


@pytest.mark.asyncio
async def test_send_transport_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(fcm_client.settings, "FIREBASE_CREDENTIALS_JSON", _FAKE_CREDS)
    with (patch.object(fcm_client, "_access_token", AsyncMock(side_effect=RuntimeError("boom"))),):
        result = await fcm_client.send(
            "tok-1", {"type": "escalation_alert"}, platform=DevicePlatform.ANDROID
        )
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


def test_android_visible_alert_is_data_only():
    """A `notification` block on Android suppresses `onBackgroundMessage` entirely —
    the app never gets to render its own full-screen-intent alert, and `clear_alert`
    can't cancel a notification the OS (not the app) posted. The visible escalation
    alert must therefore carry no `notification` block on Android, only `data`, with
    high priority so FCM doesn't defer it in Doze."""
    envelope = fcm_client._envelope(
        "tok-android", {"type": "escalation_alert"}, DevicePlatform.ANDROID, _RING_NOTIFICATION
    )
    assert "notification" not in envelope
    assert envelope["data"] == {"type": "escalation_alert"}
    assert envelope["android"]["priority"] == "high"
    assert "notification" not in envelope["android"]


def test_ios_visible_alert_keeps_the_notification_block():
    """iOS has no full-screen-intent equivalent and displays nothing for a
    data-only silent push, so it must keep getting a real `notification` block,
    unlike Android."""
    envelope = fcm_client._envelope(
        "tok-ios", {"type": "escalation_alert"}, DevicePlatform.IOS, _RING_NOTIFICATION
    )
    assert envelope["notification"] == _RING_NOTIFICATION
    assert envelope["apns"]["headers"]["apns-priority"] == "10"


def test_ios_visible_alert_names_the_custom_escalation_sound():
    """On iOS the OS — not the app — chooses the notification sound, and it
    chooses it from `aps.sound`. Unless the payload names the bundled .caf
    exactly, iOS silently falls back to the default sound with nothing
    observable server-side, so this filename is the whole failure mode."""
    envelope = fcm_client._envelope(
        "tok-ios", {"type": "escalation_alert"}, DevicePlatform.IOS, _RING_NOTIFICATION
    )
    assert envelope["apns"]["payload"]["aps"]["sound"] == "luran_escalation.caf"
    assert envelope["apns"]["payload"]["aps"]["sound"] == fcm_client._IOS_ALERT_SOUND


def test_android_visible_alert_has_no_sound_key():
    """Android's sound comes from the app's own notification channel, not the
    push payload — the envelope must not carry a sound key anywhere, and the
    data-only-on-Android contract from `test_android_visible_alert_is_data_only`
    must still hold."""
    envelope = fcm_client._envelope(
        "tok-android", {"type": "escalation_alert"}, DevicePlatform.ANDROID, _RING_NOTIFICATION
    )
    assert "notification" not in envelope
    assert "sound" not in envelope["apns"]["payload"]["aps"]
    assert "sound" not in envelope["android"]


@pytest.mark.parametrize("platform", [DevicePlatform.ANDROID, DevicePlatform.IOS])
def test_silent_messages_are_unchanged_on_both_platforms(platform):
    """`clear_alert` and `missed_escalation` pass notification=None today and must
    keep behaving identically on both platforms — still silent, still
    content-available on iOS, never carrying a `notification` key, and never a
    sound: a sound on the clear would ring every losing agent a second time."""
    envelope = fcm_client._envelope("tok", {"type": "clear_alert"}, platform, None)
    assert "notification" not in envelope
    assert envelope["apns"]["payload"]["aps"]["content-available"] == 1
    assert "sound" not in envelope["apns"]["payload"]["aps"]
    assert envelope["apns"]["headers"] == {
        "apns-priority": "5",
        "apns-push-type": "background",
    }


def test_data_payload_is_identical_across_platforms():
    """The app parses the same `data` keys regardless of platform — only the
    presence of the `notification` block differs."""
    data = {"type": "escalation_alert", "ticket_id": "abc-123"}
    android = fcm_client._envelope("tok-a", data, DevicePlatform.ANDROID, _RING_NOTIFICATION)
    ios = fcm_client._envelope("tok-i", data, DevicePlatform.IOS, _RING_NOTIFICATION)
    assert android["data"] == ios["data"] == data
