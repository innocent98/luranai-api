"""The composed proof: Part 1 (delete the self-upgrade escalation route) and
Part 2 (`update_capabilities` writes `override_flags`) only matter together
if a manual override genuinely survives an *upgrade* tier sync -- and, per
Adebayo's decision, is genuinely cleared by a *downgrade* one.

`SubscriptionService._update_capabilities_for_tier` (the routine both
`upgrade_subscription` and `downgrade_subscription` funnel through) takes
an explicit `respect_overrides` argument rather than inferring direction
from tier comparison -- the caller already knows whether it's upgrading or
downgrading (`downgrade_subscription` already validates that direction
against `tier_order` before it ever reaches this call), so a second,
inferred source of truth for direction would only invite drift. The default
is `False` (ignore/clear overrides): a future caller that forgets to pass
this explicitly reverts every field to the tier default rather than
silently leaking a capability past a downgrade.

- `respect_overrides=True` (what `upgrade_subscription` passes): a field
  listed in `override_flags` is left untouched -- a platform admin's manual
  grant survives a routine tier bump. This is the documented, decided
  intent: the override mechanism exists specifically so a manual grant
  isn't undone by an upgrade.
- `respect_overrides=False` (what `downgrade_subscription` passes, and the
  default): every field in the tier's plan config is written
  unconditionally, and any override flag for a field this sync just
  overwrote is cleared -- same "don't strand a stale flag" fix as
  `PlatformSubscriptionService._update_capabilities_with_overrides`'s
  bypass mode (see `test_capability_override_bypass_clears_flags.py`).
  Keeping a manually-raised limit past a self-service downgrade to trial
  would be undetected revenue leakage nobody decided on; this makes the
  downgrade land the tenant on the new tier's real limits.

Context for why this only matters now: override_flags is only ever
populated by a super admin hand-editing capabilities through
`TenantService.update_capabilities`. A tenant who upgrades and downgrades
with no admin ever touching their capabilities has no flags at all, and
downgrades cleanly either way -- this whole distinction is only observable
for a field a human deliberately overrode.

Why `max_knowledge_sources`: this whole hardening effort was motivated by
an upcoming change that adds voice keys to plan configs, so `can_use_voice`
looks like the obvious field to prove either test with. It would be the
wrong choice today: `PlanService.get_plan_config` (backed by
`scripts/seed_data.py`) does not put `can_use_voice` in any tier's config
yet, so `_update_capabilities_for_tier`'s `for key, value in config.items()`
loop never touches it -- both tests below would pass identically regardless
of whether the fix works. `max_knowledge_sources` is in every tier's config
today (trial=2, lite=15, professional=50), so the sync step actually does
something these tests can catch. The downgrade test in particular needs a
field whose value differs at all three points -- the manual override, the
tenant's current (higher) tier, and the tier it's downgrading to -- so the
assertion can't pass by coincidence.

**Follow-up for whoever wires voice into plan configs:** once
`can_use_voice` (or any other voice key) appears in `get_plan_config`,
add `can_use_voice`-specific siblings of both tests below. Don't assume
these cover it by analogy -- a field absent from the plan config can't
exercise either guard, so a new field needs its own proof.
"""

from app.db.models.tenant import TenantCapabilities
from app.schemas.tenant import TenantCapabilitiesUpdate
from app.services.subscription_service import SubscriptionService
from app.services.tenant_service import TenantService
from tests.conftest import _bare_voice_tenant as _tenant


def _fetch_capabilities(db_session, tenant_id):
    db_session.expire_all()
    return (
        db_session.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tenant_id).one()
    )


def test_manual_override_survives_upgrade_tier_sync(db_session):
    """respect_overrides=True (the upgrade case): the override wins."""
    tenant = _tenant(db_session)  # subscription_tier="trial" by default
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            can_access_analytics=False,
            max_knowledge_sources=2,  # trial default per scripts/seed_data.py
        )
    )
    db_session.commit()

    # Step 1: a super admin manually raises the cap via the real write path.
    TenantService(db_session).update_capabilities(
        tenant.id, TenantCapabilitiesUpdate(max_knowledge_sources=500)
    )
    overridden = _fetch_capabilities(db_session, tenant.id)
    assert overridden.max_knowledge_sources == 500
    assert overridden.override_flags == {"max_knowledge_sources": True}

    # Step 2: an upgrade sync runs for a tier whose plan config disagrees
    # (50, neither the trial default nor the override).
    SubscriptionService(db_session)._update_capabilities_for_tier(
        tenant.id, "professional", respect_overrides=True
    )

    synced = _fetch_capabilities(db_session, tenant.id)
    assert synced.max_knowledge_sources == 500, (
        "manual override was clobbered by the professional plan's default (50) -- "
        "override_flags did not survive the upgrade write path"
    )
    assert synced.override_flags == {"max_knowledge_sources": True}, (
        "the flag itself must survive an upgrade too -- a later downgrade still "
        "needs to know this field was manually set"
    )


def test_manual_override_is_overwritten_by_downgrade_tier_sync(db_session):
    """respect_overrides=False (the downgrade case, and the default): the
    new tier's default wins, and the stale flag is cleared.

    Would fail (max_knowledge_sources staying 500, or the flag surviving)
    against an implementation that respects override_flags unconditionally
    -- i.e. the pre-this-change behaviour of _update_capabilities_for_tier,
    which is exactly the revenue-leakage bug this test guards against.
    """
    tenant = _tenant(db_session)
    tenant.subscription_tier = "professional"
    db_session.add(
        TenantCapabilities(
            tenant_id=tenant.id,
            max_knowledge_sources=50,  # professional default per scripts/seed_data.py
        )
    )
    db_session.commit()

    # A super admin manually raises the cap while the tenant is on professional.
    TenantService(db_session).update_capabilities(
        tenant.id, TenantCapabilitiesUpdate(max_knowledge_sources=500)
    )
    overridden = _fetch_capabilities(db_session, tenant.id)
    assert overridden.max_knowledge_sources == 500
    assert overridden.override_flags == {"max_knowledge_sources": True}

    # The tenant downgrades to lite (default 15 -- distinct from both the
    # manual override of 500 and professional's 50).
    SubscriptionService(db_session)._update_capabilities_for_tier(
        tenant.id, "lite", respect_overrides=False
    )

    synced = _fetch_capabilities(db_session, tenant.id)
    assert synced.max_knowledge_sources == 15, (
        "downgrade must land the tenant on the new tier's real limit, not the "
        "manual override (500) and not the old tier's value (50)"
    )
    assert "max_knowledge_sources" not in (synced.override_flags or {}), (
        "the flag must be cleared, not just the value overwritten -- otherwise the "
        "field stays permanently exempt from every future ordinary tier sync"
    )
