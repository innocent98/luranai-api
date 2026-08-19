"""Unit tests for `_extract_voice_token` -- the parser that pulls our opaque
voice_token out of `messages[0].content` (the ElevenLabs Agent's system
prompt, after EL's own `{{voice_token}}` substitution).

Spike 1 (2026-08-19, 28 live requests captured against a real throwaway
ElevenLabs Agent -- see `_extract_voice_token`'s and
`ElevenLabsClient.mint_conversation_token`'s docstrings for the full mint ->
JWT -> prompt-substitution chain) proved the custom-LLM request body
contains EXACTLY `messages`, `model`, `max_tokens`, `stream`,
`stream_options`, `temperature`, `tools` -- no `elevenlabs_extra_body`, no
`dynamic_variables`, no `user`, no `conversation_id`, no `agent_id`. The
three carriers the OLD `_extract_voice_token` checked never existed on the
wire; every test that covered them (the old `test_custom_llm.py::_body`
helper, which built `{"elevenlabs_extra_body": {"voice_token": token}}`)
was proving nothing about a real ElevenLabs request. These tests exercise
the real carrier instead.

`_SUBSTITUTED_CONTENT` and `_UNSUBSTITUTED_CONTENT` and `_NO_MARKER_CONTENT`
below are literal `messages[0].content` strings copied verbatim from
Spike 1's `captures.jsonl` (lines 27, 22, and 4 respectively) -- not
hand-rolled fixtures guessing at the shape. The file itself is throwaway and
is NOT read at test runtime; these three constants are the copy.
"""

import secrets

from app.api.v1.endpoints.voice import _extract_voice_token

# A real voice_token, shaped exactly like `secrets.token_urlsafe(32)`
# (always 43 chars, URL-safe base64 alphabet: A-Za-z0-9_-, see
# `_extract_voice_token`'s docstring for why that shape is load-bearing).
# Generated fresh at import time rather than hardcoded, so this file can't
# collide with a real token some other test minted into shared Redis.
_REAL_TOKEN = secrets.token_urlsafe(32)
assert len(_REAL_TOKEN) == 43

# Spike 1, captures.jsonl line 27: a genuinely EL-substituted request. The
# probe agent's prompt only ever echoed a short placeholder (SPIKEPROBE123,
# 13 chars) -- never a real 43-char token -- so it proves substitution
# happened at all, but is itself too short to double as a positive case for
# THIS module's shape check (see test_probe_placeholder_is_too_short_to_match
# below). Swapping in a real-shaped token, in the exact same captured
# surrounding text (same prefix, same trailing newline-plus-16-spaces
# continuation), gives the real positive case without hand-transcribing a
# new ~1.8KB fixture.
_SUBSTITUTED_CONTENT = 'Task description: You are an AI agent. Your character definition is provided below, stick to it. You should not provide any personal information. You should also not provide any medical, legal, or financial advice. You should not provide any information that is false or misleading. You should not provide any information that is offensive or inappropriate. You should not provide any information that is harmful or dangerous. You should not provide any information that is confidential or proprietary. You should not provide any information that is copyrighted or trademarked. Do not format your text response with bullet points, bold or headers. Since your answers will be converted to audio, make sure to not use symbols like $, %, #, @, etc. or digits in your responses, if you need to use them write them out as words e.g. "three dollars", "hashtag", "one", "two", etc.". Unless specified differently in the character answer in around 3-4 sentences for most cases. If a user responds with \'...\' it means that they didn\'t respond or say anything, you should prompt them to speak, or if they don\'t respond for a while then ask if they\'re still there. You may also be supplied with an additional documentation knowledge base which may contain information that will help you to answer questions from the user. Your current language is: en=English \n                Agent character description: Session: SPIKEPROBE123\n                Everything following this section is the documentation knowledge base. You can use this information to answer questions from the user though it might not be needed. It is supplied as a series of documents, each in HTML or Markdown format.\n                No additional documentation provided\nThe current date and time is Wednesday, 18:15 19 August 2026 (Africa/Lagos)\n                '

# Spike 1, captures.jsonl line 22: negative control proving substitution is
# driven by the mint call's `participant_name`, not coincidence -- minted
# WITHOUT it, EL left the literal template text `{{voice_token}}`
# unsubstituted. An operator's agent could realistically end up in this
# state (e.g. our own mint code regressing to not pass `participant_name`
# -- exactly the bug `test_mint_passes_voice_token_as_participant_name` in
# test_token_mint.py guards against on the OTHER end of this same chain).
_UNSUBSTITUTED_CONTENT = 'Task description: You are an AI agent. Your character definition is provided below, stick to it. You should not provide any personal information. You should also not provide any medical, legal, or financial advice. You should not provide any information that is false or misleading. You should not provide any information that is offensive or inappropriate. You should not provide any information that is harmful or dangerous. You should not provide any information that is confidential or proprietary. You should not provide any information that is copyrighted or trademarked. Do not format your text response with bullet points, bold or headers. Since your answers will be converted to audio, make sure to not use symbols like $, %, #, @, etc. or digits in your responses, if you need to use them write them out as words e.g. "three dollars", "hashtag", "one", "two", etc.". Unless specified differently in the character answer in around 3-4 sentences for most cases. If a user responds with \'...\' it means that they didn\'t respond or say anything, you should prompt them to speak, or if they don\'t respond for a while then ask if they\'re still there. You may also be supplied with an additional documentation knowledge base which may contain information that will help you to answer questions from the user. Your current language is: en=English \n                Agent character description: Session: {{voice_token}}\n                Everything following this section is the documentation knowledge base. You can use this information to answer questions from the user though it might not be needed. It is supplied as a series of documents, each in HTML or Markdown format.\n                No additional documentation provided\nThe current date and time is Wednesday, 18:12 19 August 2026 (Africa/Lagos)\n                '

# Spike 1, captures.jsonl line 4: an agent whose character description
# never had the `Session: {{voice_token}}` template text at all -- the
# realistic "operator never configured it" case the brief calls out
# explicitly (the prompt is operator-configured and can be missing or
# malformed).
_NO_MARKER_CONTENT = 'Task description: You are an AI agent. Your character definition is provided below, stick to it. You should not provide any personal information. You should also not provide any medical, legal, or financial advice. You should not provide any information that is false or misleading. You should not provide any information that is offensive or inappropriate. You should not provide any information that is harmful or dangerous. You should not provide any information that is confidential or proprietary. You should not provide any information that is copyrighted or trademarked. Do not format your text response with bullet points, bold or headers. Since your answers will be converted to audio, make sure to not use symbols like $, %, #, @, etc. or digits in your responses, if you need to use them write them out as words e.g. "three dollars", "hashtag", "one", "two", etc.". Unless specified differently in the character answer in around 3-4 sentences for most cases. If a user responds with \'...\' it means that they didn\'t respond or say anything, you should prompt them to speak, or if they don\'t respond for a while then ask if they\'re still there. You may also be supplied with an additional documentation knowledge base which may contain information that will help you to answer questions from the user. Your current language is: en=English \n                Agent character description: \n                Everything following this section is the documentation knowledge base. You can use this information to answer questions from the user though it might not be needed. It is supplied as a series of documents, each in HTML or Markdown format.\n                No additional documentation provided\nThe current date and time is Wednesday, 15:11 19 August 2026 (Africa/Lagos)\n                '


def _payload(content: str) -> dict:
    return {"messages": [{"role": "system", "content": content}]}


def test_extracts_token_from_real_captured_shape():
    """The positive case: a real-shaped token substituted into the exact
    captured system-prompt text, mid-string (preceded by "Agent character
    description: " on the same line, not line-anchored) and followed by a
    newline plus 16 spaces of continued prompt -- not a simplified fixture
    that would pass even with naive line-anchored regex like `^Session:`."""
    content = _SUBSTITUTED_CONTENT.replace("SPIKEPROBE123", _REAL_TOKEN)
    assert _extract_voice_token(_payload(content)) == _REAL_TOKEN


def test_unsubstituted_template_placeholder_fails_closed():
    """A broken/unconfigured agent whose prompt never got `{{voice_token}}`
    substituted must 401 (empty string -> unresolvable token), never partial-
    match into something that resolves. A broken implementation that, say,
    matched greedily on non-whitespace after "Session:" would happily
    extract the literal string "{{voice_token}}" here and this test would
    catch that: VoiceSessionTokenStore.resolve() would never have that
    string as a valid binding, but a LOOSER implementation might strip the
    braces or otherwise coerce it into looking like a token."""
    assert _extract_voice_token(_payload(_UNSUBSTITUTED_CONTENT)) == ""


def test_no_session_marker_at_all_fails_closed():
    """An operator who never added the `Session: {{voice_token}}` template
    text to their Agent's character description at all -- real captured
    shape, zero `Session:` occurrences anywhere in the prompt."""
    assert _extract_voice_token(_payload(_NO_MARKER_CONTENT)) == ""


def test_probe_placeholder_is_too_short_to_match():
    r"""Documents WHY `_SUBSTITUTED_CONTENT` can't be used as-is for the
    positive case: Spike 1's probe value (SPIKEPROBE123, 13 chars) is
    shorter than a real voice_token (43 chars) and must NOT satisfy the
    shape check -- a parser that accepted arbitrary trailing text after
    "Session:\s*" (the exact failure mode the brief warns against) would
    wrongly extract "SPIKEPROBE123" here."""
    assert _extract_voice_token(_payload(_SUBSTITUTED_CONTENT)) == ""


def test_trailing_extra_characters_do_not_get_absorbed_into_the_token():
    """A real token immediately followed by MORE token-alphabet characters
    (not whitespace) before the next real boundary must not match -- proves
    the parser isn't simply "43 chars starting here" but requires an actual
    non-alphabet boundary after it too. Regression target: a regex missing
    the trailing negative lookahead would greedily accept the first 43 of a
    longer run, extracting a truncated, wrong token that VoiceSessionTokenStore
    would never have a binding for anyway -- but for the wrong reason (luck
    of the 401), not because the parser actually enforced the boundary."""
    too_long = _REAL_TOKEN + "EXTRA"
    content = f"Agent character description: Session: {too_long}\n                more prompt"
    assert _extract_voice_token(_payload(content)) == ""


def test_missing_messages_fails_closed():
    assert _extract_voice_token({}) == ""
    assert _extract_voice_token({"messages": []}) == ""


def test_non_dict_first_message_fails_closed():
    """A malformed body where `messages[0]` isn't even a dict must not crash
    extraction into a 500 -- mirrors the existing malformed-last-message
    guard in `_last_user_message`, applied to the first message instead."""
    assert _extract_voice_token({"messages": ["not a dict"]}) == ""


def test_non_string_content_fails_closed():
    assert _extract_voice_token({"messages": [{"role": "system", "content": None}]}) == ""
    assert _extract_voice_token({"messages": [{"role": "system", "content": 12345}]}) == ""
