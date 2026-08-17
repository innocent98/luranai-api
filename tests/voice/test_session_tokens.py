import asyncio
import json
import time
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services.voice.metering import VoiceMeter
from app.services.voice.session_tokens import VoiceSessionTokenStore


def _key(token: str) -> str:
    return f"voice:session_token:{token}"


def _tick_key(token: str) -> str:
    return f"voice:session_tick:{token}"


async def _set_ticks(store, token, *, minted_at=None, last_metered_at=None) -> None:
    """Overwrite a minted token's `minted_at` (in its JSON payload) and/or
    its metering tick (its own separate Redis key), to simulate "a call
    that started X seconds ago and/or was last metered Y seconds ago"
    without sleeping in real time."""
    redis = await store._get_redis()
    if minted_at is not None:
        key = _key(token)
        raw = await redis.get(key)
        data = json.loads(raw)
        data["minted_at"] = minted_at
        ttl = await redis.ttl(key)
        await redis.set(key, json.dumps(data), ex=ttl if ttl > 0 else 600)
    if last_metered_at is not None:
        tick_key = _tick_key(token)
        ttl = await redis.ttl(tick_key)
        await redis.set(tick_key, last_metered_at, ex=ttl if ttl > 0 else 600)


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
async def test_resolve_does_not_resurrect_a_key_deleted_mid_flight():
    """Task 11 review round 3, finding #1: a real regression introduced by
    round 2's metering design. Round 2 slid the TTL via a plain `SET ...`
    (needed at the time to also persist a `last_metered_at` field embedded
    in the payload) instead of the original `EXPIRE`. A plain SET
    unconditionally *recreates* a missing key -- so if
    `/voice/session/end`'s `resolve_and_revoke` (GETDEL) or a
    terminal-rejection `revoke()` deletes this token's binding between this
    resolve()'s GET and its own write, the delete gets silently undone with
    a fresh full TTL. Two concrete consequences that regression reopened:
    the call keeps answering (and metering) after its concurrency slot was
    released, and a caller can loop on exactly the unfixable rejection
    revoke() exists to stop.

    Round 3 moved the metering tick to its own key, so resolve()'s
    TTL-slide went back to a bare `EXPIRE` -- a documented Redis no-op on a
    missing key that can never recreate one. This test proves that
    directly: simulates the race by deleting the key immediately after
    resolve()'s own GET returns (via a patched Redis client method, not by
    asserting resolve()'s return value, which wouldn't distinguish "still
    resolved because of a legitimate in-flight read" from "resurrected a
    key that should be gone") and asserts the key stays deleted.
    """
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)
    key = _key(token)
    redis = await store._get_redis()
    real_get = redis.get

    async def _get_then_delete_concurrently(k, *args, **kwargs):
        raw = await real_get(k, *args, **kwargs)
        if k == key:
            await redis.delete(k)  # simulate a racing /session/end (GETDEL) or revoke()
        return raw

    with patch.object(redis, "get", side_effect=_get_then_delete_concurrently):
        await store.resolve(token)

    assert await redis.exists(key) == 0, "resolve() must not resurrect a key deleted mid-flight"


@pytest.mark.asyncio
async def test_mint_records_minted_at_and_resolve_exposes_it():
    """`minted_at` is the server's own record of when the token was minted --
    diagnostic only; it must never be taken from anything a client could
    later supply. Billing itself does NOT derive duration from this field
    (see the per-turn metering tests below) since the gap between mint and
    the first real turn includes WebRTC connect + mic-permission lag, not
    just conversation time."""
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
    must stay pinned to the original mint -- it is a diagnostic field only
    (see its docstring on `VoiceSessionContext`); metering itself is driven
    by the separate `last_metered_at` tick, not by `minted_at`. If a later
    resolve() quietly bumped `minted_at` forward, it would misrepresent how
    long ago the token was actually minted."""
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


# ---------------------------------------------------------------------------
# Per-turn metering (Task 11 review round 2): resolve() is proof-of-life on
# every conversational turn, so it -- not /voice/session/end -- is what has
# to bill the call, or a client that never calls that route (crash, tab
# close, network loss) meters nothing at all and the minute cap enforces
# nothing. The tick these tests manipulate lives in its own Redis key (see
# `_tick_key`), not inside the token's JSON payload -- moved there in review
# round 3 to make bill-and-advance atomic (see `_METER_TURN_SCRIPT` in
# session_tokens.py) and to stop the TTL-slide from needing to rewrite (and
# thus risk resurrecting) the token's own payload key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_meters_elapsed_time_since_last_tick():
    """A single resolve() (one conversational turn) must bill the wall-clock
    time since the token's last metering tick -- proving the minute cap has
    a real, server-driven input instead of depending on the widget ever
    calling `/voice/session/end`."""
    store = VoiceSessionTokenStore()
    tenant_id = uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=uuid4(), text_out=False)
    await _set_ticks(store, token, last_metered_at=time.time() - 30)

    await store.resolve(token)

    assert await VoiceMeter().get_monthly_minutes(tenant_id) == pytest.approx(0.5, abs=0.05)


@pytest.mark.asyncio
async def test_resolve_advances_the_tick_so_a_second_turn_only_bills_its_own_delta():
    """Two turns 15s apart must each bill ~15s, not double-bill the first
    turn's elapsed time again on the second call -- proving the tick
    genuinely advances rather than staying pinned to the original mint."""
    store = VoiceSessionTokenStore()
    tenant_id = uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=uuid4(), text_out=False)
    await _set_ticks(store, token, last_metered_at=time.time() - 15)

    await store.resolve(token)
    first_total = await VoiceMeter().get_monthly_minutes(tenant_id)
    assert first_total == pytest.approx(0.25, abs=0.05)

    # Immediately calling resolve() again must add only the (tiny) elapsed
    # time since the FIRST resolve(), not another 15s.
    await store.resolve(token)
    second_total = await VoiceMeter().get_monthly_minutes(tenant_id)
    assert second_total - first_total < 0.05


@pytest.mark.asyncio
async def test_resolve_and_revoke_meters_the_final_partial_delta_on_top_of_turn_billing():
    """Ending a call must bill the trailing time since the last turn, ON TOP
    OF whatever per-turn metering already happened during the call -- not
    replace it, and not double-count it."""
    store = VoiceSessionTokenStore()
    tenant_id = uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=uuid4(), text_out=False)

    await _set_ticks(store, token, last_metered_at=time.time() - 20)
    await store.resolve(token)  # bills ~20s, advances the tick to "now"

    await _set_ticks(store, token, last_metered_at=time.time() - 10)  # 10 more seconds pass
    ctx = await store.resolve_and_revoke(token)  # bills the trailing ~10s

    assert ctx is not None
    total = await VoiceMeter().get_monthly_minutes(tenant_id)
    assert total == pytest.approx(0.5, abs=0.06)  # ~20s + ~10s = ~30s = 0.5 min


@pytest.mark.asyncio
async def test_concurrent_resolves_do_not_double_bill_the_same_delta():
    """Task 11 review round 3, finding #2: two overlapping resolve() calls
    on the same token are a real scenario (voice.py explicitly contemplates
    two concurrent turns racing for the same rate-limit slot, and the
    escalate-to-human tool resolves independently of an in-flight turn). A
    Python-level read-tick/meter/write-tick sequence would let both calls
    read the same stale tick and both bill the same elapsed window,
    over-billing the tenant. `_meter_turn`'s Lua script must close this by
    running the whole read-meter-advance sequence as one atomic Redis
    operation. Fires two concurrent resolve() calls via `asyncio.gather()`
    on a token whose tick is 30s stale and asserts the TOTAL billed is
    still ~30s (one delta), not ~60s (double-billed)."""
    store = VoiceSessionTokenStore()
    tenant_id = uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=uuid4(), text_out=False)
    await _set_ticks(store, token, last_metered_at=time.time() - 30)

    await asyncio.gather(store.resolve(token), store.resolve(token))

    total = await VoiceMeter().get_monthly_minutes(tenant_id)
    assert total == pytest.approx(0.5, abs=0.05), (
        f"expected ~30s billed once (~0.5 min), got {total} min -- "
        "looks like the same delta was billed twice"
    )


@pytest.mark.asyncio
async def test_metering_failure_does_not_advance_the_tick_so_the_next_success_catches_up():
    """If the Lua EVAL behind `_meter_turn` fails, the elapsed delta must
    not be silently lost -- the tick must stay put so the NEXT successful
    resolve() bills the full, uninterrupted elapsed time instead of only the
    gap since the failed attempt. Patches the real Redis client's `eval`
    method (the actual integration point `_meter_turn` calls) rather than
    `VoiceMeter.add_minutes`, which the atomic script bypasses entirely --
    the increment happens inside the Lua script itself now."""
    store = VoiceSessionTokenStore()
    tenant_id = uuid4()
    token = await store.mint(tenant_id=tenant_id, luran_session_id=uuid4(), text_out=False)
    await _set_ticks(store, token, last_metered_at=time.time() - 30)

    redis = await store._get_redis()

    async def _failing_eval(*args, **kwargs):
        raise RuntimeError("redis boom")

    with patch.object(redis, "eval", side_effect=_failing_eval):
        ctx = await store.resolve(token)

    assert ctx is not None, "a metering failure must not break resolve()'s own contract"
    assert await VoiceMeter().get_monthly_minutes(tenant_id) == 0

    # Redis "recovers" (patch removed); the NEXT resolve() must bill the
    # full ~30s the failed attempt should have billed, not just the tiny
    # gap since that attempt -- proving the lost delta wasn't dropped.
    await store.resolve(token)
    assert await VoiceMeter().get_monthly_minutes(tenant_id) == pytest.approx(0.5, abs=0.05)


@pytest.mark.asyncio
async def test_resolve_tolerates_a_payload_minted_before_minted_at_existed():
    """Migration-compat: a payload written by a pre-deploy version of mint()
    has neither `minted_at` in its payload nor a separate metering-tick key
    at all. resolve() must not KeyError/crash on it -- a token minted
    moments before a rollout must still resolve after deploy, not 401 an
    in-flight call -- and must not treat the missing tick as "started at
    the Unix epoch": defaulting to 0.0 would bill roughly 30 million
    minutes for the gap since 1970 on the very next turn."""
    store = VoiceSessionTokenStore()
    tenant_id, session_id = uuid4(), uuid4()
    # Unique per run (not a fixed literal): a hardcoded token string leaves a
    # `voice:session_tick:<token>` key behind with a 600s TTL (`_meter_turn`
    # always sets one) that outlives this test's own 60s main-key TTL below,
    # so a second run within that 600s window reads a stale tick from the
    # PREVIOUS run and bills the gap between runs instead of genuinely
    # exercising "no tick key at all". This bit the test itself in practice:
    # a standalone `pytest ...::test_...` run followed immediately by a
    # full-suite run failed on `assert 1.0166... < 1.0` for exactly this
    # reason.
    token = f"legacy-token-without-minted-at-{uuid4().hex}"
    key = _key(token)
    redis = await store._get_redis()
    await redis.set(
        key,
        json.dumps(
            {
                "tenant_id": str(tenant_id),
                "luran_session_id": str(session_id),
                "text_out": False,
                # no "minted_at" -- the pre-migration shape. Also, deliberately,
                # no corresponding tick key ever set (a genuinely pre-metering
                # token never had one).
            }
        ),
        ex=60,
    )

    ctx = await store.resolve(token)

    assert ctx is not None
    assert ctx.tenant_id == tenant_id
    assert ctx.minted_at > 0  # fell back to "now", not a KeyError
    assert await VoiceMeter().get_monthly_minutes(tenant_id) < 1.0
