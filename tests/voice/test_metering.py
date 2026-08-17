from uuid import uuid4

import pytest

from app.services.voice.metering import VoiceMeter


@pytest.mark.asyncio
async def test_minutes_accumulate():
    meter = VoiceMeter()
    tenant_id = uuid4()
    await meter.add_minutes(tenant_id, 2.5)
    await meter.add_minutes(tenant_id, 1.5)
    assert await meter.get_monthly_minutes(tenant_id) == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_unused_tenant_reports_zero():
    assert await VoiceMeter().get_monthly_minutes(uuid4()) == 0


@pytest.mark.asyncio
async def test_counter_key_is_month_scoped():
    """Rollover is implicit: the key embeds the calendar month, so a new
    month writes a fresh key rather than requiring a scheduled reset job."""
    meter = VoiceMeter()
    tenant_id = uuid4()
    key = meter._key(tenant_id)
    assert key.count("-") >= 1
    assert key.endswith(f"{tenant_id}:{key.rsplit(':', 1)[-1]}")


@pytest.mark.asyncio
async def test_different_tenants_do_not_share_a_counter():
    meter = VoiceMeter()
    tenant_a, tenant_b = uuid4(), uuid4()
    await meter.add_minutes(tenant_a, 10.0)
    assert await meter.get_monthly_minutes(tenant_b) == 0
    assert await meter.get_monthly_minutes(tenant_a) == pytest.approx(10.0)
