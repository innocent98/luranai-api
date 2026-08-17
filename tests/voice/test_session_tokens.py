import asyncio
import json
import time
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.voice.session_tokens import VoiceSessionTokenStore


@pytest.mark.asyncio
async def test_mint_produces_unguessable_url_safe_token():
    """Pins the token shape to what secrets.token_urlsafe(32) actually produces
    (43 URL-safe base64 chars, no padding) so a regression to uuid4() (36 chars,
    hyphenated) or random/predictable generation fails this test."""
    store = VoiceSessionTokenStore()
    token_a = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=True)
    token_b = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=True)

    assert len(token_a) == 43
    assert all(c.isalnum() or c in "-_" for c in token_a)
    assert token_a != token_b


@pytest.mark.asyncio
async def test_mint_then_resolve_round_trips_context():
    store = VoiceSessionTokenStore()
    tenant_id, session_id = uuid4(), uuid4()

    token = await store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=True)

    ctx = await store.resolve(token)
    assert ctx is not None
    assert ctx.tenant_id == tenant_id
    assert ctx.luran_session_id == session_id
    assert ctx.text_out is True

    redis = await store._get_redis()
    ttl = await redis.ttl(f"voice:session_token:{token}")
    assert 0 < ttl <= settings.VOICE_SESSION_TOKEN_TTL_SECONDS


@pytest.mark.asyncio
async def test_mint_then_resolve_round_trips_text_out_false():
    """text_out=True is covered by the round-trip test above; this pins the
    False case so a hardcoded/inverted flag doesn't slip through unnoticed."""
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)

    ctx = await store.resolve(token)
    assert ctx is not None
    assert ctx.text_out is False


@pytest.mark.asyncio
async def test_resolve_unknown_token_returns_none():
    store = VoiceSessionTokenStore()
    assert await store.resolve("not-a-real-token") is None


@pytest.mark.asyncio
async def test_revoke_makes_token_unresolvable():
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)
    assert await store.resolve(token) is not None

    await store.revoke(token)
    assert await store.resolve(token) is None


@pytest.mark.asyncio
async def test_resolve_slides_ttl_forward_so_a_long_call_never_goes_silent():
    """A voice call can run turns for longer than the mint TTL (600s) as long
    as they keep arriving. If resolve() didn't slide the TTL forward, the
    first turn after the original mint's countdown ran out would 401 mid-call
    — and ElevenLabs cannot render a 401 as audio, so that becomes silence on
    a predictable schedule. Simulates "deep into a long call" by manually
    shrinking the key's TTL, then asserts resolve() restores the full window
    rather than leaving the shrunk countdown running.
    """
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)

    redis = await store._get_redis()
    key = f"voice:session_token:{token}"
    await redis.expire(key, 5)

    ctx = await store.resolve(token)
    assert ctx is not None

    ttl_after_resolve = await redis.ttl(key)
    assert ttl_after_resolve > 5, (
        "resolve() must slide the TTL back to the full window, not leave the "
        "shrunk countdown running"
    )


@pytest.mark.asyncio
async def test_resolve_does_not_extend_ttl_for_a_corrupt_payload():
    """A corrupt stored payload (fails json.loads or UUID()) must not have its
    TTL renewed. If the `expire()` call ran before parsing, every failing
    turn on a broken token would revive its lifetime forever — the same
    unbounded-unfixable-loop shape finding #3 (round 2) closed for policy
    rejections, just one layer lower: the caller would get `_FALLBACK_REPLY`
    on an infinite loop instead of the binding aging out on its original
    countdown."""
    store = VoiceSessionTokenStore()
    redis = await store._get_redis()
    token = "corrupt-token"
    key = f"voice:session_token:{token}"
    await redis.set(key, "not valid json", ex=5)

    with pytest.raises(json.JSONDecodeError):
        await store.resolve(token)

    ttl_after = await redis.ttl(key)
    assert 0 < ttl_after <= 5, "TTL must not be extended for a payload that failed to parse"


@pytest.mark.asyncio
async def test_mint_records_minted_at_and_resolve_exposes_it():
    """`minted_at` is the server's own record of when the call started -- the
    only input `/voice/session/end` trusts for billing. It must be captured
    at mint time, not derived from anything a client could later supply."""
    store = VoiceSessionTokenStore()
    before = time.time()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)
    after = time.time()

    ctx = await store.resolve(token)
    assert ctx is not None
    assert before <= ctx.minted_at <= after


@pytest.mark.asyncio
async def test_resolve_does_not_move_minted_at_forward():
    """Unlike the token's TTL (which slides on every resolve), `minted_at`
    must stay pinned to the original mint -- it is the start-of-call marker
    that elapsed-duration metering is computed from. If a later resolve()
    quietly bumped it forward, a long call would always look like it lasted
    zero minutes."""
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)

    first = await store.resolve(token)
    await asyncio.sleep(0.05)
    second = await store.resolve(token)

    assert first is not None and second is not None
    assert first.minted_at == second.minted_at


@pytest.mark.asyncio
async def test_resolve_and_revoke_returns_context_then_deletes_binding():
    store = VoiceSessionTokenStore()
    tenant_id, session_id = uuid4(), uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=True)

    ctx = await store.resolve_and_revoke(token)
    assert ctx is not None
    assert ctx.tenant_id == tenant_id
    assert ctx.luran_session_id == session_id

    assert await store.resolve(token) is None
    assert await store.resolve_and_revoke(token) is None


@pytest.mark.asyncio
async def test_resolve_and_revoke_unknown_token_returns_none():
    store = VoiceSessionTokenStore()
    assert await store.resolve_and_revoke("not-a-real-token") is None


@pytest.mark.asyncio
async def test_resolve_and_revoke_is_atomic_under_concurrent_duplicate_calls():
    """The whole reason `resolve_and_revoke` exists instead of `resolve()`
    followed by `revoke()`: two racing calls for the same token (a
    double-tap hangup, a client retry racing the original request) must
    never both see the payload. GETDEL is a single Redis round trip, so
    exactly one of two concurrent callers gets the context back and the
    other gets None -- proving the idempotency guarantee `/voice/session/end`
    depends on actually holds under a real race, not just sequential calls.
    """
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)

    results = await asyncio.gather(
        store.resolve_and_revoke(token),
        store.resolve_and_revoke(token),
    )

    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, "exactly one of two racing callers must get the context"
