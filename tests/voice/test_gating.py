import asyncio
import time
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services.voice.gating import VoiceDenialReason, VoiceGate


@pytest.mark.asyncio
async def test_flag_disabled_denies_first(db_session, voice_tenant_factory):
    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=0)
    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    # Both flag and cap would fail; flag must win — it is checked first.
    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


@pytest.mark.asyncio
async def test_minute_cap_reached_denies(db_session, voice_tenant_factory, voice_usage_setter):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=10)
    voice_usage_setter(tenant.id, minutes=10)
    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    assert result.reason == VoiceDenialReason.MINUTE_CAP_REACHED


@pytest.mark.asyncio
async def test_concurrency_ceiling_denies(db_session, voice_tenant_factory, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", 1)
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)

    await gate.acquire_slot(tenant.id, "tok-1")
    result = await gate.check(tenant.id)
    assert result.reason == VoiceDenialReason.CONCURRENCY_LIMIT

    await gate.release_slot(tenant.id, "tok-1")
    assert (await gate.check(tenant.id)).allowed is True


@pytest.mark.asyncio
async def test_unlimited_minutes_allowed(db_session, voice_tenant_factory):
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)
    assert (await gate.check(tenant.id)).allowed is True


@pytest.mark.asyncio
async def test_usage_below_finite_cap_allowed(db_session, voice_tenant_factory, voice_usage_setter):
    """A finite cap with headroom remaining must allow — distinct from the
    unlimited (None) case above and the at-cap boundary case in
    test_minute_cap_reached_denies."""
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=10)
    voice_usage_setter(tenant.id, minutes=5)
    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_missing_capabilities_row_denies_closed(db_session):
    """A tenant with no TenantCapabilities row at all (never provisioned,
    or row deleted) must be denied, not treated as unrestricted access."""
    from tests.conftest import _bare_voice_tenant

    tenant = _bare_voice_tenant(db_session)
    db_session.commit()

    gate = VoiceGate(db_session)
    result = await gate.check(tenant.id)
    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


@pytest.mark.asyncio
async def test_try_acquire_slot_is_atomic_under_concurrency(
    db_session, voice_tenant_factory, monkeypatch
):
    """Fires many genuinely-concurrent try_acquire_slot calls against a small
    ceiling and asserts exactly `ceiling` succeed. This is the property the
    Lua-script atomic reserve exists to guarantee — the old check()-then-
    acquire_slot() sequence (two separate Redis round trips) could let more
    than `ceiling` callers past a shared scard() peek. asyncio.gather here
    issues the requests concurrently over real Redis connections, so this
    only stays green if the reserve is actually atomic server-side, not
    merely because the calls happened to run one after another.
    """
    ceiling = 5
    fan_out = 30
    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", ceiling)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)

    results = await asyncio.gather(
        *(gate.try_acquire_slot(tenant.id, f"tok-{i}") for i in range(fan_out))
    )

    assert sum(1 for acquired in results if acquired) == ceiling


@pytest.mark.asyncio
async def test_try_acquire_slot_evicts_stale_members(db_session, voice_tenant_factory, monkeypatch):
    """A slot orphaned by a crashed/never-released session must age out of the
    ceiling instead of counting against it forever (the whole-key `EXPIRE`
    resets on every acquire, so it never protected an individual leaked
    member — see gating.py's `_CONCURRENCY_KEY_TTL_SECONDS` comment).

    Seeds a "stale" member directly in Redis (bypassing acquire_slot, the way
    a slot left by a crashed process would look — present in the set, but
    old), shrinks the TTL so that seed is unambiguously past it, then proves
    two things: a new caller isn't blocked by the stale entry, AND the stale
    entry is actually gone afterward — not merely skipped-over this one time,
    which would still leak memory and eventually blow the real ceiling once
    enough orphans accumulate.
    """
    from app.services.voice import gating as gating_module

    monkeypatch.setattr(gating_module, "_CONCURRENCY_KEY_TTL_SECONDS", 1)
    ceiling = 1
    monkeypatch.setattr(settings, "VOICE_MAX_CONCURRENT_SESSIONS", ceiling)

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)
    redis = await gate._get_redis()
    key = f"voice:concurrent:{tenant.id}"

    # Seed a slot that "acquired" 10 seconds ago — long past the 1s TTL —
    # without going through acquire_slot/try_acquire_slot, so this is purely
    # state, not a claim about either method's own correctness.
    await redis.zadd(key, {"stale-tok": time.time() - 10})
    assert await redis.zcard(key) == 1  # sanity: the stale entry is really there

    acquired = await gate.try_acquire_slot(tenant.id, "fresh-tok")

    assert acquired is True, "a stale slot must not count against the ceiling"
    members = await redis.zrange(key, 0, -1)
    assert members == ["fresh-tok"], "the stale entry must actually be evicted, not just skipped"


@pytest.mark.asyncio
async def test_tenant_disabled_voice_is_denied_even_when_granted(db_session, voice_tenant_factory):
    """The whole point of the preference: granted, but the tenant said no.

    Would fail if the preference check were missing entirely (the tenant would
    be allowed) — which is the defect this task exists to prevent.
    """
    from app.db.models.tenant import TenantAIConfig

    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)
    db_session.add(TenantAIConfig(tenant_id=tenant.id, voice_enabled=False))
    db_session.commit()

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


@pytest.mark.asyncio
async def test_ungranted_tenant_is_denied_by_the_grant_not_the_preference(
    db_session, voice_tenant_factory
):
    """An ungranted tenant is denied regardless of their preference setting.

    A tenant with no grant but preference ON must still be denied. This test
    cannot prove ordering (both the grant and preference checks return the same
    reason code), but it does guard against the preference check short-circuiting
    the grant denial. See test_ungranted_tenant_never_consults_the_preference
    for a mock-based test that actually proves ordering.
    """
    from app.db.models.tenant import TenantAIConfig

    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    db_session.add(TenantAIConfig(tenant_id=tenant.id, voice_enabled=True))
    db_session.commit()

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED


@pytest.mark.asyncio
async def test_missing_ai_config_row_does_not_block_a_granted_tenant(
    db_session, voice_tenant_factory
):
    """A tenant with no TenantAIConfig row at all must still get voice.

    The preference defaults to enabled, so absence means 'not opted out', not
    'disabled'. Would fail if the lookup treated a missing row as False —
    which would silently kill voice for every tenant lacking that row.
    """
    tenant = voice_tenant_factory(can_use_voice=True, max_voice_minutes_per_month=None)

    result = await VoiceGate(db_session).check(tenant.id)

    assert result.allowed is True


@pytest.mark.asyncio
async def test_ungranted_tenant_never_consults_the_preference(db_session, voice_tenant_factory):
    """Ordering is load-bearing and result-only assertions cannot prove it.

    Both guards return the same FLAG_DISABLED reason, so a test that only
    inspects the returned result passes whichever order they sit in —
    verified by physically swapping the blocks. The observable difference is
    a side effect: with the grant checked first, the preference lookup is
    never reached at all. That is also the property we actually want, since
    an ungranted tenant should cost no extra query.
    """
    tenant = voice_tenant_factory(can_use_voice=False, max_voice_minutes_per_month=None)
    gate = VoiceGate(db_session)

    with patch.object(
        VoiceGate, "_voice_enabled_by_tenant", return_value=True
    ) as preference_lookup:
        result = await gate.check(tenant.id)

    assert result.allowed is False
    assert result.reason == VoiceDenialReason.FLAG_DISABLED
    preference_lookup.assert_not_called()
