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

# --- The real ElevenLabs custom-LLM request shape -----------------------
# Spike 1 (2026-08-19, 28 live requests captured against a real throwaway
# ElevenLabs Agent) proved the request body contains EXACTLY `messages`,
# `model`, `max_tokens`, `stream`, `stream_options`, `temperature`, `tools`
# -- no `elevenlabs_extra_body`, no `dynamic_variables`, no `user`. The old
# `_body()` helper here built `{"elevenlabs_extra_body": {"voice_token":
# token}}`, which never once appeared on the wire -- every test built on it
# was proving nothing about a real ElevenLabs request. `_body()` below
# instead embeds the token the real way: substituted into `messages[0]`
# (the system prompt) as `Session: <token>`, mirroring the real captured
# shape (see tests/voice/test_voice_token_extraction.py for the literal
# captures and the parser's own dedicated unit tests).


def _system_content(token: str) -> str:
    return (
        "Task description: You are an AI agent. Your current language is: en=English \n"
        f"                Agent character description: Session: {token}\n"
        "                Everything following this section is the documentation "
        "knowledge base.\n                No additional documentation provided"
    )


def _body(voice_token: str, user_text: str = "what are your hours?") -> dict:
    return {
        "model": "luran-voice",
        "stream": True,
        "messages": [
            {"role": "system", "content": _system_content(voice_token)},
            {"role": "user", "content": user_text},
        ],
    }


# Spike 1, captures.jsonl line 22: real captured system-prompt content where
# EL left the literal template text `{{voice_token}}` unsubstituted (the
# mint call in that capture ran without `participant_name`). Used verbatim
# as a negative control at the full endpoint level -- the parser-level unit
# test for the same fixture lives in test_voice_token_extraction.py; this
# proves the SAME fixture also 401s through the real route, dependencies,
# and all.
_UNSUBSTITUTED_CAPTURED_CONTENT = 'Task description: You are an AI agent. Your character definition is provided below, stick to it. You should not provide any personal information. You should also not provide any medical, legal, or financial advice. You should not provide any information that is false or misleading. You should not provide any information that is offensive or inappropriate. You should not provide any information that is harmful or dangerous. You should not provide any information that is confidential or proprietary. You should not provide any information that is copyrighted or trademarked. Do not format your text response with bullet points, bold or headers. Since your answers will be converted to audio, make sure to not use symbols like $, %, #, @, etc. or digits in your responses, if you need to use them write them out as words e.g. "three dollars", "hashtag", "one", "two", etc.". Unless specified differently in the character answer in around 3-4 sentences for most cases. If a user responds with \'...\' it means that they didn\'t respond or say anything, you should prompt them to speak, or if they don\'t respond for a while then ask if they\'re still there. You may also be supplied with an additional documentation knowledge base which may contain information that will help you to answer questions from the user. Your current language is: en=English \n                Agent character description: Session: {{voice_token}}\n                Everything following this section is the documentation knowledge base. You can use this information to answer questions from the user though it might not be needed. It is supplied as a series of documents, each in HTML or Markdown format.\n                No additional documentation provided\nThe current date and time is Wednesday, 18:12 19 August 2026 (Africa/Lagos)\n                '

# Spike 1, captures.jsonl line 4: real captured system-prompt content from
# an agent whose character description never had the `Session:` template
# text at all -- the operator-never-configured-it case.
_NO_MARKER_CAPTURED_CONTENT = 'Task description: You are an AI agent. Your character definition is provided below, stick to it. You should not provide any personal information. You should also not provide any medical, legal, or financial advice. You should not provide any information that is false or misleading. You should not provide any information that is offensive or inappropriate. You should not provide any information that is harmful or dangerous. You should not provide any information that is confidential or proprietary. You should not provide any information that is copyrighted or trademarked. Do not format your text response with bullet points, bold or headers. Since your answers will be converted to audio, make sure to not use symbols like $, %, #, @, etc. or digits in your responses, if you need to use them write them out as words e.g. "three dollars", "hashtag", "one", "two", etc.". Unless specified differently in the character answer in around 3-4 sentences for most cases. If a user responds with \'...\' it means that they didn\'t respond or say anything, you should prompt them to speak, or if they don\'t respond for a while then ask if they\'re still there. You may also be supplied with an additional documentation knowledge base which may contain information that will help you to answer questions from the user. Your current language is: en=English \n                Agent character description: \n                Everything following this section is the documentation knowledge base. You can use this information to answer questions from the user though it might not be needed. It is supplied as a series of documents, each in HTML or Markdown format.\n                No additional documentation provided\nThe current date and time is Wednesday, 15:11 19 August 2026 (Africa/Lagos)\n                '


def _fake_chat_response(session_id, text, *, should_escalate=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        session_id=session_id,
        message=SimpleNamespace(content=text),
        should_escalate=should_escalate,
        escalation_reason="customer asked for a person" if should_escalate else None,
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
    earlier test fails with "Event loop is closed" here -- `_turn_allowed`
    fails open on that (see voice.py), but resetting the cached client keeps
    every test's rate-limit behaviour deterministic rather than silently
    skipped. Same pattern as `test_mint_is_rate_limited` in test_token_mint.py.
    """
    voice_llm_turn_rate_limiter._redis = None
    yield
    voice_llm_turn_rate_limiter._redis = None


def _auth():
    return {"Authorization": "Bearer s3cret"}


def _frames(resp) -> list[dict]:
    out = []
    for line in resp.text.split("\n\n"):
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        out.append(json.loads(line[len("data: ") :]))
    return out


def _spoken_text(resp) -> str:
    contents = [f["choices"][0]["delta"].get("content") for f in _frames(resp)]
    return "".join(c for c in contents if c)


def test_unauthenticated_request_is_rejected(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A request missing the ElevenLabs shared secret must be rejected by
    `verify_elevenlabs_request` itself. Using a REAL, resolvable voice token
    here is what isolates the auth guard: with a bogus token, this test would
    still pass at 401 even if the guard were deleted entirely, because
    resolve() would separately 401 it (see test_unknown_voice_token_is_rejected)
    -- same status, no signal. Asserting the exact `detail` string that only
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


def test_unsubstituted_template_placeholder_is_rejected(pg_client):
    """Fail-closed proof #1 at the FULL endpoint level (see
    test_voice_token_extraction.py for the parser-unit-level proof against
    the same literal capture): an agent whose prompt still carries the
    unsubstituted `{{voice_token}}` literal must 401, never resolve into
    some guessed/partial session."""
    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": _UNSUBSTITUTED_CAPTURED_CONTENT},
            {"role": "user", "content": "hello"},
        ],
    }
    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())
    assert resp.status_code == 401


def test_no_session_marker_at_all_is_rejected(pg_client):
    """Fail-closed proof #2 at the full endpoint level: an operator who
    never added the `Session: {{voice_token}}` template text to their
    Agent's character description at all must also 401."""
    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": _NO_MARKER_CAPTURED_CONTENT},
            {"role": "user", "content": "hello"},
        ],
    }
    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())
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
    binding may ever reach `send_message`."""
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
    body["elevenlabs_extra_body"] = {"voice_token": "totally-different-token"}

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
    denial fallbacks -- a test that only checks "sorry" is in the text can't
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
    fall back to re-sending the previous user message -- that would silently
    re-drive the pipeline (re-running lead scoring, the action engine, etc.)
    with stale text and speak the old answer back to a caller who said
    nothing new this turn."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": _system_content(token)},
            {"role": "user", "content": "what are your hours?"},
            {"role": "assistant", "content": "We open at nine."},
            {"role": "user", "content": ""},
        ],
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
    treated as no input too."""
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
    """`_NO_INPUT_REPLY` previously had zero test coverage -- this pins it to
    a body that never had a user turn to begin with."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [{"role": "system", "content": _system_content(token)}],
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
    be treated as no input, never crash the extraction into a 500 -- this is
    specifically about `_last_user_message`'s guard on `messages[-1]`, so
    `messages[0]` here is a VALID system message carrying a real, resolvable
    token: a malformed *first* message is `_extract_voice_token`'s own
    fail-closed concern, covered separately in
    test_voice_token_extraction.py and by
    test_no_session_marker_at_all_is_rejected above."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": _system_content(token)},
            "not a dict",
        ],
    }

    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    assert resp.status_code == 200
    spoken = _spoken_text(resp)
    assert "did not catch" in spoken.lower()


def test_resolve_failure_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A Redis outage (or any other failure) during token resolution must not
    leave the caller in silence -- this happens before we even know who the
    caller is, so it can't get the tenant-aware `_FALLBACK_REPLY` logging,
    but it must still end in a speakable 200 carrying EXACTLY the generic
    setup-failure fallback."""
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
    conversation-limit-reached) is not transient -- retrying with the same
    words can never succeed. It must speak EXACTLY `_TERMINAL_REPLY` and
    revoke the voice token so the *next* turn 401s cleanly."""
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
    that token must still get a speakable 200 carrying EXACTLY
    `_RATE_LIMITED_REPLY`, and the pipeline must not be invoked for a
    throttled turn."""
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
    both pass the fast usage check and then race for the same slot. A stuck
    `acquire()` must resolve to a throttled (but still speakable, still
    fast) turn, not hang the request."""
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
        tick_key = f"voice:session_tick:{token}"
        ttl = await redis.ttl(tick_key)
        await redis.set(tick_key, time.time() - seconds_ago, ex=ttl if ttl > 0 else 600)

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


# --- escalate_to_human: tool_calls emission (Spike 1 fix) ----------------
# Confirmed live: ElevenLabs only invokes the escalate_to_human CLIENT tool
# when it sees an OpenAI tool_calls delta from us. Before this fix,
# `voice_custom_llm` never read `chat_response.should_escalate` at all --
# escalation was dead end-to-end even though `ChatResponse` already carried
# the flag; the pipeline's decision never reached ElevenLabs in a shape it
# would act on.


def test_escalation_emits_tool_call_instead_of_spoken_text(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(
            return_value=_fake_chat_response(
                session.id, "Let me get you a human.", should_escalate=True
            )
        ),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    payloads = _frames(resp)
    tool_call_frames = [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]]
    assert len(tool_call_frames) == 1, "expected exactly one tool_calls delta"
    call = tool_call_frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "escalate_to_human"

    finishes = [p for p in payloads if p["choices"][0]["finish_reason"] is not None]
    assert len(finishes) == 1
    assert finishes[0]["choices"][0]["finish_reason"] == "tool_calls"

    # The pipeline's spoken text must NOT leak out as content when
    # escalating -- ElevenLabs would try to speak it AND invoke the tool,
    # which isn't the intended UX (the tool call itself is what triggers
    # the widget's switch-to-text handoff, see voice_escalate_to_human).
    spoken = _spoken_text(resp)
    assert "Let me get you a human." not in spoken


def test_non_escalating_reply_still_speaks_text_not_a_tool_call(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """Regression guard the other direction: a normal (non-escalating) reply
    must keep speaking text, not suddenly emit a tool call."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    with patch(
        "app.api.v1.endpoints.voice.send_message",
        new=AsyncMock(
            return_value=_fake_chat_response(session.id, "We open at nine.", should_escalate=False)
        ),
    ):
        resp = pg_client.post(
            "/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth()
        )

    assert resp.status_code == 200
    payloads = _frames(resp)
    tool_call_frames = [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]]
    assert tool_call_frames == []
    assert "We open at nine." in _spoken_text(resp)


# --- the tool-result callback turn (the trap) -----------------------------
# After the escalate_to_human CLIENT tool runs, ElevenLabs calls this
# endpoint AGAIN with the tool's result appended as the next turn:
# {"role": "tool", "tool_call_id": ..., "content": ...}. `_last_user_message`
# only ever inspects `messages[-1]` and returns "" for any non-"user" role
# -- so unhandled, this turn would hit `_NO_INPUT_REPLY` ("Sorry, I did not
# catch that") immediately after an escalation, which is confusing and
# wrong: the caller didn't say anything unclear, EL just reported back a
# tool result.


def test_tool_result_turn_speaks_ack_and_does_not_redrive_pipeline(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session = voice_session_factory(tenant.id)
    token = minted_voice_token(tenant.id, session.id)

    body = {
        "model": "luran-voice",
        "messages": [
            {"role": "system", "content": _system_content(token)},
            {"role": "user", "content": "I need to speak to a human"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "escalate_to_human", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc123", "content": "switched to text"},
        ],
    }

    mock = AsyncMock(return_value=_fake_chat_response(session.id, "should not be called"))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=body, headers=_auth())

    mock.assert_not_called()
    assert resp.status_code == 200
    payloads = _frames(resp)
    assert [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]] == []
    spoken = _spoken_text(resp)
    assert "did not catch" not in spoken.lower(), (
        "a tool-result callback turn must not be mistaken for no-input -- " f"got: {spoken!r}"
    )
    assert spoken.strip() != "...".strip()
