"""OpenAI-compatible SSE framing for the ElevenLabs custom-LLM endpoint.

Our pipeline does not stream (see plan "Spec Corrections" #1): we get a finished
reply and re-emit it as chunks. The buffer word is therefore load-bearing — it is
the only thing covering pipeline latency before audio starts.
"""

import json
import time
import uuid
from collections.abc import Iterator

# Ellipsis + TRAILING SPACE. The trailing space is required by ElevenLabs so TTS
# has prosody to speak while the rest of the completion arrives (spec §3).
BUFFER_WORD = "... "


def openai_chunk(
    *,
    completion_id: str,
    model: str,
    created: int,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def stream_reply(
    reply_text: str,
    *,
    model: str,
    buffer_word: bool = True,
) -> Iterator[str]:
    """Yield the reply as OpenAI SSE frames, buffer word first."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        return openai_chunk(
            completion_id=completion_id,
            model=model,
            created=created,
            delta=delta,
            finish_reason=finish_reason,
        )

    yield chunk({"role": "assistant"})

    # Buffer word depends only on the buffer_word parameter.
    # It prevents dead air for empty replies (spec §3).
    if buffer_word:
        yield chunk({"content": BUFFER_WORD})

    # Only emit word frames if reply has non-whitespace text.
    if reply_text.strip():
        for word in reply_text.split(" "):
            yield chunk({"content": word + " "})

    yield chunk({}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def stream_tool_call(
    tool_name: str,
    *,
    model: str,
    arguments: str = "{}",
) -> Iterator[str]:
    """Yield an OpenAI tool_calls delta as SSE frames, instead of text.

    ElevenLabs only invokes a client tool (e.g. `escalate_to_human`) when it
    sees THIS shape from us -- plain spoken text, however phrased, never
    triggers it. `finish_reason` must be the literal string `"tool_calls"`,
    not `"stop"` (see `stream_reply`'s finish frame) -- EL's OpenAI-
    compatible client branches on that exact value.

    Unlike `stream_reply`, there is no buffer word and no content frame at
    all: a tool-call turn is silent by design (the caller hears nothing
    while EL hands off to the client tool), so nothing here should be
    mistaken for something needing prosody cover.

    `arguments` is a JSON-encoded STRING (OpenAI's wire shape for
    `function.arguments`), not a bare dict -- our only tool today
    (`escalate_to_human`) takes no parameters, so the default `"{}"` covers
    it, but the shape must stay a string if a future tool needs real args.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    call_id = f"call_{uuid.uuid4().hex[:24]}"

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        return openai_chunk(
            completion_id=completion_id,
            model=model,
            created=created,
            delta=delta,
            finish_reason=finish_reason,
        )

    yield chunk({"role": "assistant"})
    yield chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": arguments},
                }
            ]
        }
    )
    yield chunk({}, finish_reason="tool_calls")
    yield "data: [DONE]\n\n"
