# WhatsApp default-on (plan-driven) + capacity alert: implementation plan

Branch `feature/whatsapp-default-on` -> `develop`. Decision change by the owner (2026-10-29): WhatsApp is
available to every tenant by plan; the 2026-10-28 "platform grant" model (`can_use_whatsapp` default false,
granted one tenant at a time) is replaced. The global kill switch `WHATSAPP_ENABLED` (default false) is
untouched and is what keeps WhatsApp dark in production until the production sidecar exists.

## Facts found while reading the code
* `can_use_whatsapp` exists only on `tenant_capabilities` (`TENANT_ONLY_CAPABILITY_FIELDS`), never on
  `subscription_plans`; `FEATURES["whatsapp"]` is `limit=max_whatsapp_numbers, grant=can_use_whatsapp`.
* A platform-admin edit (`PUT /platform/tenants/{id}/capabilities`, `TenantService.update_capabilities`)
  stores each explicitly sent field in `override_flags` (`{field: true}`). The resolver already keeps the
  stored value for any field listed there, so an admin-sent `true` or `false` survives as long as the
  field is also a plan column. Existing hand-granted tenants (staging) already carry the override.
* `whatsapp_inbound._tenant_may_use` reads the stored `tenant_capabilities.can_use_whatsapp` directly, not
  the resolver; `whatsapp_service._require_enabled` uses `entitlements.value("can_use_whatsapp")`.
* The backend never polls the sidecar `/health` today (`{sessions, max_sessions, rss_mb, rss_refuse_mb}`,
  `whatsapp-sidecar/src/manager.mjs`), so there is no status poll to reuse: the alert adds one arq cron
  (every 5 minutes) plus a cheap count-only check after a successful connection create.
* There is no operator-email setting; `EmailService._send_email` is blocking SMTP (30 s timeout).
* Alembic head is `ref10referral`.

## Rulings (cost if wrong in the PR description)
1. `can_use_whatsapp` becomes a plan column (true for all five tiers) and leaves `TENANT_ONLY_CAPABILITY_FIELDS`.
2. Backfill sets `tenant_capabilities.can_use_whatsapp` from the plan only where `override_flags` has no key for it.
3. `whatsapp.enabled` in `GET /tenant/entitlements` = plan/override flag AND limit != 0 AND not lapsed (the generic
   rule); the kill switch is NOT folded into it. Additive `platform_available` (the kill switch) on the
   `whatsapp` entry only, so the dashboard can hide the section while production is dark.
4. Inbound/outbound `_tenant_may_use` goes through the resolver value (`can_use_whatsapp`), like the create path.
5. Feature string `WhatsApp (beta, on request): N` -> `WhatsApp (beta): N` (it is no longer on request).
6. Capacity alert conditions: `connections` (live >= 80% of `WHATSAPP_MAX_CONNECTIONS`) and `sidecar_memory`
   (sidecar `rss_mb` >= 80% of `rss_refuse_mb`); Redis `SET NX EX 21600` per condition; recipients from the new
   `PLATFORM_ALERT_EMAILS` (comma separated, empty = alert logged, nothing sent, dedupe key not consumed).
7. Tenant-facing 503 body: `{"code":"capacity_exceeded","message":"WhatsApp is temporarily at capacity. Please try again later."}`
   from one constant used at all four raise sites.

## Tasks
- [ ] **T1 Plan-driven access** (Sonnet implements; Opus reviews migration/backfill/resolver)
  - [ ] `subscription_plans.can_use_whatsapp` column + model + schemas + catalog (true on all tiers) + feature string
  - [ ] `TENANT_ONLY_CAPABILITY_FIELDS` / parity test; `FEATURES["whatsapp"]` flag instead of grant; `platform_available`
  - [ ] `_tenant_may_use` via resolver; `_require_enabled` unchanged semantics; downgrade-conflict check on the flag
  - [ ] Migration `wa30whatsappdefaulton` (down_revision `ref10referral`): column, plan data, features, tenant backfill, reversible
  - [ ] Tests: override true/false respected, backfill idempotent, new-tenant default, admin revoke survives, entitlements payload, parity, migration up/down/up on scratch Postgres
- [ ] **T2 Capacity alert + stable 503** (Sonnet implements, in parallel with T1)
  - [ ] `PLATFORM_ALERT_EMAILS` setting, `.env.example`
  - [ ] `LinkedDeviceAdapter.get_health`; `app/services/whatsapp_capacity.py` (evaluate, Redis dedupe, send)
  - [ ] Email template + `EmailService` method; arq cron + registration; after-create hook (fire and forget)
  - [ ] One capacity message constant at all raise sites
  - [ ] Tests: thresholds, dedupe, empty recipients, SMTP failure never raises, create unaffected by alert failure
- [ ] **T3 Docs and live captures** (me)
  - [ ] Local live run (scratch Postgres 15 + Redis 7, spare ports), captures to `docs/fe-integration-captures/whatsapp-default-on/`
  - [ ] FE guides (entitlements, whatsapp-channel) with the captures, SOP `docs/sop/whatsapp_default_on.md`, `docs/sop/plan_entitlements.md`, master checklist
- [ ] **T4 Verify and ship**
  - [ ] Opus review of the migration/backfill/resolver, then of the whole branch
  - [ ] CI parity: `ruff check app/`, `black --check app/`, full `pytest tests/`, SQLite regression job
  - [ ] Push, PR to `develop`, no merge
