import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.services.voice.elevenlabs_client import ElevenLabsMintError
from app.services.voice.session_tokens import VoiceSessionTokenStore


def _payload(tenant_id, session_id):
    return {
        "tenant_id": str(tenant_id),
        "luran_session_id": str(session_id),
        "text_out": False,
    }


@pytest.fixture(autouse=True)
def _voice_provider_configured(monkeypatch):
    """All tests here mock the ElevenLabs client — never call the real API —
    but the endpoint still fails closed with 503 `voice_not_configured` if
    ELEVENLABS_AGENT_ID isn't set, which it isn't by default in the test env
    (see test_voice_capabilities.py::test_voice_settings_exist_with_safe_defaults).
    Configure it here so tests exercise the gating/mint logic instead."""
    monkeypatch.setattr(settings, "ELEVENLABS_AGENT_ID", "test-agent-id")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "test-api-key")


def test_mint_succeeds_and_returns_tokens(pg_client, voice_tenant_factory, voice_session_factory):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-abc"),
    ):
        resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_token"] == "el-convo-token-abc"
    assert body["agent_id"] == "test-agent-id"
    assert len(body["voice_token"]) >= 32
    assert body["expires_in_seconds"] == 600


def test_mint_409_when_flag_disabled(pg_client, voice_tenant_factory, voice_session_factory):
    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "voice_disabled"


def test_mint_409_when_cap_reached(
    pg_client, voice_tenant_factory, voice_session_factory, voice_usage_setter
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=5)
    session = voice_session_factory(tenant.id)
    voice_usage_setter(tenant.id, minutes=5)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "minute_cap_reached"


def test_mint_rejects_session_from_another_tenant(
    pg_client, voice_tenant_factory, voice_session_factory
):
    tenant_a = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    tenant_b = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_b = voice_session_factory(tenant_b.id)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant_a.id, session_b.id))

    assert resp.status_code == 404


def test_mint_409_when_concurrency_limit_reached(
    pg_client, voice_tenant_factory, voice_session_factory, monkeypatch
):
    """The second concurrent request for a tenant already at its ceiling must
    be denied with `concurrency_limit`, proving the endpoint enforces the
    atomic reserve rather than the earlier check-then-acquire race."""
    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", 1)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_a = voice_session_factory(tenant.id)
    session_b = voice_session_factory(tenant.id)

    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-1"),
    ):
        resp_a = pg_client.post(
            "/api/v1/voice/session/token", json=_payload(tenant.id, session_a.id)
        )
        assert resp_a.status_code == 200

        resp_b = pg_client.post(
            "/api/v1/voice/session/token", json=_payload(tenant.id, session_b.id)
        )

    assert resp_b.status_code == 409
    assert resp_b.json()["detail"]["reason"] == "concurrency_limit"


def test_mint_revokes_token_and_releases_slot_when_el_fails(
    pg_client, voice_tenant_factory, voice_session_factory
):
    """A failed ElevenLabs mint must not leave an orphaned voice-token binding
    or a leaked concurrency slot in Redis.

    `VoiceSessionTokenStore.mint` is spied on (real implementation, real
    Redis) rather than replaced with a canned return value — replacing it
    entirely would mean nothing was ever actually written to Redis, making
    the "was it revoked" assertion below pass vacuously regardless of
    whether the endpoint's cleanup code runs at all.
    """
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    original_mint = VoiceSessionTokenStore.mint
    captured: dict[str, str] = {}

    async def _spy_mint(self, **kwargs):
        token = await original_mint(self, **kwargs)
        captured["token"] = token
        return token

    with (
        patch("app.api.v1.endpoints.voice.VoiceSessionTokenStore.mint", new=_spy_mint),
        patch(
            "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
            new=AsyncMock(side_effect=ElevenLabsMintError("boom")),
        ),
    ):
        resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "voice_provider_unavailable"
    assert "token" in captured, "mint() must have actually run for this test to prove anything"

    async def _post_failure_state():
        store = VoiceSessionTokenStore()
        ctx = await store.resolve(captured["token"])
        redis = await store._get_redis()
        slot_count = await redis.scard(f"voice:concurrent:{tenant.id}")
        return ctx, slot_count

    ctx, slot_count = asyncio.run(_post_failure_state())
    assert ctx is None, "voice token must be revoked, not left orphaned"
    assert slot_count == 0, "concurrency slot must be released, not leaked"


def test_mint_returns_503_when_redis_unreachable(
    pg_client, voice_tenant_factory, voice_session_factory, monkeypatch
):
    """A Redis outage must surface as a controlled 503 with a machine-readable
    reason, never an unhandled 500."""
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "voice_provider_unavailable"
