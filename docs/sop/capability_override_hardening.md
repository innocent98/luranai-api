# SOP — Capability override hardening: delete the self-upgrade escalation route, make override_flags real

**Status:** Shipped on branch `fix/capability-override-hardening` (off `develop`, not yet merged) ·
commits `d77ecfa` through `c09cccc` (`git log e6e6fb8..c09cccc` for the full range — `e6e6fb8` is
`develop`'s merge-base and is excluded) · 2026-08-22 · this doc's own commit follows the range above.
**Area:** `app/api/v1/endpoints/subscription.py`, `app/schemas/subscription.py`,
`app/services/tenant_service.py`, `app/services/subscription_service.py`,
`app/services/platform_subscription_service.py`

---

## What shipped

Two related fixes to the tenant-capability system, found while reviewing the upcoming
tenant-voice-settings plan-config work (`docs/sop/tenant_voice_settings.md`'s Follow-ups flagged
this as a blocking prerequisite):

1. **Deleted `POST /api/v1/subscription/upgrade`** (`upgrade_subscription_direct`) — a
   tenant-admin-authed route that upgraded a tenant's subscription tier with no payment
   processing, and along the way overwrote `TenantCapabilities` with whatever fields that tier's
   plan config carried.
2. **Made `override_flags` — the guard meant to protect a legitimate capability grant from being
   undone by a tier sync — actually work.** It was read in three places and written in none, so
   the "manual overrides will be preserved" promise on `_update_capabilities_for_tier`'s docstring
   was inert. Fixed the write, then (via review) fixed two follow-on correctness gaps in the same
   mechanism: a capability-tier bypass leaving stale flags behind, and upgrade vs. downgrade both
   respecting overrides when only upgrade should.

| Commit | What it covers |
|---|---|
| `d77ecfa` | Delete `upgrade_subscription_direct` + orphaned `SubscriptionUpgradeRequest`/`Response` schemas + 404 regression test |
| `5e4ef7f` | `TenantService.update_capabilities` now writes `override_flags` |
| `c44288b` | A capability-tier bypass (`respect_overrides=False`) clears the flags it overwrites instead of stranding them |
| `2e0c084` | Test hardening: prove the subscription router is mounted, not just the deleted route missing |
| `c09cccc` | Decided behaviour: overrides survive an upgrade, are cleared on a downgrade |

## Why

### The escalation path, and why deletion beat re-gating

```
POST /api/v1/subscription/upgrade
  auth: Depends(get_current_tenant_admin)          <- a TENANT admin
  docstring: "upgrades subscription without payment processing"
        v  SubscriptionService.upgrade_subscription()
        v  _update_capabilities_for_tier()
        v  for key, value in plan_config.items(): setattr(capabilities, key, value)
```

A tenant admin could hand themselves any subscription tier for free. Today that's contained to the
capability fields already in `SubscriptionPlan.get_capabilities()` — but the tenant-voice-settings
follow-on work adds voice keys to plan configs, at which point this same route becomes a route to
self-granting `can_use_voice`/`max_voice_minutes_per_month`, a paid capability the platform-admin
grant path exists specifically to gate.

The route was already marked `⚠️ DEPRECATED` in its own docstring, had zero callers (the dashboard
calls `/upgrade/initialize` + `/upgrade/verify`; `luran-chat` calls neither), and zero test
coverage. `override_tenant_subscription`
(`app/api/v1/endpoints/platform/subscriptions.py`, super-admin gated) is the existing, correct
"manual admin upgrade" path.

**Re-gating (swap the dependency to a platform-admin check) doesn't work.** The handler derives
the tenant from `current_user.tenant_id`, and `SubscriptionUpgradeRequest` has no `tenant_id`
field — a platform admin has no tenant of their own to resolve. Deletion was the only fix that
actually closes the path; a super-admin version of this exact route isn't possible without a
different request contract, which already exists as `override_tenant_subscription`.

The orphaned `SubscriptionUpgradeRequest`/`SubscriptionUpgradeResponse` schemas were deleted too —
an unreferenced request/response pair for a deliberately-removed endpoint is an invitation for
someone to "restore the missing endpoint" later, which would reopen exactly this path.

### `override_flags` was read in three places, written in none

`override_flags` on `TenantCapabilities` (comment: `{field_name: true} to persist through tier
upgrades`) is read by:
- `SubscriptionService._update_capabilities_for_tier`
- `PlatformSubscriptionService._update_capabilities_with_overrides`
- `PlatformSubscriptionService.get_subscription_details` (surfaces `has_overrides`/`override_fields`
  in the platform-admin subscription-detail view)

Nothing wrote it. `TenantService.update_capabilities` — the only place a super admin actually sets
a manual capability override — applied the requested fields with a plain `setattr` loop and never
touched `override_flags`. So every read-side guard was checking a column that was always empty (or
`None`): a manual grant looked "safe" by design, but the very next tier sync (upgrade, downgrade,
webhook-confirmed payment, promo-grant lifecycle) would silently overwrite it back to the plan
default. The docstring's promise ("manual overrides will be preserved") was never true in
production; it just happened to read as if it were.

## How it works — three decisions worth explaining

### 1. Delete, don't re-gate

Covered above. Worth restating as the general principle this branch follows: when a route's
*existence* is the vulnerability (not just its auth), fixing the auth doesn't fix the vulnerability
if a legitimate, differently-shaped replacement already exists.

### 2. Fields, not the whole column, get flagged — and cleared

`TenantService.update_capabilities` now merges each explicitly-set field
(`model_dump(exclude_unset=True)` — fields the caller never mentioned are never flagged) into
`override_flags`, onto whatever was already there (a tenant accumulates overrides across separate
admin actions; a second call must not wipe the first one's flags).

The inverse also had to be fixed, once flags could actually exist: **a capability-tier bypass
(`respect_overrides=False`) must clear the flags for the fields it overwrites, not just the
values.** Three call sites run this bypass — `override_tenant_subscription(apply_tier_capabilities=True)`,
and both `PromotionalGrantService` call sites (`create_grant`, and `_revert_tenant_to_snapshot`,
reached by `expire_grants_due`/`revoke_grant`). Before this fix, a bypass reset a field's *value*
to the tier default but left its *flag* set — which meant every future *ordinary* sync
(`respect_overrides=True`) would keep skipping that field forever, pinned to whatever the bypass
happened to write, with no admin action responsible for the pin. A promo grant expiring was a
concrete instance of this: `_revert_tenant_to_snapshot` reverts the tenant to their pre-grant tier,
but without this fix would leave a stale flag behind even though the value had already reverted —
the flag becomes a lie about what's actually overridden. All three sites share one implementation
(`_update_capabilities_with_overrides`), so one fix covers all three.

Both fixes (write-side and clear-side) assign a **new** dict to `override_flags` rather than
mutating the existing one in place — SQLAlchemy does not track in-place mutation of a JSON column,
so `capabilities.override_flags[field] = True` would silently never persist.

### 3. Overrides survive an upgrade, are cleared on a downgrade — decided, not left open

This was raised during review as an open question ("a tenant granted `max_knowledge_sources=500`
keeps it after self-downgrading to trial — is that intended?") and then decided by Adebayo:
**respect overrides on upgrade, ignore them on downgrade.**

Rationale: a manually-granted capability surviving an *upgrade* is the documented, intended
purpose of the mechanism — a platform admin's grant shouldn't be undone by a routine tier bump. It
surviving a *downgrade* is different: a tenant who drops to trial should land on trial's real
limits, including a field a super admin previously raised by hand. Keeping it was undetected
revenue leakage nobody had decided on.

`_update_capabilities_for_tier` (`app/services/subscription_service.py`) now takes an explicit
`respect_overrides` argument rather than inferring direction from tier comparison — the caller
already knows its own direction (`downgrade_subscription` validates that direction against its own
`tier_order` dict before ever reaching this call), so a second, inferred source of truth would only
invite drift between the two. `upgrade_subscription` passes `True`; `downgrade_subscription` passes
`False`. The parameter defaults to `False` (ignore/clear overrides) — named so a future caller that
forgets to pass this explicitly reverts every field to the tier default rather than silently
leaking a capability past a downgrade. The downgrade branch also clears stale flags for fields it
overwrites, for the same reason as the bypass fix above.

**Scope of the change:** `override_flags` is only ever populated by a super admin hand-editing
capabilities through `TenantService.update_capabilities`. A tenant who upgrades and downgrades
with no admin ever touching their capabilities has no flags at all and downgrades cleanly either
way — this only changes behaviour for a field a human deliberately overrode.

## What's involved (files touched)

| File | Change |
|---|---|
| `app/api/v1/endpoints/subscription.py` | Deleted `upgrade_subscription_direct` + 2 unused schema imports |
| `app/schemas/subscription.py` | Deleted orphaned `SubscriptionUpgradeRequest`/`SubscriptionUpgradeResponse` |
| `app/services/tenant_service.py` | `update_capabilities` now writes `override_flags` (merge, not replace) |
| `app/services/platform_subscription_service.py` | `_update_capabilities_with_overrides` clears flags for fields a bypass overwrites |
| `app/services/subscription_service.py` | `_update_capabilities_for_tier` takes explicit `respect_overrides`; upgrade passes `True`, downgrade passes `False` (and clears stale flags in the `False` branch) |
| `tests/api/v1/test_subscription_upgrade_direct_removed.py` | Route deleted (404) + router-still-mounted regression test |
| `tests/services/test_tenant_capabilities_override_flags.py` | `update_capabilities` write-side unit tests |
| `tests/services/test_subscription_tier_sync_respects_manual_override.py` | Composed proof: override survives upgrade, is overwritten by downgrade |
| `tests/services/test_capability_override_bypass_clears_flags.py` | Bypass clears overwritten flags, preserves untouched ones, promo-grant expiry doesn't strand a flag |
| `docs/sop/tenant_voice_settings.md` | Follow-up bullet marked resolved, pointing here |
| `docs/FRONTEND_API_DOCUMENTATION.md` | §12.3 rewritten from "deprecated" to "removed", with the replacement route |
| `docs/RATE_LIMITERS.md` | Deleted route removed from the `payment_rate_limit` endpoint list |
| `docs/SUBSCRIPTION_TESTING_GUIDE.md` | curl example repointed at `POST /platform/subscriptions/{tenant_id}/override` |
| `../docs/checklist/luran-master-checklist.md` (workspace root, outside this repo) | 🔴 blocker marked resolved |

No migration — `override_flags` (JSON, nullable, `default=dict`) already existed on
`tenant_capabilities`; this shipment is entirely read/write-path code, no schema change.

## Verification

Full suite run against `luran_test` (not the partially-migrated dev DB):
```bash
PW=$(grep -E '^DATABASE_URL' .env | sed 's|.*://luran_user:||' | sed 's|@.*||')
DATABASE_URL="postgresql://luran_user:${PW}@localhost:5432/luran_test" \
  REDIS_URL=redis://localhost:6379/0 poetry run pytest tests/ -q --no-cov
```
Baseline before this branch: 1159 passed, 33 skipped, 0 failed. After all five commits: 1164+
passed (baseline plus this branch's new tests), 33 skipped, **0 failed** throughout — every fix in
this SOP was TDD'd (RED before the fix, GREEN after), and the three fixes with a break-it step
(override_flags write, bypass flag-clearing, downgrade-clears-overrides) were each independently
verified by reverting just that fix and confirming its own test — and only its own test — goes red.

`ruff` and `black --check` clean on every file this branch touches (pre-existing repo-wide drift
elsewhere left untouched, per standing convention). `mypy` clean on every file this branch touches
except 3 pre-existing, unrelated errors in `tenant_service.py:175-178` (auto-signup-grant argument
typing), confirmed pre-existing by diffing mypy output against the file before this branch's edits.

A second reviewer independently re-verified the security claim from first principles (grepped
every `TenantCapabilities` writer rather than trusting the two call sites named during triage,
found exactly three, enumerated every route reaching them, confirmed no tenant-scoped principal
can reach a capability increase) and mutation-tested the composed test three ways: deleting the
`override_flags` write (3 of 4 related tests fail, the negative test correctly still passes),
reverting the fix and separately stripping the intermediate assertion (proving the final assertion
is load-bearing and `professional`'s plan config really is 50), and replacing the new-dict
assignment with in-place mutation (which the merge test catches, proving the `expire_all()` +
re-query pattern used throughout these tests is doing real work, not decoration).

## Operate / roll back

No deploy-time action needed beyond the normal merge — no migration, no config flag, no data
backfill. To roll back, revert commits `d77ecfa..c09cccc` in order; each is independently
revertible (they touch disjoint code paths except the two `subscription_service.py`/
`platform_subscription_service.py` commits, which apply cleanly in sequence).

If this needs to be un-shipped **after** voice keys land in plan configs, re-verify the original
escalation path is still closed before re-adding any tenant-facing direct-upgrade route — the
motivating reason for `d77ecfa` doesn't go away just because this SOP predates that follow-on work.

## Follow-ups

- **`can_use_voice`/`max_voice_minutes_per_month` still aren't in `PlanService.get_plan_config`.**
  This branch's fixes are what make it *safe* to add them (a tenant can no longer self-upgrade, and
  a super-admin voice grant now genuinely survives a tier sync) — but the composed and bypass tests
  in this branch prove the mechanism using `max_knowledge_sources`, the only field that's actually
  wired into every tier's plan config today. **Whoever adds voice keys to plan configs must add a
  `can_use_voice`-specific sibling of `test_manual_override_survives_upgrade_tier_sync` (and
  ideally the downgrade/bypass tests too)** — a field absent from the plan config can't exercise
  either guard, so this branch's tests can't be assumed to cover it by analogy.
- **Deferred, not addressed here** (flagged during review, explicitly out of scope for this pass):
  the explicit-`null` flagging edge case on `TenantCapabilitiesUpdate` (does an explicit `null` for
  a nullable field count as "set" for override-flagging purposes — currently yes, via
  `exclude_unset=True`, which is arguably correct but wasn't stress-tested here), the `conftest`
  import style used across these new test files (`from tests.conftest import _bare_voice_tenant as
  _tenant`, matching the existing voice-test convention rather than a dedicated capability-test
  fixture), and `max_audits_per_month`/`max_audit_pages_per_crawl` not being reachable through
  `TenantCapabilitiesUpdate` at all (so they can never be manually overridden or flagged, unrelated
  to this shipment's bug).
