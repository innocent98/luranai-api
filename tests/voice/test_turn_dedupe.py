"""turn_dedupe against the real Redis (no mocks), plus fail-open behaviour."""

from uuid import uuid4

from app.core.config import settings
from app.services.voice import turn_dedupe


async def test_claim_store_wait_release_roundtrip():
    key = turn_dedupe.turn_key(uuid4(), 3, "hello")
    assert await turn_dedupe.claim(key) is True
    assert await turn_dedupe.claim(key) is False  # second request sees the claim
    assert await turn_dedupe.wait_for_outcome(key, timeout=0.2) is None  # still pending
    await turn_dedupe.store(key, {"text": "hi", "tool_name": None})
    assert await turn_dedupe.wait_for_outcome(key, timeout=0.2) == {"text": "hi", "tool_name": None}
    await turn_dedupe.release(key)
    assert await turn_dedupe.claim(key) is True
    await turn_dedupe.release(key)


def test_key_differs_by_history_length_and_text():
    sid = uuid4()
    assert turn_dedupe.turn_key(sid, 2, "a") == turn_dedupe.turn_key(sid, 2, "a")
    assert turn_dedupe.turn_key(sid, 2, "a") != turn_dedupe.turn_key(sid, 4, "a")
    assert turn_dedupe.turn_key(sid, 2, "a") != turn_dedupe.turn_key(sid, 2, "b")


async def test_fails_open_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    assert await turn_dedupe.claim("k") is True
    assert await turn_dedupe.wait_for_outcome("k", timeout=0.1) is None
