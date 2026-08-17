"""Tests for POST /api/v1/voice/session/end.

Duration is deliberately derived server-side from the token's recorded
`minted_at`, never from a client-supplied field -- see the design note on
`end_voice_session` in app/api/v1/endpoints/voice.py. Tests that need a
non-trivial elapsed duration therefore backdate `minted_at` directly in
Redis (the same pattern `test_session_tokens.py` already uses to simulate
"deep into a long call"), rather than asking the endpoint to trust a
duration the request body claims.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.services.voice.gating import VoiceGate
from app.services.voice.metering import VoiceMeter
from app.services.voice.session_tokens import VoiceSessionTokenStore


def _backdate_minted_at(token: str, seconds_ago: float) -> None:
    """Rewrite a minted token's `minted_at` to simulate a call that has
    already been running for `seconds_ago` seconds, preserving its TTL."""

    async def _do() -> None:
        store = VoiceSessionTokenStore()
        redis = await store._get_redis()
        key = f"voice:session_token:{token}"
        raw = await redis.get(key)
        assert raw is not None, "token must exist before it can be backdated"
        data = json.loads(raw)
        data["minted_at"] = time.time() - seconds_ago
        ttl = await redis.ttl(key)
        await redis.set(key, json.dumps(data), ex=ttl if ttl > 0 else 600)

    asyncio.run(_do())


def _slot_count(tenant_id) -> int:
    async def _do() -> int:
        store = VoiceSessionTokenStore()
        redis = await store._get_redis()
        return await redis.zcard(f"voice:concurrent:{tenant_id}")

    return asyncio.run(_do())


def _monthly_minutes(tenant_id) -> float:
    return asyncio.run(VoiceMeter().get_monthly_minutes(tenant_id))


def _reserve_slot(db_session, tenant_id, token) -> None:
    """Seed a real reserved concurrency slot the same way the mint route
    does (`VoiceGate.acquire_slot`) -- session/end tests need a slot to
    actually exist so releasing it is provable, without going through the
    full mint endpoint (which needs a mocked ElevenLabs client)."""
    asyncio.run(VoiceGate(db_session).acquire_slot(tenant_id, token))


def test_session_end_releases_slot_and_meters(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_minted_at(token, seconds_ago=90)

    resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _monthly_minutes(tenant.id) == pytest.approx(1.5, abs=0.05)
    assert _slot_count(tenant.id) == 0


def test_session_end_frees_slot_for_a_subsequent_mint_at_the_ceiling(
    pg_client,
    db_session,
    voice_tenant_factory,
    voice_session_factory,
    minted_voice_token,
    monkeypatch,
):
    """The concrete symptom this task closes: without a release, a tenant at
    its concurrency ceiling stays locked out of voice until the 2h ZSET
    eviction. Prove the fix by actually minting again after ending a call."""
    monkeypatch.setattr(settings, "ELEVENLABS_AGENT_ID", "test-agent-id")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "test-api-key")
    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", 1)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_a = voice_session_factory(tenant.id)
    session_b = voice_session_factory(tenant.id)
    token_a = minted_voice_token(tenant.id, session_a.id)
    _reserve_slot(db_session, tenant.id, token_a)

    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-1"),
    ):
        denied = pg_client.post(
            "/api/v1/voice/session/token",
            json={
                "tenant_id": str(tenant.id),
                "luran_session_id": str(session_b.id),
                "text_out": False,
            },
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["reason"] == "concurrency_limit"

        end_resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token_a})
        assert end_resp.status_code == 200

        allowed = pg_client.post(
            "/api/v1/voice/session/token",
            json={
                "tenant_id": str(tenant.id),
                "luran_session_id": str(session_b.id),
                "text_out": False,
            },
        )

    assert allowed.status_code == 200


def test_session_end_ignores_client_supplied_duration(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A client-reported duration is a number a client can lie about, and it
    meters a paid resource -- the endpoint must derive elapsed time from the
    server-recorded mint time and ignore any `duration_seconds` the request
    body claims, even if such a field is present."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    # Real elapsed time since mint is a few milliseconds; the claimed
    # duration below is wildly larger and must be rejected/ignored.
    resp = pg_client.post(
        "/api/v1/voice/session/end",
        json={"voice_token": token, "duration_seconds": 36000},
    )

    assert resp.status_code == 200
    assert _monthly_minutes(tenant.id) < 0.01


def test_session_end_is_idempotent_second_call_is_a_noop(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_minted_at(token, seconds_ago=60)

    first = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})
    metered_after_first = _monthly_minutes(tenant.id)

    second = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})
    metered_after_second = _monthly_minutes(tenant.id)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert metered_after_first == pytest.approx(1.0, abs=0.05)
    assert (
        metered_after_second == metered_after_first
    ), "a second call for an already-ended session must not meter again"
    assert _slot_count(tenant.id) == 0


def test_session_end_unknown_token_returns_ok_without_error(pg_client):
    resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": "not-a-real-token"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_session_end_never_500s_and_still_releases_slot_when_metering_fails(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Metering and slot release are independent -- a failure writing the
    metering counter must not prevent the (self-healing but still worth
    freeing promptly) concurrency slot from being released."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_minted_at(token, seconds_ago=60)

    with patch(
        "app.api.v1.endpoints.voice.VoiceMeter.add_minutes",
        new=AsyncMock(side_effect=RuntimeError("redis boom")),
    ):
        resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _slot_count(tenant.id) == 0


def test_session_end_never_500s_and_still_meters_when_slot_release_fails(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """The reverse of the case above: a failure releasing the concurrency
    slot (which self-heals via its own TTL) must not cost us the metering
    write, which has no self-healing path and would silently under-bill."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_minted_at(token, seconds_ago=60)

    with patch(
        "app.api.v1.endpoints.voice.VoiceGate.release_slot",
        new=AsyncMock(side_effect=RuntimeError("redis boom")),
    ):
        resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _monthly_minutes(tenant.id) == pytest.approx(1.0, abs=0.05)


def test_session_end_requires_voice_token_field(pg_client):
    resp = pg_client.post("/api/v1/voice/session/end", json={})
    assert resp.status_code == 422
