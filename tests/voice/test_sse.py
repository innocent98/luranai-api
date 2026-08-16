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
