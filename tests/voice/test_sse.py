import json

from app.services.voice.sse import BUFFER_WORD, stream_reply


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
