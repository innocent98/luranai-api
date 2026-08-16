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

    if buffer_word:
        yield chunk({"content": BUFFER_WORD})

    for word in reply_text.split(" "):
        yield chunk({"content": word + " "})

    yield chunk({}, finish_reason="stop")
    yield "data: [DONE]\n\n"
