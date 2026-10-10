"""True-streaming behaviour of POST /voice/llm/chat/completions.

These call the endpoint function directly and iterate the StreamingResponse's
body iterator, because TestClient buffers the whole body and so cannot prove
WHEN a frame is emitted relative to the pipeline.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.db.models.conversation import MessageRole
from fastapi import HTTPException

from app.api.v1.endpoints import voice
from app.services.voice import turn_dedupe
from app.services.voice.session_tokens import VoiceSessionContext
from app.services.voice.sse import BUFFER_WORD

TOKEN = "A" * 43


def _payload(text="hello there", history=2):
    msgs = [{"role": "system", "content": f"Session: {TOKEN}"}]
    msgs += [{"role": "user", "content": text}] * (history - 1)
    return {"model": "luran-voice", "messages": msgs}


def _ctx():
    return VoiceSessionContext(
        tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False, minted_at=time.time()
    )


def _request(disconnected=False):
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=disconnected)
    return req


def _reply(text, *, escalate=False):
    return SimpleNamespace(
        message=SimpleNamespace(content=text, role=MessageRole.ASSISTANT),
        should_escalate=escalate,
        escalation_reason="asked" if escalate else None,
    )


def _deltas(frames):
    out = []
    for f in frames:
        f = f.strip()
        if f == "data: [DONE]":
            out.append("DONE")
        else:
            c = json.loads(f[len("data: ") :])["choices"][0]
            out.append((c["delta"], c["finish_reason"]))
    return out


_patches: list = []


async def _call(payload, ctx, *, request=None, send=None, allowed=True):
    # Patches must outlive this call: the response body is a generator that
    # runs the pipeline lazily, after the endpoint function has returned.
    for p in (
        patch.object(voice.VoiceSessionTokenStore, "resolve", AsyncMock(return_value=ctx)),
        patch.object(voice, "_turn_allowed", AsyncMock(return_value=allowed)),
        patch.object(voice, "send_message", send or AsyncMock(return_value=_reply("ok"))),
    ):
        p.start()
        _patches.append(p)
    return await voice.voice_custom_llm(request=request or _request(), payload=payload)


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    while _patches:
        _patches.pop().stop()


async def _drain(resp):
    return [c async for c in resp.body_iterator]


@pytest.fixture(autouse=True)
def _isolated_dedupe():
    store: dict = {}

    async def claim(key):
        if key in store:
            return False
        store[key] = "pending"
        return True

    async def put(key, outcome):
        store[key] = outcome

    async def release(key):
        store.pop(key, None)

    async def wait(key, *, timeout):
        for _ in range(50):
            v = store.get(key)
            if isinstance(v, dict):
                return v
            await asyncio.sleep(0.01)
        return None

    with (
        patch.object(turn_dedupe, "claim", claim),
        patch.object(turn_dedupe, "store", put),
        patch.object(turn_dedupe, "release", release),
        patch.object(turn_dedupe, "wait_for_outcome", wait),
    ):
        yield store


async def test_first_frames_emitted_before_pipeline_finishes():
    release_pipeline = asyncio.Event()
    started = asyncio.Event()

    async def slow_send(*_a, **_k):
        started.set()
        await release_pipeline.wait()
        return _reply("the answer is here")

    resp = await _call(_payload(), _ctx(), send=AsyncMock(side_effect=slow_send))
    it = resp.body_iterator.__aiter__()

    t0 = time.monotonic()
    role = await it.__anext__()
    assert time.monotonic() - t0 < 0.1
    assert '"role": "assistant"' in role
    assert not release_pipeline.is_set()

    buffer = await it.__anext__()  # arrives after the grace period, pipeline still blocked
    assert json.dumps(BUFFER_WORD) in buffer
    assert not release_pipeline.is_set()

    release_pipeline.set()
    rest = [c async for c in it]
    spoken = "".join(d["content"] for d, _ in _deltas(rest[:-1]) if "content" in d)
    assert spoken == "the answer is here "
    assert rest[-1].strip() == "data: [DONE]"


async def test_fast_text_reply_keeps_old_wire_shape():
    resp = await _call(_payload(), _ctx(), send=AsyncMock(return_value=_reply("two words")))
    d = _deltas(await _drain(resp))
    assert d[0] == ({"role": "assistant"}, None)
    assert d[1] == ({"content": BUFFER_WORD}, None)
    assert d[2:4] == [({"content": "two "}, None), ({"content": "words "}, None)]
    assert d[4] == ({}, "stop")
    assert d[5] == "DONE"


async def test_tool_call_turn_has_no_content_when_pipeline_is_fast():
    send = AsyncMock(return_value=_reply("ignored", escalate=True))
    d = _deltas(await _drain(await _call(_payload(), _ctx(), send=send)))
    assert d[0] == ({"role": "assistant"}, None)
    assert "tool_calls" in d[1][0]
    assert d[1][0]["tool_calls"][0]["function"]["name"] == voice._ESCALATE_TOOL_NAME
    assert d[2] == ({}, "tool_calls")
    assert d[3] == "DONE"
    assert not any("content" in x[0] for x in d[:-1])


async def test_tool_call_turn_after_slow_pipeline_still_ends_in_tool_calls(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_GRACE_SECONDS", 0.01)

    async def slow(*_a, **_k):
        await asyncio.sleep(0.05)
        return _reply("x", escalate=True)

    d = _deltas(await _drain(await _call(_payload(), _ctx(), send=AsyncMock(side_effect=slow))))
    assert d[1] == ({"content": BUFFER_WORD}, None)
    assert d[2][0]["tool_calls"][0]["function"]["name"] == voice._ESCALATE_TOOL_NAME
    assert d[3] == ({}, "tool_calls")


def _spoken(frames):
    return "".join(
        d["content"] for d, _ in _deltas(frames)[:-1] if isinstance(d, dict) and "content" in d
    ).replace(BUFFER_WORD, "", 1)


async def test_pipeline_exception_speaks_fallback():
    send = AsyncMock(side_effect=RuntimeError("boom"))
    assert voice._FALLBACK_REPLY in _spoken(
        await _drain(await _call(_payload(), _ctx(), send=send))
    )


async def test_terminal_http_error_speaks_terminal_reply_and_revokes():
    send = AsyncMock(side_effect=HTTPException(status_code=402, detail="limit"))
    with patch.object(voice, "_revoke_voice_token_best_effort", AsyncMock()) as revoke:
        frames = await _drain(await _call(_payload(), _ctx(), send=send))
    assert voice._TERMINAL_REPLY in _spoken(frames)
    revoke.assert_awaited_once()


async def test_empty_reply_speaks_empty_fallback():
    send = AsyncMock(return_value=_reply("  "))
    assert voice._EMPTY_REPLY in _spoken(await _drain(await _call(_payload(), _ctx(), send=send)))


async def test_rate_limited_and_no_input_and_tool_result_branches():
    ctx = _ctx()
    limited = await _call(_payload(), ctx, allowed=False)
    assert voice._RATE_LIMITED_REPLY in _spoken(await _drain(limited))

    empty = {"model": "m", "messages": [{"role": "system", "content": f"Session: {TOKEN}"}]}
    assert voice._NO_INPUT_REPLY in _spoken(await _drain(await _call(empty, ctx)))

    tool = _payload()
    tool["messages"].append({"role": "tool", "tool_call_id": "c", "content": "ok"})
    send = AsyncMock()
    assert voice._ESCALATION_ACK_REPLY in _spoken(await _drain(await _call(tool, ctx, send=send)))
    send.assert_not_awaited()


async def test_streaming_headers_prevent_proxy_buffering():
    for resp in (
        await _call(_payload(), _ctx()),
        await _call(_payload(), _ctx(), allowed=False),
    ):
        assert resp.media_type == "text/event-stream"
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.headers["cache-control"] == "no-cache"
        await _drain(resp)


async def test_disconnect_before_pipeline_start_skips_pipeline():
    send = AsyncMock(return_value=_reply("never"))
    resp = await _call(_payload(), _ctx(), request=_request(disconnected=True), send=send)
    frames = await _drain(resp)
    assert len(frames) == 1  # role frame only
    send.assert_not_awaited()


async def test_retry_of_same_turn_replays_instead_of_rerunning_pipeline():
    ctx = _ctx()
    send = AsyncMock(return_value=_reply("first answer"))
    first = _spoken(await _drain(await _call(_payload(), ctx, send=send)))
    second = _spoken(await _drain(await _call(_payload(), ctx, send=send)))
    assert first == second == "first answer "
    assert send.await_count == 1


async def test_same_words_later_in_longer_history_is_a_new_turn():
    ctx = _ctx()
    send = AsyncMock(return_value=_reply("answer"))
    await _drain(await _call(_payload(history=2), ctx, send=send))
    await _drain(await _call(_payload(history=4), ctx, send=send))
    assert send.await_count == 2


async def test_failed_turn_is_not_cached_so_retry_reruns():
    ctx = _ctx()
    send = AsyncMock(side_effect=[RuntimeError("x"), _reply("recovered")])
    await _drain(await _call(_payload(), ctx, send=send))
    assert "recovered" in _spoken(await _drain(await _call(_payload(), ctx, send=send)))
    assert send.await_count == 2


async def test_abandoned_request_still_finishes_and_retry_replays():
    """EL abandons mid-pipeline (generator closed); the task completes and the
    re-request replays its outcome instead of running a second pipeline."""
    ctx = _ctx()
    gate = asyncio.Event()

    async def slow(*_a, **_k):
        await gate.wait()
        return _reply("late answer")

    send = AsyncMock(side_effect=slow)
    resp = await _call(_payload(), ctx, send=send)
    it = resp.body_iterator.__aiter__()
    await it.__anext__()  # role
    await it.__anext__()  # buffer word: pipeline task is running
    await it.aclose()  # client went away
    gate.set()
    await asyncio.gather(*voice._background_turns)

    retry = await _call(_payload(), ctx, send=send)
    assert "late answer" in _spoken(await _drain(retry))
    assert send.await_count == 1


@pytest.fixture
def session_spy():
    sessions: list = []

    def factory():
        sess = MagicMock(name="stream_session")
        sessions.append(sess)
        return sess

    with patch.object(voice, "_open_stream_session", factory):
        yield sessions


async def test_stream_session_is_closed_after_normal_stream(session_spy):
    await _drain(await _call(_payload(), _ctx()))
    assert len(session_spy) == 1
    session_spy[0].close.assert_called_once()


async def test_stream_session_is_closed_after_pipeline_exception(session_spy):
    send = AsyncMock(side_effect=RuntimeError("boom"))
    await _drain(await _call(_payload(), _ctx(), send=send))
    assert len(session_spy) == 1
    session_spy[0].close.assert_called_once()


async def test_stream_session_is_closed_after_abandoned_request(session_spy):
    gate = asyncio.Event()

    async def slow(*_a, **_k):
        await gate.wait()
        return _reply("late")

    resp = await _call(_payload(), _ctx(), send=AsyncMock(side_effect=slow))
    it = resp.body_iterator.__aiter__()
    await it.__anext__()  # role
    await it.__anext__()  # buffer word: pipeline task is running
    await it.aclose()  # client disconnects mid-pipeline
    gate.set()
    await asyncio.gather(*voice._background_turns)
    assert len(session_spy) == 1
    session_spy[0].close.assert_called_once()


async def test_no_session_opened_when_disconnected_or_replayed(session_spy):
    await _drain(await _call(_payload(), _ctx(), request=_request(disconnected=True)))
    ctx = _ctx()
    await _drain(await _call(_payload(), ctx))
    await _drain(await _call(_payload(), ctx))  # replayed from dedupe, no pipeline
    assert len(session_spy) == 1


async def test_request_db_is_not_used_by_the_pipeline(session_spy):
    send = AsyncMock(return_value=_reply("ok"))
    await _drain(await _call(_payload(), _ctx(), send=send))
    assert send.await_args.args[1] is session_spy[0]
