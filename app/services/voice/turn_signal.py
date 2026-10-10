"""Per-turn "generation is about to start" signal, voice only.

The voice stream wants to speak a filler word ("... ") to cover LLM latency,
but only on turns that really are going to generate an answer. Escalations and
other early returns in the chat pipeline are decided BEFORE generation and may
take longer than any fixed grace period, and filler content ahead of a
`tool_calls` frame stops ElevenLabs invoking the client tool.

The text pipeline calls `generation_started()` immediately before it calls the
LLM. When voice has bound an event for the current task context, that event is
set; otherwise (text chat, jobs, tests) the call is a no-op.
"""

import asyncio
from contextvars import ContextVar, Token

_turn_started: ContextVar[asyncio.Event | None] = ContextVar("voice_turn_started", default=None)


def bind(event: asyncio.Event) -> Token:
    """Bind `event` to the current context. Call it INSIDE the task that runs
    the pipeline so concurrent turns (separate tasks, separate contexts) never
    share an event."""
    return _turn_started.set(event)


def generation_started() -> None:
    event = _turn_started.get()
    if event is not None:
        event.set()
