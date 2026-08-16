import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings


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


def test_unauthenticated_request_is_rejected(pg_client):
    resp = pg_client.post("/api/v1/voice/llm/chat/completions", json=_body("tok"))
    assert resp.status_code == 401


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


def test_cross_tenant_voice_token_is_never_honoured(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A voice token minted for tenant A's session must never let a caller
    drive a completion against tenant B's session, even if something upstream
    of the token store passed a mismatched pair in. resolve() is the single
    source of truth for the (tenant, session) binding."""
    tenant_a = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    tenant_b = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    session_a = voice_session_factory(tenant_a.id)

    token = minted_voice_token(tenant_a.id, session_a.id)

    mock = AsyncMock(return_value=_fake_chat_response(session_a.id, "ok"))
    with patch("app.api.v1.endpoints.voice.send_message", new=mock):
        pg_client.post("/api/v1/voice/llm/chat/completions", json=_body(token), headers=_auth())

    message_request = mock.call_args.args[0]
    assert message_request.tenant_id == tenant_a.id
    assert message_request.tenant_id != tenant_b.id


def test_pipeline_error_still_returns_speakable_audio(
    pg_client, voice_tenant_factory, voice_session_factory, minted_voice_token
):
    """A pipeline exception must not leave the caller in silence."""
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
    assert "sorry" in resp.text.lower()


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
