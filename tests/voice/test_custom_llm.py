import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from app.core.config import settings
from app.services.voice.session_tokens import VoiceSessionTokenStore
from app.utils.http_rate_limiter import voice_llm_turn_rate_limiter


def _body(voice_token: str, user_text: str = "what are your hours?") -> dict:
    return {
        "model": "luran-voice",
        "stream": True,
        "messages": [
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": user_text},
        ],
        "elevenlabs_extra_body": {"voice_token": voice_token},
    }


def _fake_chat_response(session_id, text):
    from types import SimpleNamespace

    return SimpleNamespace(
        session_id=session_id,
        message=SimpleNamespace(content=text),
        should_escalate=False,
        escalation_reason=None,
        ticket_id=None,
    )


@pytest.fixture(autouse=True)
def _voice_secret(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SHARED_SECRET", "s3cret")


@pytest.fixture(autouse=True)
def _fresh_turn_rate_limiter():
    """`voice_llm_turn_rate_limiter` is a process-wide singleton whose cached
    Redis client is bound to whichever test's event loop created it first.
    Each `pg_client` runs its own loop, so a client left over from an
    earlier test fails with "Event loop is closed" here — `_turn_allowed`
    fails open on that (see voice.py), but resetting the cached client keeps
    every test's rate-limit behaviour deterministic rather than silently
    skipped. Same pattern as `test_mint_is_rate_limited` in test_token_mint.py.
    """
    voice_llm_turn_rate_limiter._redis = None
    yield
    voice_llm_turn_rate_limiter._redis = None


def _auth():
    return {"Authorization": "Bearer s3cret"}


def _spoken_text(resp) -> str:
    frames = [
        json.loads(line[len("data: ") :])
        for line in resp.text.split("\n\n")
        if line.startswith("data: ") and not line.endswith("[DONE]")
    ]
    contents = [f["choices"][0]["delta"].get("content") for f in frames]
    return "".join(c for c in contents if c)


def test_unauthenticated_request_is_rejected(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A request missing the ElevenLabs shared secret must be rejected by
    `verify_elevenlabs_request` itself. Using a REAL, resolvable voice token
    here is what isolates the auth guard: with a bogus token, this test would
    still pass at 401 even if the guard were deleted entirely, because
    resolve() would separately 401 it (see test_unknown_voice_token_is_rejected)
    — same status, no signal. Asserting the exact `detail` string that only
    `auth.py::verify_elevenlabs_request` produces is what pins this to the
    guard specifically.
    """
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=_body(token))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid credentials"


def test_unknown_voice_token_is_rejected(pg_client):
    resp = pg_client.post(
        "/api/v1/voice/llm/chat/completions", json=_body("bogus"), headers=_auth()
    )
    assert resp.status_code == 401


def test_streams_pipeline_reply_behind_buffer_word(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine.")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    spoken = _spoken_text(resp)
    assert spoken.startswith("... ")
    assert "We open at nine." in spoken


def test_pipeline_receives_the_resolved_tenant_and_session(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """The pipeline must be driven by OUR resolved context, never EL-supplied ids."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "ok"))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        pg_client.post("/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth())

    message_request = mock.call_args.args[0]
    assert message_request.session_id == session.id
    assert message_request.tenant_id == tenant.id
    assert message_request.message == "what are your hours?"


def test_cross_tenant_fields_in_body_are_never_honoured(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Injecting tenant B's ids into the body alongside tenant A's real voice
    token must never leak into the pipeline call. Only the token's resolved
    binding may ever reach `send_message`. A bug like

        tenant_id=UUID(payload["tenant_id"]) if payload.get("tenant_id") else context.tenant_id

    — trusting a body-supplied id when present — is exactly the injection the
    brief forbids, and previous versions of this test (which never put tenant
    B's ids anywhere in the body) could not have caught it: the assertion
    `tenant_id != tenant_b.id` was implied by `tenant_id == tenant_a.id` with
    nothing in the request even offering tenant_b as an alternative.
    """
    tenant_a = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    tenant_b = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_a = voice_session_factory(tenant_a.id)
    session_b = voice_session_factory(tenant_b.id)

    token_a = minted_voice_token(tenant_a.id, session_a.id)

    body = _body(token_a)
    body["tenant_id"] = str(tenant_b.id)
    body["session_id"] = str(session_b.id)
    body["user"] = str(tenant_b.id)
    body["dynamic_variables"] = {
        "tenant_id": str(tenant_b.id),
        "session_id": str(session_b.id),
    }

    mock = AsyncMock(return_value=_fake_chat_response(session_a.id, "ok"))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    assert resp.status_code == 200
    message_request = mock.call_args.args[0]
    assert message_request.tenant_id == tenant_a.id
    assert message_request.session_id == session_a.id
    assert message_request.tenant_id != tenant_b.id
    assert message_request.session_id != session_b.id


def test_pipeline_error_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A pipeline exception must not leave the caller in silence, and must be
    distinguishable (by exact spoken content) from the no-input and policy-
    denial fallbacks — a test that only checks "sorry" is in the text can't
    tell `_FALLBACK_REPLY` apart from any of those."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(side_effect=RuntimeError("pipeline exploded")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    spoken = _spoken_text(resp)
    assert spoken == "... I am sorry, I ran into a problem just then. Could you say that again? "


def test_empty_pipeline_reply_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """An empty (but successful) pipeline reply must not leave the caller
    hearing only the buffer word and then silence."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(return_value=_fake_chat_response(session.id, "")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert spoken.strip() != "...", spoken
    assert len(spoken.strip()) > len("...")


def test_stale_empty_last_turn_does_not_redrive_pipeline(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Only the LAST message in the array is the caller's current turn. A
    failed-ASR or silent turn (empty content) after a real answer must not
    fall back to re-sending the previous user message — that would silently
    re-drive the pipeline (re-running lead scoring, the action engine, etc.)
    with stale text and speak the old answer back to a caller who said
    nothing new this turn."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": "what are your hours?"},
            {"role": "assistant", "content": "We open at nine."},
            {"role": "user", "content": ""},
        ],
        "elevenlabs_extra_body": {"voice_token": token},
    }

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine."))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    mock.assert_not_called()
    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert "We open at nine." not in spoken
    assert spoken.strip().endswith("Sorry, I did not catch that. Could you say that again?")


def test_whitespace_only_last_turn_does_not_redrive_pipeline(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A last user turn of a single space (a realistic partial-ASR artifact,
    distinct from a fully empty string but no more meaningful) must be
    treated as no input too. `content == " "` is truthy in Python, so without
    stripping it clears the `if not user_text` gate and reaches the
    pipeline — persisting a junk message and re-firing lead scoring and the
    action engine for zero actual caller input."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = _body(token, user_text=" ")

    mock = AsyncMock()
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    mock.assert_not_called()
    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert spoken == "... Sorry, I did not catch that. Could you say that again? "


def test_no_user_turn_speaks_no_input_fallback(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """`_NO_INPUT_REPLY` previously had zero test coverage — this pins it to
    a body that never had a user turn to begin with."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [{"role": "system", "content": "you are a helpful agent"}],
        "elevenlabs_extra_body": {"voice_token": token},
    }

    mock = AsyncMock()
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    mock.assert_not_called()
    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert spoken == "... Sorry, I did not catch that. Could you say that again? "


def test_malformed_last_message_is_treated_as_no_input(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A `messages` entry that isn't a dict (some malformed EL payload) must
    be treated as no input, never crash the extraction into a 500."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": ["not a dict"],
        "elevenlabs_extra_body": {"voice_token": token},
    }

    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert "did not catch" in spoken.lower()


def test_resolve_failure_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A Redis outage (or any other failure) during token resolution must not
    leave the caller in silence — this happens before we even know who the
    caller is, so it can't get the tenant-aware `_FALLBACK_REPLY` logging,
    but it must still end in a speakable 200 carrying EXACTLY the generic
    setup-failure fallback, not merely something containing "sorry" — every
    one of `_FALLBACK_REPLY`, `_NO_INPUT_REPLY`, and `_TERMINAL_REPLY`
    contains that word, so a substring check can't tell them apart."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.VoiceSessionTokenStore.resolve",
        new=AsyncMock(side_effect=RedisError("redis unreachable")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    spoken = _spoken_text(resp)
    assert spoken == "... I am sorry, I ran into a problem just then. Could you say that again? "


def test_pipeline_policy_denial_speaks_terminal_reply_and_ends_ability_to_answer(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """An entitlement/existence HTTPException from the pipeline (e.g. 402
    conversation-limit-reached) is not transient — retrying with the same
    words can never succeed. It must speak EXACTLY `_TERMINAL_REPLY`, not
    merely something that isn't `_FALLBACK_REPLY`'s or `_NO_INPUT_REPLY`'s
    text (two negatives don't prove the terminal reply was the one actually
    spoken). The voice token must also be revoked so the *next* turn 401s
    cleanly instead of repeating the same rejection — that ends OUR ability
    to answer, not the WebRTC call itself (which stays open until the caller
    or widget disconnects; see the production comment on `_TERMINAL_REPLY`)."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=402, detail={"error": "conversation_limit_reached"}
            )
        ),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert spoken == (
        "... I'm sorry, but I'm not able to continue this call right now. "
        "Please check your account or contact support. Goodbye. "
    )

    ctx = asyncio.run(VoiceSessionTokenStore().resolve(token))
    assert ctx is None, "voice token must be revoked after a terminal pipeline rejection"


def test_turn_rate_limit_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token, monkeypatch
):
    """Once a single voice token exceeds its turn budget, further turns on
    that token must still get a speakable 200 (never a raw 429 ElevenLabs
    can't render as audio) carrying EXACTLY `_RATE_LIMITED_REPLY` — not just
    something other than the buffer word alone, which `_NO_INPUT_REPLY`
    would also satisfy — and the pipeline must not be invoked for a
    throttled turn. This is a per-token limit so it can't be satisfied by
    the existing IP-keyed `voice_rate_limit`."""
    monkeypatch.setattr(voice_llm_turn_rate_limiter, "max_calls", 1)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine."))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp1 = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )
        resp2 = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert mock.call_count == 1, "a throttled turn must not reach the pipeline"
    spoken2 = _spoken_text(resp2)
    assert spoken2 == "... Sorry, one moment please. "


def test_turn_rate_limiter_acquire_timeout_throttles_instead_of_hanging(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token, monkeypatch
):
    """`RedisRateLimiter.acquire()` re-checks its own window and can
    `asyncio.sleep` up to `window_seconds + 1` (61s for
    `voice_llm_turn_rate_limiter`) if two concurrent turns on the same token
    both pass the fast usage check and then race for the same slot. A 61s
    hang is far worse than a false-positive throttle on the one endpoint
    whose entire purpose is preventing silence — a stuck `acquire()` must
    resolve to a throttled (but still speakable, still fast) turn, not hang
    the request. Patches `acquire` to hang well past a tiny monkeypatched
    timeout budget and asserts the response returns quickly anyway."""
    import app.api.v1.endpoints.voice as voice_module

    monkeypatch.setattr(voice_module, "_TURN_RATE_LIMIT_TIMEOUT_SECONDS", 0.05)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    async def _hangs(_identifier):
        await asyncio.sleep(3)

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine."))
    with (
        patch.object(voice_llm_turn_rate_limiter, "acquire", new=_hangs),
        patch("app.api.v1.endpoints.voice.send_message", new=mock),
    ):
        start = time.monotonic()
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )
        elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"a stuck acquire() must be bounded by the timeout, took {elapsed:.2f}s"
    assert resp.status_code == 200
    mock.assert_not_called()
    spoken = _spoken_text(resp)
    assert spoken == "... Sorry, one moment please. "


def test_a_single_turn_meters_minutes_without_session_end_ever_being_called(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Task 11 review round 2, finding #1: metering must not depend on the
    widget calling `/voice/session/end` -- a crashed or disconnected client
    never does, so a purely end-triggered meter enforces nothing. This is
    the end-to-end proof: hitting the REAL turn endpoint once, with
    `/voice/session/end` never called at all, must still bill minutes on
    its own via `VoiceSessionTokenStore.resolve()`'s per-turn metering."""
    from app.services.voice.metering import VoiceMeter

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    async def _backdate_last_metered_at(seconds_ago: float) -> None:
        store = VoiceSessionTokenStore()
        redis = await store._get_redis()
        key = f"voice:session_token:{token}"
        raw = await redis.get(key)
        data = json.loads(raw)
        data["last_metered_at"] = time.time() - seconds_ago
        ttl = await redis.ttl(key)
        await redis.set(key, json.dumps(data), ex=ttl if ttl > 0 else 600)

    # Simulate "the call has been running 2 minutes with no prior turn" by
    # backdating the token's metering tick directly (mirrors
    # test_session_tokens.py's `_set_ticks` helper).
    asyncio.run(_backdate_last_metered_at(120))

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(return_value=_fake_chat_response(session.id, "We open at nine.")),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    metered = asyncio.run(VoiceMeter().get_monthly_minutes(tenant.id))
    assert metered == pytest.approx(2.0, abs=0.05), (
        "the turn itself must have billed the elapsed minutes -- "
        "/voice/session/end was never called in this test"
    )
