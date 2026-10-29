# SOP: WhatsApp is plan-driven for every tenant (default-on) + capacity alert, 2026-10-29

## What shipped
Branch `feature/whatsapp-default-on` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges). Production untouched.

1. **WhatsApp is a plan capability.** `can_use_whatsapp` is now a column on `subscription_plans` (true for Trial, Lite, Professional, Growth, Enterprise). Number counts are unchanged: 1 / 1 / 2 / 5 / unlimited.
2. **Alembic `wa30whatsappdefaulton`** (down_revision `ref10referral`, single head): adds the plan column, sets it, rewrites the one WhatsApp feature string, backfills `tenant_capabilities.can_use_whatsapp` from the tenant's plan **except where `override_flags` has a `can_use_whatsapp` key**.
3. **Resolver**: `FEATURES["whatsapp"]` uses the plan flag (no more `grant`); `GET /tenant/entitlements` `whatsapp.enabled` follows plan/override; the `whatsapp` entry gains `platform_available` (the kill switch).
4. **Capacity alert**: operators in `PLATFORM_ALERT_EMAILS` are emailed once per 6 hours per condition when live connections near `WHATSAPP_MAX_CONNECTIONS` or the sidecar's memory nears its refuse threshold.
5. **Stable tenant-facing 503**: `{"code":"capacity_exceeded","message":"WhatsApp is temporarily at capacity. Please try again later."}` from one constant at all four raise sites.

FE contract: `docs/fe-integration-guide-whatsapp-default-on.md` (captures in `docs/fe-integration-captures/whatsapp-default-on/`, script `scripts/capture_whatsapp_default_on.py`). Plan: `docs/superpowers/plans/2026-10-29-whatsapp-default-on.md`.

## Why
The 2026-10-28 decision made WhatsApp a platform grant: default false, switched on one tenant at a time by an admin. On 2026-10-29 the owner reversed it: WhatsApp should be available to every tenant by plan. The risk of "everyone" is the 8 GB VPS (per-session RAM beyond one session is unmeasured), so operators now get a warning *before* tenants hit the 503.

## How (decisions and trade-offs)
| Decision | Why / rejected alternative |
|---|---|
| Plan column + the existing `override_flags` mechanism, not a new "revoked" table | The platform capabilities editor already records every sent field in `override_flags` and the resolver already keeps a stored value for any listed field. Making the field a plan column is enough; nothing new to learn for admins. Rejected: a separate `whatsapp_revoked` flag (second source of truth). |
| Backfill skips any tenant with a `can_use_whatsapp` key in `override_flags` | An admin's explicit `true` or `false` survives. Rejected: backfilling everyone (would silently undo revokes). |
| A **revoke** (override present, stored `false`) also survives forced tier syncs (downgrade, promotional grant start/end, platform tier override); every other field still resets on a forced sync | Added after the Opus review: a revoke is a safety action (spam, abuse); the new plan-mirrored field would otherwise have let a tenant regain WhatsApp by downgrading. An admin-set `true` is *not* special: it is reset like any other override. |
| `enabled` does not include `WHATSAPP_ENABLED`; new additive `platform_available` does | With default-on, every tenant shows `enabled: true` while production is dark. The dashboard must hide the section from `platform_available`; folding the kill switch into `enabled` would hide *why* from the tenant state. |
| Inbound/outbound (`_tenant_may_use`) go through the resolver `value("can_use_whatsapp")`, like the create path | They read the stored copy before, so a stale copy could widen access. `value()` (not `enabled()`) keeps the lapsed-account behaviour unchanged (the 402 path handles it). Cost: about 4 small queries per message. |
| Downgrade validation: a target plan without the flag allows 0 numbers | Same violation shape as before (`whatsapp_numbers`). Every current plan has the flag, so this only matters for a future plan without it. |
| Feature string `WhatsApp (beta, on request): N` -> `WhatsApp (beta): N` | It is no longer on request. The migration replaces only that element and only when the stored list holds exactly the old string; hand-edited lists are left and a WARNING names the tier. |
| Alert: no `/health` poll existed, so one arq cron every 5 minutes (`whatsapp_capacity_check`) plus a cheap count-only check after a successful create/re-pair | Reuse was impossible; the cron is not a tight loop and does nothing while `WHATSAPP_ENABLED` is false. Rejected: polling from the request path. |
| `connections` condition fires at `min(ceil(PERCENT% of max), max - 1)` (at least 1) | With the real limits (3 default, 2 in `.env.example`) 80% equals 100%, i.e. after tenants already got the 503. One slot short is the useful warning. |
| Dedupe: Redis `SET wa:capacity_alert:<condition> NX EX 21600`; the winner sends; if every recipient fails the key is deleted so the next run retries; empty `PLATFORM_ALERT_EMAILS` logs a warning and does **not** consume the key; Redis down sends nothing | Better silent for one run than a mail every 5 minutes. SMTP is blocking, so it runs in a worker thread. Failures are logged by exception class and never raised into request paths or worker jobs. |
| Post-create hook is a fire-and-forget task with its own DB session | A slow SMTP server can never delay `POST /whatsapp/connections`. |

## What's involved
* `alembic/versions/wa30_whatsapp_default_on.py` (frozen values, `SET LOCAL lock_timeout='5s'`, existence-checked, idempotent, reversible).
* Models/schemas: `app/db/models/subscription_plan.py`, `app/db/models/tenant.py` (`TENANT_ONLY_CAPABILITY_FIELDS` no longer lists the flag), `app/schemas/{entitlements,pricing,tenant}.py`.
* `app/services/plan_catalog.py` (flag on every tier, feature string), `app/services/entitlements.py` (feature def, `platform_available`, revocation rule in `apply_plan_to_capabilities`), `app/services/whatsapp_inbound.py`, `app/services/subscription_validation_service.py`, `scripts/seed_staging_e2e.py`.
* Capacity: `app/services/whatsapp_capacity.py` (new), `app/channels/{base,linked_device}.py` (`SidecarHealth`, `get_health`), `app/services/whatsapp_service.py` (`CAPACITY_MESSAGE`, post-create hook), `app/services/email_service.py` + `app/templates/emails/whatsapp_capacity_alert.html`, `app/workers/whatsapp.py` + `app/workers/document_processor.py` (cron every 5 minutes), `app/core/config.py` + `.env.example` (`PLATFORM_ALERT_EMAILS`, `WHATSAPP_CAPACITY_ALERT_PERCENT`).
* Tests: `tests/entitlements/*`, `tests/migrations/test_wa30_whatsapp_default_on.py`, `tests/whatsapp/test_capacity_alert.py` (+ conftest), `tests/referral/test_models_migration.py` (single-head assertion relaxed to "`ref10referral` is in the chain").

## What "WhatsApp grant" means for the platform admin now
`PUT /api/v1/platform/tenants/{id}/capabilities {"can_use_whatsapp": true|false}` is unchanged on the wire and still records an override. `false` revokes (survives plan changes and forced syncs); `true` undoes a revoke (it is otherwise a no-op because every plan includes WhatsApp). `max_whatsapp_numbers` is the per-tenant allowance as before. There is no endpoint to clear an override and the response does not show `override_flags` (follow-up).

## Verification
| Check | Result |
|---|---|
| `ruff check app/` (0.8.6), `black --check app/` (24.10.0) | see the final CI-parity row in the PR description |
| Migration up/down/up on scratch Postgres 15 (automated round trip test + by hand on a live local DB: old-state tenants a/b/c/d: never granted, hand-granted, revoked, NULL flags) | capture 22: never granted -> true, hand-granted kept, revoked kept false, NULL flags -> true, second upgrade changed nothing |
| Live API (Postgres 15, Redis 7, fake sidecar, local SMTP sink) | captures 01-22; see the verification table of the FE guide |
| NOT exercised | the real Baileys sidecar, a real phone, a real SMTP provider, production-size data (backfill is one set-based UPDATE), revoke surviving a downgrade/promo/tier override with a real Paystack flow (unit tests only) |

## Operate / roll back
* **Before deploying to any real environment, count the tenants the backfill will leave dark** (admin revoked, or the admin UI once sent the whole capabilities form, which records an override with `false`):
  `SELECT count(*) FROM tenant_capabilities WHERE override_flags::jsonb ? 'can_use_whatsapp' AND NOT can_use_whatsapp;`
  Compare with the tenants you know were revoked on purpose; decide the rest with the owner before migrating. (Read-only; run by the owner, not part of this PR.)
* **Deploy order: migrate first, then the new code.** New code on an un-migrated database fails every entitlements lookup (`subscription_plans.can_use_whatsapp` does not exist). Old code on a migrated database is safe.
* **Nothing turns WhatsApp on by itself.** `WHATSAPP_ENABLED` (default false) is the kill switch; production stays dark until the production sidecar exists and the owner sets it. Set `PLATFORM_ALERT_EMAILS` (comma separated) in the same release, and `WHATSAPP_MAX_CONNECTIONS` to what the host can carry.
* Review plan `features` after migrating: the WARNING names tiers whose WhatsApp line was hand-edited. A fresh database built only from migrations warns for `growth` (as for ent10); the seed then writes the right strings.
* Roll back: `alembic downgrade -1` restores the old feature strings, sets the stored flag back to false for tenants **without** an override key (a grant that was set by raw SQL without an override is lost too), and drops the plan column. Roll code and migration back together.

## Follow-ups
* Dashboard (separate task): see the list in the PR description / FE guide section 1-5.
* Admin can neither see nor clear a `can_use_whatsapp` override (`override_flags` is not in the platform response).
* Contact exclusion list (T15) and the Terms wording remain separate follow-ups.
* A capacity alert for the sidecar's session count (`sessions` / `max_sessions`) is not included; only memory and the backend's live count are.
