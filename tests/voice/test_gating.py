import pytest

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
