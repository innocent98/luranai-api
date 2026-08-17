"""Tests for POST /api/v1/voice/session/end.

Since Task 11 review round 2, metering is NOT primarily enforced by this
route -- `VoiceSessionTokenStore.resolve()` meters incrementally on every
conversational turn (see tests/voice/test_session_tokens.py), because a
widget that crashes, loses network, or is simply closed never calls this
route at all. `/session/end` now only bills the small trailing delta since
the last turn and frees the concurrency slot. Tests here therefore focus on
the route's own concerns (status codes, slot release, idempotency, failure
isolation) rather than re-proving the metering math, which is covered at the
store level. Tests that need a non-trivial metered delta backdate BOTH
`minted_at` and `last_metered_at` directly in Redis (mirroring
`test_session_tokens.py`'s `_set_ticks` helper), simulating "a call that
started X seconds ago and has had no intervening turn."
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


def _backdate_ticks(token: str, seconds_ago: float) -> None:
    """Rewrite a minted token's `minted_at` AND `last_metered_at` to
    simulate a call that started `seconds_ago` seconds ago and has had no
    turn (no `resolve()` call) since, preserving its TTL."""

    async def _do() -> None:
        store = VoiceSessionTokenStore()
        redis = await store._get_redis()
        key = f"voice:session_token:{token}"
        raw = await redis.get(key)
        assert raw is not None, "token must exist before it can be backdated"
        data = json.loads(raw)
        backdated = time.time() - seconds_ago
        data["minted_at"] = backdated
        data["last_metered_at"] = backdated
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


def test_session_end_releases_slot_and_meters_the_trailing_delta(
    pg_client, db_session, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_ticks(token, seconds_ago=90)

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
    meters a paid resource -- the endpoint must derive its trailing delta
    from server-recorded timestamps and ignore any `duration_seconds` the
    request body claims, even if such a field is present."""
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
    _backdate_ticks(token, seconds_ago=60)

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
    """A failure in `resolve_and_revoke`'s internal metering (swallowed
    inside `VoiceSessionTokenStore`, see its docstring) must not prevent the
    (self-healing but still worth freeing promptly) concurrency slot from
    being released by the route."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)
    _reserve_slot(db_session, tenant.id, token)
    _backdate_ticks(token, seconds_ago=60)

    with patch(
        "app.services.voice.metering.VoiceMeter.add_minutes",
        new=AsyncMock(side_effect=RuntimeError("redis boom")),
    ):
        resp = pg_client.post("/api/v1/voice/session/end", json={"voice_token": token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _slot_count(tenant.id) == 0
    # Proves the failure was real, not a no-op: the delta that should have
    # been billed genuinely wasn't.
    assert _monthly_minutes(tenant.id) == 0


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
    _backdate_ticks(token, seconds_ago=60)

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


def test_session_end_has_no_rate_limit_dependency():
    """Review round 2 finding: `/session/end` must NOT share a counter with
    `/session/token`'s `voice_rate_limit` (same `http_voice` Redis prefix) --
    doing so silently halved the mint budget (every call now costs 2 units)
    and could 429 the cleanup/billing path itself for visitors behind shared
    NAT. Asserted structurally against the route's dependants rather than by
    hammering the endpoint N times, which would be both slow and a weaker
    proof (a sufficiently high shared limit could hide the sharing)."""
    from app.api.v1.endpoints.voice import router

    end_route = next(r for r in router.routes if r.path == "/session/end")
    dependency_calls = {dep.call for dep in end_route.dependant.dependencies}
    from app.utils.http_rate_limiter import voice_rate_limit

    assert voice_rate_limit not in dependency_calls
