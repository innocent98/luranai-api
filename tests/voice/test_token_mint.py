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
        "user_identifier": f"visitor-{session_id}",
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


def test_mint_passes_voice_token_as_participant_name(
    pg_client, voice_tenant_factory, voice_session_factory
):
    """Spike 1 proved `participant_name` is the ONLY wire mechanism that
    carries our opaque voice_token to ElevenLabs (see
    ElevenLabsClient.mint_conversation_token's docstring). If the endpoint
    stopped wiring `voice_token` through as `participant_name` -- e.g.
    reverted to the old (never-transmitted) `dynamic_variables` shape, or
    passed some other value -- every custom-LLM request for that call would
    silently carry no token at all, and `_extract_voice_token` would 401
    every turn. This asserts the actual kwargs `mint_conversation_token` was
    called with, not just that mint succeeded."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)

    mock_mint = AsyncMock(return_value="el-convo-token-abc")
    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token", new=mock_mint
    ):
        resp = pg_client.post("/api/v1/voice/session/token", json=_payload(tenant.id, session.id))

    assert resp.status_code == 200
    voice_token = resp.json()["voice_token"]
    mock_mint.assert_awaited_once_with(
        agent_id="test-agent-id",
        participant_name=voice_token,
    )


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
    assert resp.json()["detail"]["reason"] == "session_not_found"


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
        # Concurrency slots live in a sorted set (see gating.py) so each
        # member can expire individually — zcard, not scard.
        slot_count = await redis.zcard(f"voice:concurrent:{tenant.id}")
        return ctx, slot_count

    ctx, slot_count = asyncio.run(_post_failure_state())
    assert ctx is None, "voice token must be revoked, not left orphaned"
    assert slot_count == 0, "concurrency slot must be released, not leaked"


def test_mint_revokes_token_and_releases_slot_on_unexpected_el_error(
    pg_client, voice_tenant_factory, voice_session_factory
):
    """Cleanup after a reserved slot must not depend on the failure being
    specifically an ElevenLabsMintError. Any unanticipated exception from
    the EL call — a client bug, a response shape nobody guarded against yet
    — must still release the slot and revoke the token via the endpoint's
    broad `except Exception` + `finally`, not just the one error type this
    module happens to define. Otherwise a bug in the client becomes a
    leaked concurrency slot on top of whatever else it broke.
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
            new=AsyncMock(side_effect=RuntimeError("not an ElevenLabsMintError")),
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
        slot_count = await redis.zcard(f"voice:concurrent:{tenant.id}")
        return ctx, slot_count

    ctx, slot_count = asyncio.run(_post_failure_state())
    assert ctx is None, "voice token must be revoked for an unanticipated exception type too"
    assert (
        slot_count == 0
    ), "concurrency slot must be released for an unanticipated exception type too"


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


def test_mint_is_rate_limited(pg_client, voice_tenant_factory, voice_session_factory, monkeypatch):
    """A single caller looping this endpoint must eventually get 429 — the
    concurrency ceiling only throttles how many *simultaneous* sessions a
    tenant can hold, not how fast one caller can churn through requests,
    each of which is a DB read plus Redis writes and (when not denied by
    policy first) a billable ElevenLabs call.

    Uses a policy-denied tenant (can_use_voice=False) so the requests that
    get through the rate limiter cheaply 409 rather than needing an
    ElevenLabs mock — rate limiting runs as a dependency before the route
    body, so a denied-by-policy request still counts against it.
    """
    from app.utils.http_rate_limiter import voice_rate_limiter

    monkeypatch.setattr(voice_rate_limiter, "max_calls", 2)
    # voice_rate_limiter is a process-wide singleton (shared with every other
    # test in the suite) that lazily caches its async Redis client on first
    # use and never recreates it. Each test's TestClient runs its own event
    # loop, so a cached client left over from an earlier test is bound to a
    # loop that's since been closed — its first use here fails with "Event
    # loop is closed", gets swallowed by check_rate_limit's fail-open
    # handling (RATE_LIMIT_FAIL_OPEN=True), and silently skips incrementing
    # the counter, throwing off the exact count this test depends on. Force
    # a fresh client bound to *this* test's loop so the three calls below
    # are deterministic regardless of what ran before this test in the
    # suite.
    monkeypatch.setattr(voice_rate_limiter, "_redis", None)

    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    payload = _payload(tenant.id, session.id)

    resp1 = pg_client.post("/api/v1/voice/session/token", json=payload)
    resp2 = pg_client.post("/api/v1/voice/session/token", json=payload)
    resp3 = pg_client.post("/api/v1/voice/session/token", json=payload)

    assert resp1.status_code == 409
    assert resp2.status_code == 409
    assert resp3.status_code == 429


def _mint(pg_client, payload):
    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-abc"),
    ):
        return pg_client.post("/api/v1/voice/session/token", json=payload)


def test_mint_without_customer_credential_is_403(
    pg_client, voice_tenant_factory, voice_session_factory
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    base = _payload(tenant.id, session.id)
    del base["user_identifier"]
    resp = _mint(pg_client, base)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": {"reason": "invalid_customer_credential"}}
    assert _mint(pg_client, {**base, "user_identifier": "nope"}).status_code == 403
    assert _mint(pg_client, {**base, "visitor_token": "nope"}).status_code == 403


def test_mint_accepts_visitor_token_credential(
    pg_client, voice_tenant_factory, voice_session_factory, db_session
):
    from uuid import uuid4

    from app.db.models.visitor import Visitor, VisitorSource
    from app.utils.datetime import utcnow_naive

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    now = utcnow_naive()
    visitor = Visitor(
        tenant_id=tenant.id,
        public_token=f"vtok-{uuid4().hex}",
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db_session.add(visitor)
    db_session.flush()
    session.visitor_id = visitor.id
    db_session.commit()
    base = _payload(tenant.id, session.id)
    del base["user_identifier"]
    resp = _mint(pg_client, {**base, "visitor_token": visitor.public_token})
    assert resp.status_code == 200, resp.text


def _customer_headers(db_session, *, owns_session=None):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from jose import jwt

    from app.db.models.customer_user import CustomerUser

    customer = CustomerUser(
        full_name="Cus Tomer",
        email=f"c-{uuid4().hex[:6]}@example.com",
        email_hash=uuid4().hex + uuid4().hex,
        hashed_password="x",
        is_active=True,
    )
    db_session.add(customer)
    db_session.flush()
    if owns_session is not None:
        owns_session.customer_user_id = customer.id
    db_session.commit()
    token = jwt.encode(
        {
            "sub": str(customer.id),
            "auth_type": "customer",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


def test_mint_accepts_owning_customer_token_without_explicit_credential(
    pg_client, voice_tenant_factory, voice_session_factory, db_session
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    # authed customers can own a session whose user_identifier != chat_user_key
    hdrs = _customer_headers(db_session, owns_session=session)
    base = _payload(tenant.id, session.id)
    del base["user_identifier"]
    with patch(
        "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
        new=AsyncMock(return_value="el-convo-token-abc"),
    ):
        resp = pg_client.post("/api/v1/voice/session/token", json=base, headers=hdrs)
    assert resp.status_code == 200, resp.text


def test_mint_rejects_stranger_customer_token_without_explicit_credential(
    pg_client, voice_tenant_factory, voice_session_factory, db_session
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    hdrs = _customer_headers(db_session)
    base = _payload(tenant.id, session.id)
    del base["user_identifier"]
    resp = pg_client.post("/api/v1/voice/session/token", json=base, headers=hdrs)
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": {"reason": "invalid_customer_credential"}}


def test_mint_accepts_owning_customer_by_dedicated_cookie(
    pg_client, voice_tenant_factory, voice_session_factory, db_session
):
    """Browsers authenticate customers with the dedicated cookie, not a Bearer header."""
    from app.core.security import CUSTOMER_ACCESS_COOKIE

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = _customer_headers(db_session, owns_session=session)["Authorization"][7:]
    base = _payload(tenant.id, session.id)
    del base["user_identifier"]
    pg_client.cookies.set(CUSTOMER_ACCESS_COOKIE, token)
    try:
        with patch(
            "app.api.v1.endpoints.voice.ElevenLabsClient.mint_conversation_token",
            new=AsyncMock(return_value="el-convo-token-abc"),
        ):
            resp = pg_client.post("/api/v1/voice/session/token", json=base)
    finally:
        pg_client.cookies.clear()
    assert resp.status_code == 200, resp.text
