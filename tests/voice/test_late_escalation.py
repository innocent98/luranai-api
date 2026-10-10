"""Late, non-keyword escalations must not be preceded by the buffer word: the
buffer is released by `turn_signal.generation_started()`, not by a timer."""

import asyncio
from unittest.mock import AsyncMock

from app.api.v1.endpoints import voice
from app.services.voice import turn_signal
from tests.voice.test_escalation_ux import (  # noqa: F401 - fixtures are autouse
    _cleanup,
    _no_dedupe,
    _call,
    _ctx,
    _deltas,
    _drain,
    _has_content,
    _payload,
    _reply,
    _tool_names,
)

BUFFER = {"content": "... "}


async def test_late_escalation_without_signal_has_no_content(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 2.5)

    async def late_escalation(*_a, **_k):
        await asyncio.sleep(0.8)  # e.g. tenant keyword / router decided late
        return _reply("x", escalate=True)

    frames = await _drain(
        await _call(
            _payload("this is outrageous, fix it now"),
            _ctx(),
            send=AsyncMock(side_effect=late_escalation),
        )
    )
    assert not _has_content(frames)
    d = _deltas(frames)
    assert d[0]["delta"] == {"role": "assistant"}
    assert _tool_names(frames) == [voice._ESCALATE_TOOL_NAME]
    assert d[-2]["finish_reason"] == "tool_calls"
    assert d[-1] == "DONE"


async def test_normal_turn_gets_buffer_when_signal_fires(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 2.5)
    loop = asyncio.get_running_loop()
    marks: dict[str, float] = {}

    async def normal(*_a, **_k):
        await asyncio.sleep(0.4)
        turn_signal.generation_started()
        marks["signal"] = loop.time()
        await asyncio.sleep(0.8)
        return _reply("an answer")

    resp = await _call(_payload("what are your hours"), _ctx(), send=AsyncMock(side_effect=normal))
    it = resp.body_iterator.__aiter__()
    t0 = loop.time()
    await it.__anext__()  # role
    buffer = await it.__anext__()
    assert loop.time() - t0 < 1.0  # released by the signal, long before the reply
    assert "... " in buffer
    rest = await _drain_iter(it)
    spoken = "".join(
        c["delta"]["content"] for c in _deltas(rest)[:-1] if "content" in c["delta"]
    )
    assert spoken == "an answer "


async def _drain_iter(it):
    return [c async for c in it]


async def test_buffer_at_cap_when_signal_never_fires(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 0.2)
    loop = asyncio.get_running_loop()

    async def slow(*_a, **_k):
        await asyncio.sleep(0.6)
        return _reply("late answer")

    resp = await _call(_payload("what are your hours"), _ctx(), send=AsyncMock(side_effect=slow))
    it = resp.body_iterator.__aiter__()
    await it.__anext__()
    t0 = loop.time()
    buffer = await it.__anext__()
    elapsed = loop.time() - t0
    assert "... " in buffer
    assert 0.15 < elapsed < 0.5


def test_generation_started_is_noop_without_binding():
    turn_signal.generation_started()  # must not raise, no event bound


async def test_contextvar_isolated_between_concurrent_turns(monkeypatch):
    monkeypatch.setattr(voice, "_BUFFER_MAX_WAIT_SECONDS", 2.5)
    gate = asyncio.Event()

    async def dispatch(request, *_a, **_k):
        if request.message == "what are your hours":
            turn_signal.generation_started()
            await gate.wait()
            return _reply("a")
        await gate.wait()
        return _reply("b", escalate=True)

    send = AsyncMock(side_effect=dispatch)
    r1 = await _call(_payload("what are your hours"), _ctx(), send=send)
    r2 = await _call(_payload("odd request"), _ctx(), send=send)
    i1, i2 = r1.body_iterator.__aiter__(), r2.body_iterator.__aiter__()
    await i1.__anext__()
    await i2.__anext__()
    # Turn 1 signalled -> buffer arrives while its pipeline is blocked.
    assert "... " in await asyncio.wait_for(i1.__anext__(), 1.0)
    # Turn 2 must NOT have been released by turn 1's signal.
    nxt = asyncio.ensure_future(i2.__anext__())
    await asyncio.sleep(0.3)
    assert not nxt.done()
    gate.set()
    frames2 = [await nxt, *await _drain_iter(i2)]
    assert not _has_content(frames2)
    assert _tool_names(frames2) == [voice._ESCALATE_TOOL_NAME]
    await _drain_iter(i1)
