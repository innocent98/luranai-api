"""A capability-tier bypass must clear the override_flags it overwrites.

`PlatformSubscriptionService._update_capabilities_with_overrides` has two
modes: `respect_overrides=True` (the normal tier sync -- skip any field
listed in `override_flags`) and `respect_overrides=False` (a deliberate
bypass -- reset every field in the tier's plan config regardless of
`override_flags`). Three callers use the bypass: `override_tenant_subscription`
with `apply_tier_capabilities=True`, and both `PromotionalGrantService`
call sites (`create_grant` and `_revert_tenant_to_snapshot`, the latter
reached by `expire_grants_due` and `revoke_grant`).

Before this fix, the bypass overwrote the *value* of every overridden field
but never touched the *flag* -- `override_flags` kept saying "this field is
manually overridden" for a field that had just been reset to a tier
default. Two live consequences, both only possible once
`TenantService.update_capabilities` started actually populating
`override_flags` (see `test_tenant_capabilities_override_flags.py`):

1. Every ordinary tier sync afterward (`respect_overrides=True`) would keep
   skipping that field forever, pinning it to whatever value the bypass
   happened to write, with no admin action responsible for that pin.
2. A promo-grant expiring would leave a stale flag behind even though the
   value had already been reverted to the pre-grant tier's default --
   the flag becomes a lie about what's actually overridden.

The fix: when a bypass overwrites a field, its entry (if any) is removed
from `override_flags`, so the flag and the value tell the same story again.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.platform_subscription_service import PlatformSubscriptionService
from app.services.promotional_grant_service import PromotionalGrantService
from tests.conftest import _bare_voice_tenant as _tenant


def _fetch_capabilities(db_session, tenant_id):
    db_session.expire_all()
    return (
        db_session.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one()
    )


def test_bypass_clears_the_override_flag_it_overwrites(db_session):
    """Would fail (override_flags still containing the key) against the
    pre-fix implementation, which only ever set capabilities.override_flags
    via TenantService.update_capabilities and never cleared it anywhere."""
    tenant = _tenant(db_session)  # subscription_tier="trial"
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            max_knowledge_sources=999,  # a manual override value
            override_flags={"max_knowledge_sources": True},
        )
    )
    db_session.commit()

    PlatformSubscriptionService(db_session)._update_capabilities_with_overrides(
        tenant.id, "trial", respect_overrides=False
    )

    caps = _fetch_capabilities(db_session, tenant.id)
    assert caps.max_knowledge_sources == 2  # trial default, per scripts/seed_data.py
    assert "max_knowledge_sources" not in (caps.override_flags or {})


def test_bypass_preserves_override_flags_for_fields_outside_the_plan_config(db_session):
    """A bypass must clear only the flags for fields its own plan config
    actually overwrote -- an override flag for a field the tier config
    doesn't mention (can_use_voice/max_voice_minutes_per_month aren't in
    PlanService.get_plan_config yet, see
    test_subscription_tier_sync_respects_manual_override.py) must survive
    untouched. Guards against a fix that clears override_flags wholesale
    (e.g. `capabilities.override_flags = {}`) instead of only the keys it
    actually overwrote.
    """
    tenant = _tenant(db_session)
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            max_knowledge_sources=999,  # in trial's plan config -> gets cleared
            can_use_voice=True,  # not in any plan config yet -> must survive
            override_flags={
                "max_knowledge_sources": True,
                "can_use_voice": True,
            },
        )
    )
    db_session.commit()

    PlatformSubscriptionService(db_session)._update_capabilities_with_overrides(
        tenant.id, "trial", respect_overrides=False
    )

    caps = _fetch_capabilities(db_session, tenant.id)
    assert caps.max_knowledge_sources == 2  # trial default -- bypass overwrote it
    assert caps.can_use_voice is True  # untouched by the bypass
    assert caps.override_flags == {"can_use_voice": True}


def test_promo_grant_expiry_does_not_strand_a_stale_override_flag(db_session):
    """End-to-end through the real lifecycle path: a platform admin manually
    overrides a capability while a promotional grant is active; the grant
    then expires and reverts the tenant to its pre-grant tier. The bypass
    that revert runs must clear the flag, not just the value -- otherwise
    the field stays pinned to the reverted value forever, invisible to every
    future ordinary tier sync.

    Would fail (override_flags still containing the key post-expiry)
    against the pre-fix implementation.
    """
    tenant = Tenant(
        name=f"promo_test_{uuid4().hex[:8]}",
        display_name="Promo Test Co",
        api_key_hash=uuid4().hex,
        subscription_tier="professional",
        subscription_status="active",
    )
    db_session.add(tenant)
    db_session.flush()

    # Admin manually overrode a field while the tenant was on the grant.
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            max_knowledge_sources=999,
            override_flags={"max_knowledge_sources": True},
        )
    )

    grant = PromotionalGrant(
        tenant_id=tenant.id,
        campaign_code="TEST_STALE_FLAG",
        campaign_name="Stale Flag Test Campaign",
        granted_tier="professional",
        duration_days=90,
        grant_starts_at=datetime.now(UTC) - timedelta(days=91),
        grant_ends_at=datetime.now(UTC) - timedelta(days=1),  # already due for expiry
        previous_tier="trial",
        previous_status="trial_expired",
        status="active",
        source="admin_single",
        reason="stale override flag regression test",
    )
    db_session.add(grant)
    db_session.commit()

    expired_ids = PromotionalGrantService(db_session).expire_grants_due()
    assert grant.id in expired_ids

    caps = _fetch_capabilities(db_session, tenant.id)
    assert caps.max_knowledge_sources == 2  # trial default -- the revert applied
    assert "max_knowledge_sources" not in (caps.override_flags or {})
