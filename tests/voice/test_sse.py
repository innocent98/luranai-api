import json

from app.services.voice.sse import BUFFER_WORD, stream_reply, stream_tool_call


def _frames(chunks):
    """Parse SSE strings into the JSON payloads, dropping the [DONE] sentinel."""
    out = []
    for chunk in chunks:
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        body = chunk[len("data: ") : -2]
        if body == "[DONE]":
            continue
        out.append(json.loads(body))
    return out


def test_buffer_word_is_first_content_and_keeps_trailing_space():
    chunks = list(stream_reply("Hello there friend", model="m", buffer_word=True))
    payloads = _frames(chunks)
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    first_content = next(c for c in contents if c)
    assert first_content == BUFFER_WORD
    assert BUFFER_WORD.endswith(" "), "ElevenLabs needs the trailing space for prosody"


def test_stream_ends_with_done_sentinel():
    chunks = list(stream_reply("hi", model="m"))
    assert chunks[-1] == "data: [DONE]\n\n"


def test_full_reply_text_is_preserved():
    reply = "Your meeting is booked for Tuesday at three."
    chunks = list(stream_reply(reply, model="m", buffer_word=False))
    text = "".join(p["choices"][0]["delta"].get("content") or "" for p in _frames(chunks))
    assert text.strip() == reply


def test_first_frame_announces_assistant_role():
    payloads = _frames(list(stream_reply("hi", model="m")))
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_finish_reason_stop_present_exactly_once():
    payloads = _frames(list(stream_reply("hi", model="m")))
    stops = [p for p in payloads if p["choices"][0]["finish_reason"] == "stop"]
    assert len(stops) == 1


def test_empty_reply_with_buffer_word_yields_buffer_only():
    """Empty reply with buffer_word=True should emit buffer, no word frames."""
    chunks = list(stream_reply("", model="m", buffer_word=True))
    payloads = _frames(chunks)
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    # Should have buffer word but no word frames
    assert BUFFER_WORD in contents, f"Expected buffer word in {contents}"
    # No word frames (only buffer)
    word_contents = [c for c in contents if c and c != BUFFER_WORD]
    assert word_contents == [], f"Expected no word frames for empty reply, got {word_contents}"


def test_empty_reply_without_buffer_word_yields_no_content():
    """Empty reply with buffer_word=False should emit no content frames."""
    chunks = list(stream_reply("", model="m", buffer_word=False))
    payloads = _frames(chunks)
    contents = [
        p["choices"][0]["delta"].get("content")
        for p in payloads
        if "content" in p["choices"][0]["delta"]
    ]
    assert (
        contents == []
    ), f"Expected no content frames for empty reply with buffer_word=False, got {contents}"


def test_whitespace_only_reply_with_buffer_word_yields_buffer_only():
    """Whitespace-only reply with buffer_word=True should emit buffer, no word frames."""
    chunks = list(stream_reply("   ", model="m", buffer_word=True))
    payloads = _frames(chunks)
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    # Should have buffer word but no word frames
    assert BUFFER_WORD in contents, f"Expected buffer word in {contents}"
    # No word frames (only buffer)
    word_contents = [c for c in contents if c and c != BUFFER_WORD]
    assert (
        word_contents == []
    ), f"Expected no word frames for whitespace-only reply, got {word_contents}"


def test_completion_id_and_created_identical_across_frames():
    """Every frame in one stream must share the same completion_id and created timestamp."""
    chunks = list(stream_reply("hello world", model="m"))
    payloads = _frames(chunks)
    assert len(payloads) > 0, "Expected at least one payload"
    first_id = payloads[0]["id"]
    first_created = payloads[0]["created"]
    for payload in payloads:
        assert payload["id"] == first_id, f"completion_id mismatch: {payload['id']} != {first_id}"
        assert (
            payload["created"] == first_created
        ), f"created mismatch: {payload['created']} != {first_created}"


# --- stream_tool_call ---------------------------------------------------
# ElevenLabs only invokes a client tool (e.g. escalate_to_human) when it
# sees an OpenAI tool_calls delta from us -- plain text, however phrased,
# never triggers it (confirmed live: a caller saying "I need a human" while
# `voice_custom_llm` only ever emitted text left the tool dead). These tests
# pin the exact shape EL's OpenAI-compatible client needs to recognize a
# tool call, not just "some JSON came back".


def test_tool_call_announces_assistant_role_first():
    """A broken implementation that starts straight with the tool_calls
    delta (skipping the role announcement `stream_reply` always leads with)
    would pass a looser assertion but not this one."""
    payloads = _frames(list(stream_tool_call(tool_name="escalate_to_human", model="m")))
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}


def test_tool_call_delta_carries_function_name_and_call_id():
    payloads = _frames(list(stream_tool_call(tool_name="escalate_to_human", model="m")))
    tool_call_payloads = [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]]
    assert len(tool_call_payloads) == 1, "expected exactly one tool_calls delta frame"
    call = tool_call_payloads[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0
    assert call["type"] == "function"
    assert call["function"]["name"] == "escalate_to_human"
    assert call["id"].startswith("call_")
    # arguments must be a JSON-encoded STRING (OpenAI shape), not a bare
    # dict -- a broken implementation that passed `{}` straight through
    # would fail this.
    assert isinstance(call["function"]["arguments"], str)
    json.loads(call["function"]["arguments"])  # must be valid JSON


def test_tool_call_finish_reason_is_tool_calls_exactly_once():
    """The finish_reason MUST be literally "tool_calls", never "stop" --
    EL's OpenAI-compatible client branches on this exact string to decide
    whether to invoke the client tool at all. A broken implementation that
    reused `stream_reply`'s "stop" finish reason would pass a test that only
    checked "some finish_reason is present"."""
    payloads = _frames(list(stream_tool_call(tool_name="escalate_to_human", model="m")))
    finishes = [p for p in payloads if p["choices"][0]["finish_reason"] is not None]
    assert len(finishes) == 1
    assert finishes[0]["choices"][0]["finish_reason"] == "tool_calls"


def test_tool_call_stream_ends_with_done_sentinel():
    chunks = list(stream_tool_call(tool_name="escalate_to_human", model="m"))
    assert chunks[-1] == "data: [DONE]\n\n"


def test_tool_call_emits_no_spoken_content():
    """A tool-call turn is silent -- there is no text for the caller to
    hear while EL hands off to the client tool. A broken implementation
    that accidentally carried over `stream_reply`'s buffer-word framing
    would fail this."""
    payloads = _frames(list(stream_tool_call(tool_name="escalate_to_human", model="m")))
    contents = [
        p["choices"][0]["delta"].get("content")
        for p in payloads
        if "content" in p["choices"][0]["delta"]
    ]
    assert contents == []


def test_tool_call_completion_id_and_created_identical_across_frames():
    payloads = _frames(list(stream_tool_call(tool_name="escalate_to_human", model="m")))
    first_id = payloads[0]["id"]
    first_created = payloads[0]["created"]
    for payload in payloads:
        assert payload["id"] == first_id
        assert payload["created"] == first_created


def test_tool_call_uses_the_given_tool_name():
    """Not hardcoded to escalate_to_human -- proves the parameter is
    actually threaded through rather than a hardcoded literal in the
    function body."""
    payloads = _frames(list(stream_tool_call(tool_name="some_other_tool", model="m")))
    tool_call_payloads = [p for p in payloads if "tool_calls" in p["choices"][0]["delta"]]
    assert tool_call_payloads[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == (
        "some_other_tool"
    )
