"""Voice escalation UX: no buffer word ahead of a known tool call, and no
echo of the caller's own words on an already-escalated session."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.api.v1.endpoints import voice
from app.db.models.conversation import MessageRole
from app.services.rag_service import RAGService
from app.services.voice import turn_dedupe
from app.services.voice.session_tokens import VoiceSessionContext

TOKEN = "A" * 43


def _payload(text="hello there", extra=()):
    msgs = [{"role": "system", "content": f"Session: {TOKEN}"}, {"role": "user", "content": text}]
    msgs.extend(extra)
    # the live user turn must be last
    msgs.append({"role": "user", "content": text})
    return {"model": "luran-voice", "messages": msgs}


def _ctx():
    return VoiceSessionContext(
        tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False, minted_at=time.time()
    )


def _request():
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)
    return req


def _reply(text, *, escalate=False, role=MessageRole.ASSISTANT):
    return SimpleNamespace(
        message=SimpleNamespace(content=text, role=role),
        should_escalate=escalate,
        escalation_reason="asked" if escalate else None,
    )


def _deltas(frames):
    out = []
    for f in frames:
        f = f.strip()
        out.append("DONE" if f == "data: [DONE]" else json.loads(f[6:])["choices"][0])
    return out


_patches: list = []


async def _call(payload, ctx, *, send):
    for p in (
        patch.object(voice.VoiceSessionTokenStore, "resolve", AsyncMock(return_value=ctx)),
        patch.object(voice, "_turn_allowed", AsyncMock(return_value=True)),
        patch.object(voice, "send_message", send),
        patch.object(voice, "_open_stream_session", lambda: MagicMock()),
    ):
        p.start()
        _patches.append(p)
    return await voice.voice_custom_llm(request=_request(), payload=payload)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    while _patches:
        _patches.pop().stop()


@pytest.fixture(autouse=True)
def _no_dedupe():
    with (
        patch.object(turn_dedupe, "claim", AsyncMock(return_value=True)),
        patch.object(turn_dedupe, "store", AsyncMock()),
        patch.object(turn_dedupe, "release", AsyncMock()),
    ):
        yield


async def _drain(resp):
    return [c async for c in resp.body_iterator]


def _has_content(frames):
    return any("content" in c["delta"] for c in _deltas(frames)[:-1])


def _tool_names(frames):
    return [
        tc["function"]["name"]
        for c in _deltas(frames)[:-1]
        for tc in c["delta"].get("tool_calls", [])
    ]


# --- shared predicate -------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I want to talk to a human agent", True),
        ("can I speak to someone please", True),
        ("get me customer service", True),
        ("thanks, that's all", False),
        ("what are your opening hours", False),
    ],
)
def test_explicit_human_request_predicate(text, expected):
    assert RAGService.is_explicit_human_request(text) is expected


# --- no buffer word before a known tool call --------------------------------


async def test_explicit_human_request_never_emits_content_even_when_slow(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 0.01)

    async def slow(*_a, **_k):
        await asyncio.sleep(0.1)
        return _reply("x", escalate=True)

    resp = await _call(
        _payload("I want to speak to a human agent"), _ctx(), send=AsyncMock(side_effect=slow)
    )
    frames = await _drain(resp)
    assert not _has_content(frames)
    assert _tool_names(frames) == [voice._ESCALATE_TOOL_NAME]
    assert _deltas(frames)[-2]["finish_reason"] == "tool_calls"


async def test_non_human_slow_turn_still_gets_buffer_word(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 0.01)

    async def slow(*_a, **_k):
        await asyncio.sleep(0.1)
        return _reply("an answer")

    frames = await _drain(
        await _call(_payload("what are your hours"), _ctx(), send=AsyncMock(side_effect=slow))
    )
    assert _deltas(frames)[1]["delta"] == {"content": "... "}


async def test_explicit_request_that_pipeline_does_not_escalate_still_speaks():
    # e.g. a flow/required-contact capture asks for the email first.
    frames = await _drain(
        await _call(
            _payload("talk to a human agent"),
            _ctx(),
            send=AsyncMock(return_value=_reply("What is your email?")),
        )
    )
    assert _has_content(frames)
    assert _tool_names(frames) == []


# --- already-escalated session: never echo ----------------------------------


async def test_escalated_session_returns_tool_call_not_echo():
    echo = _reply("anything the caller said", role=MessageRole.USER)
    frames = await _drain(
        await _call(_payload("anything the caller said"), _ctx(), send=AsyncMock(return_value=echo))
    )
    assert not _has_content(frames)
    assert _tool_names(frames) == [voice._ESCALATE_TOOL_NAME]


async def test_escalated_session_after_tool_already_invoked_speaks_line_then_tool():
    echo = _reply("still there?", role=MessageRole.USER)
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    frames = await _drain(
        await _call(
            _payload("still there?", extra=history), _ctx(), send=AsyncMock(return_value=echo)
        )
    )
    spoken = "".join(c["delta"].get("content", "") for c in _deltas(frames)[:-1])
    assert voice._ESCALATED_CHAT_REPLY.strip() in spoken
    assert "still there" not in spoken
    assert _tool_names(frames) == [voice._ESCALATE_TOOL_NAME]
    kinds = [
        "content" if "content" in c["delta"] else "tool" if "tool_calls" in c["delta"] else "o"
        for c in _deltas(frames)[:-1]
    ]
    assert kinds.index("tool") > max(i for i, k in enumerate(kinds) if k == "content")


async def test_escalated_echo_outcome_is_cached_for_retry_dedupe():
    store = AsyncMock()
    echo = _reply("hi", role=MessageRole.USER)
    with patch.object(turn_dedupe, "store", store):
        await _drain(await _call(_payload("hi"), _ctx(), send=AsyncMock(return_value=echo)))
    stored = store.await_args.args[1]
    assert stored["tool_name"] == voice._ESCALATE_TOOL_NAME
    assert "hi" not in (stored["text"] or "")
