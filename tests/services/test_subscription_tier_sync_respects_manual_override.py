"""The composed proof: Part 1 (delete the self-upgrade escalation route) and
Part 2 (`update_capabilities` writes `override_flags`) only matter together
if a manual override genuinely survives a subsequent tier sync.

`SubscriptionService._update_capabilities_for_tier` (the routine every
upgrade/downgrade path funnels through -- webhook confirmation, platform
admin override, downgrade) already *reads* `override_flags` and skips any
field listed there. That guard was always correct. What was missing is
`TenantService.update_capabilities` ever populating `override_flags` in the
first place, which is what `tests/services/test_tenant_capabilities_override_flags.py`
covers in isolation. This test proves the two halves compose end to end:
a real admin override made through the real write path survives a real
tier sync that would otherwise clobber it.

Scenario: a tenant starts on `trial` (max_knowledge_sources=2 per
`scripts/seed_data.py`). A super admin manually raises the cap to 500. A
tier sync to `professional` then runs -- `professional`'s plan config sets
max_knowledge_sources=50, which does not match either the trial default (2)
or the override (500), so the assertion cannot pass by coincidence: it can
only pass if the override genuinely took priority over the plan config.

Why `max_knowledge_sources` and not `can_use_voice`: this whole hardening
effort was motivated by an upcoming change that adds voice keys to plan
configs, so `can_use_voice` looks like the obvious field to prove the fix
with. It would be the wrong choice *today*: `PlanService.get_plan_config`
(backed by `scripts/seed_data.py`) does not put `can_use_voice` in any
tier's config yet, so `_update_capabilities_for_tier`'s
`for key, value in config.items()` loop never touches it -- the tier-sync
step would be a silent no-op, and the test would pass identically whether
or not the `override_flags` write in `TenantService.update_capabilities`
existed at all. That's the exact vacuous-test failure mode this codebase
keeps producing (see the module docstring history). `max_knowledge_sources`
is in every tier's config today, so the sync step actually does something
this test can catch.

**Follow-up for whoever wires voice into plan configs:** once
`can_use_voice` (or any other voice key) appears in `get_plan_config`,
add a `can_use_voice`-specific sibling of this test. Don't assume this one
covers it by analogy -- the whole point above is that a field absent from
the plan config can't exercise the guard, so a new field needs its own
proof.
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


def test_manual_override_survives_tier_sync(db_session):
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

    # Step 2: a tier sync runs for a tier whose plan config disagrees (50,
    # neither the trial default nor the override).
    SubscriptionService(db_session)._update_capabilities_for_tier(tenant.id, "professional")

    synced = _fetch_capabilities(db_session, tenant.id)
    assert synced.max_knowledge_sources == 500, (
        "manual override was clobbered by the professional plan's default (50) -- "
        "override_flags did not survive the write path"
    )
