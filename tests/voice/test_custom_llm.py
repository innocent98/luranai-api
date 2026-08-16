import asyncio
import json
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
    earlier test fails with "Event loop is closed" here — `_enforce_turn_rate_limit`
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
    but it must still end in a speakable 200."""
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
    assert "sorry" in resp.text.lower()


def test_pipeline_policy_denial_speaks_terminal_reply_and_ends_call(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """An entitlement/existence HTTPException from the pipeline (e.g. 402
    conversation-limit-reached) is not transient — retrying with the same
    words can never succeed. It must not get the retry-inviting
    `_FALLBACK_REPLY` phrasing (that would trap the caller in an infinite
    loop burning ElevenLabs minutes), and the voice token must be revoked so
    the call actually ends rather than repeating the same rejection every
    turn until the caller hangs up."""
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
    assert "ran into a problem just then" not in spoken
    assert "did not catch" not in spoken

    ctx = asyncio.run(VoiceSessionTokenStore().resolve(token))
    assert ctx is None, "voice token must be revoked after a terminal pipeline rejection"


def test_turn_rate_limit_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token, monkeypatch
):
    """Once a single voice token exceeds its turn budget, further turns on
    that token must still get a speakable 200 (never a raw 429 ElevenLabs
    can't render as audio), and the pipeline must not be invoked for a
    throttled turn — this is a per-token limit so it can't be satisfied by
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
    assert spoken2.strip() != "...", spoken2
