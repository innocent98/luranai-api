"""Unit tests for `_describe_voice_token_shape` -- the shape-only diagnostic
logged when a custom-LLM call carries an unresolvable voice token.

Staging (2026-10-10) showed every first custom-LLM call 401ing with
"unresolvable voice token", but that single log line can't tell apart
"no marker", "EL didn't substitute {{voice_token}}", "ambiguous marker" and
"token extracted but unknown to Redis". The diagnostic separates those cases
WITHOUT ever emitting the token itself or any prompt text.
"""

import secrets

from app.api.v1.endpoints.voice import _describe_voice_token_shape
from tests.voice.test_voice_token_extraction import (
    _NO_MARKER_CONTENT,
    _UNSUBSTITUTED_CONTENT,
    _payload,
)

_TOKEN = secrets.token_urlsafe(32)


def _with_token(token: str) -> str:
    return _UNSUBSTITUTED_CONTENT.replace("{{voice_token}}", token)


def test_unsubstituted_template_is_reported():
    shape = _describe_voice_token_shape(_payload(_UNSUBSTITUTED_CONTENT))
    assert shape["has_session_marker"] is True
    assert shape["has_unsubstituted_template"] is True
    assert shape["token_candidates"] == 0
    assert shape["extracted"] is False


def test_missing_marker_is_reported():
    shape = _describe_voice_token_shape(_payload(_NO_MARKER_CONTENT))
    assert shape["has_session_marker"] is False
    assert shape["token_candidates"] == 0
    assert shape["extracted"] is False


def test_real_token_is_reported_as_extracted_without_leaking_it():
    shape = _describe_voice_token_shape(_payload(_with_token(_TOKEN)))
    assert shape["token_candidates"] == 1
    assert shape["extracted"] is True
    # The diagnostic must never carry the token, any fragment of it, or prompt text.
    rendered = repr(shape)
    assert _TOKEN not in rendered
    assert _TOKEN[:8] not in rendered
    assert "Agent character description" not in rendered


def test_wrong_length_value_after_marker_is_classified():
    shape = _describe_voice_token_shape(_payload(_with_token("SPIKEPROBE123")))
    assert shape["has_session_marker"] is True
    assert shape["token_candidates"] == 0
    assert shape["marker_value_len"] == len("SPIKEPROBE123")


def test_ambiguous_markers_are_counted():
    content = _with_token(_TOKEN) + "\nSession: " + secrets.token_urlsafe(32)
    shape = _describe_voice_token_shape(_payload(content))
    assert shape["token_candidates"] == 2
    assert shape["extracted"] is False


def test_malformed_payloads_do_not_raise():
    assert _describe_voice_token_shape({})["messages"] == 0
    assert _describe_voice_token_shape({"messages": ["x"]})["first_message_type"] == "str"
    shape = _describe_voice_token_shape({"messages": [{"role": "system", "content": None}]})
    assert shape["first_role"] == "system"
    assert shape["content_len"] is None
