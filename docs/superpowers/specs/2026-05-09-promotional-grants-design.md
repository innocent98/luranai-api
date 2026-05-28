# Promotional grants — extended onboarding & re-engagement

**Date:** 2026-05-09
**Status:** Design — pending approval
**Repos affected:** `luran` (backend, primary), `luran-dashboard` (admin UI), `luran-docs` (operator docs)

---

## 1. Context & motivation

Luran currently has 7 registered tenants. 5 of them are on `subscription_status='trial_expired'` and have not converted; 0 are paying. The product is pre-PMF and the immediate bottleneck is **feedback velocity from integrated tenants**, not revenue capture. Every tenant who installs Luran into a real workflow and uses it for weeks is worth more right now than the LTV of a single Lite subscription.

The standard 30-day trial — restricted to the trial tier — is too short and too constrained for prospects to integrate Luran meaningfully and form a judgment about whether the product is worth paying for. We need a longer, richer onboarding window, plus a way to re-engage the 5 tenants who already churned out.

**This feature is not a marketing stunt.** It is the way the product behaves while we look for product-market fit. Once we have data on conversion and engagement, we'll decide when to wind it down and return to a standard paid trial.

## 2. Goals

1. Allow platform admins to grant time-bounded elevated subscription tiers (Lite / Growth / Professional) to selected tenants, primarily for re-engaging churned tenants.
2. Auto-grant the same offer to brand-new tenants on signup during a configurable campaign window, replacing the standard 30-day trial.
3. Drive conversion: clear reminder emails before grant expiry, automatic conversion detection if a tenant pays during the grant.
4. Produce clean cohort analytics so we can answer "how did the launch campaign perform?" by source (admin-bulk vs auto-signup) and by campaign code.
5. Wind down cleanly: when the campaign ends, flip a config flag and new signups return to the standard trial without disturbing existing grants.

## 3. Non-goals (v1)

- **No public claim flow / promo codes.** Auto-grant on signup applies universally during the campaign window. No code entry, no abuse-prevention surface for code sharing.
- **No grant extension UI.** To extend, admins revoke and re-grant. Cleaner audit; can revisit if it proves to be a real workflow.
- **No tenant-facing in-app banner** (e.g. "your Pro access ends in X days"). Email reminders only for v1.
- **No multi-tier campaigns** (e.g. Lite to one cohort, Pro to another within the same campaign code). One tier per campaign — run two campaigns if needed.
- **No advanced cohort filters.** v1 supports `subscription_status IN (...)` and `churned_within_days`. Knowledge-source-count and previous-tier filters are deferred.
- **No granted-tier above Professional.** Enterprise tier remains contact-only and is not a valid `granted_tier`.

## 4. Decisions log

| Decision | Choice | Reason |
|---|---|---|
| Cohort for re-engagement | Existing churned tenants (`trial_expired`, `canceled`) | Win-back the 5 tenants who already churned. |
| Revert behavior | Snapshot previous state at grant time, restore exactly at expiry | Faithful revert; no race with concurrent admin overrides; no recomputation from external state. |
| Reminder cadence | T-14, T-7, T-1 emails; hard revert at T+0 | Mirrors existing trial-reminder pattern; conversion-optimized. |
| Mid-grant payment handling | Paystack webhook auto-cancels grant on payment success (`status='converted'`) | No double-revert; clean conversion attribution. |
| Stacking | Skip already-granted by default; explicit `force_replace_active` flag | DB-level partial unique index enforces "one active grant per tenant"; bulk re-imports are safe. |
| Manual revocation | Allowed, reason required, immediate revert | Recovery from accidental grants and abuse cases. |
| Duration | Configurable per campaign, default 90 days, bounded 14-180 | Forward-compatible without code changes. |
| Implementation approach | New `promotional_grants` table + ARQ worker | Clean separation, faithful audit, mirrors the existing trial-reminder worker pattern. |
| Two creation paths | Admin-bulk (existing churned) + auto on signup (new tenants) | Single underlying model; one campaign code unifies analytics across both cohorts. |
| Auto-signup revert target | `trial_expired` (same paywall as today) | Maximum conversion pressure; consistent with the only paywall users have already experienced. |
| Tier choice | Single tier per campaign (Lite / Growth / Professional) | Matches marketing reality; analytics stay simple. |
| Auth | `get_current_super_admin` only | Same gate as existing override endpoint. |
| Bulk cap | 5,000 tenants per request | Larger campaigns split across multiple calls under the same campaign code (idempotency index handles it). |

## 5. Data model

One new table: `promotional_grants`. No changes to existing tables (the `subscription_history.action` column is already a free-form `String(50)` and accepts the new action types additively).

```sql
CREATE TABLE promotional_grants (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    campaign_code               VARCHAR(64) NOT NULL,
    campaign_name               VARCHAR(160),
    granted_tier                VARCHAR(50) NOT NULL,            -- 'lite' | 'growth' | 'professional'
    duration_days               INTEGER NOT NULL,                -- 14..180
    grant_starts_at             TIMESTAMPTZ NOT NULL,
    grant_ends_at               TIMESTAMPTZ NOT NULL,

    -- Pre-grant snapshot (for faithful revert)
    previous_tier               VARCHAR(50) NOT NULL,
    previous_status             VARCHAR(50) NOT NULL,
    previous_billing_cycle      VARCHAR(20),
    previous_trial_ends_at      TIMESTAMPTZ,
    previous_subscription_ends_at TIMESTAMPTZ,

    -- Lifecycle
    status                      VARCHAR(20) NOT NULL,            -- 'active' | 'expired' | 'revoked' | 'converted'
    source                      VARCHAR(20) NOT NULL,            -- 'admin_bulk' | 'admin_single' | 'auto_signup'
    expired_at                  TIMESTAMPTZ,
    revoked_at                  TIMESTAMPTZ,
    converted_at                TIMESTAMPTZ,
    revoke_reason               TEXT,

    -- Audit
    granted_by_admin_id         UUID REFERENCES platform_admins(id),  -- null for auto_signup
    granted_by_admin_name       VARCHAR(255),
    reason                      TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Operational rules enforced at the DB layer
CREATE UNIQUE INDEX promo_grants_one_active_per_tenant
    ON promotional_grants(tenant_id) WHERE status = 'active';

CREATE UNIQUE INDEX promo_grants_idempotency
    ON promotional_grants(tenant_id, campaign_code) WHERE status IN ('active', 'converted');

CREATE INDEX promo_grants_expiry_scan
    ON promotional_grants(grant_ends_at, status) WHERE status = 'active';

CREATE INDEX promo_grants_campaign_lookup
    ON promotional_grants(campaign_code, status);

CREATE INDEX promo_grants_tenant_lookup
    ON promotional_grants(tenant_id, created_at DESC);
```

A second small singleton table holds the auto-signup configuration. There is no existing `platform_settings` store in the codebase, so we add a dedicated typed table rather than a generic key/value bag — the values here are mostly typed (bool, str, int, timestamp) and a typed table keeps the worker and signup paths from string-parsing.

```sql
CREATE TABLE auto_signup_grant_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    campaign_code   VARCHAR(64),
    campaign_name   VARCHAR(160),
    granted_tier    VARCHAR(50),                  -- 'lite' | 'growth' | 'professional'
    duration_days   INTEGER,                       -- 14..180
    valid_until     TIMESTAMPTZ,                   -- NULL = no end date
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by_admin_id UUID REFERENCES platform_admins(id),
    updated_by_admin_name VARCHAR(255)
);

-- Singleton invariant: only one row ever exists.
CREATE UNIQUE INDEX auto_signup_grant_config_singleton
    ON auto_signup_grant_config((true));
```

The Alembic migration seeds a single row with `enabled=false`. All service reads/writes target that row; `PUT /config/auto-signup` is an UPDATE not an INSERT.

**Snapshot semantics by source:**

| Source | `previous_tier` | `previous_status` | `previous_trial_ends_at` |
|---|---|---|---|
| `admin_bulk` / `admin_single` | The tenant's actual current tier (typically `trial`) | The tenant's actual current status (e.g. `trial_expired`) | Tenant's current `trial_ends_at` |
| `auto_signup` | `'trial'` | `'trial_expired'` | `now()` (set as if the trial just ended) |

The auto-signup snapshot is intentionally synthetic so the revert restores the tenant directly to `trial_expired` without going through `trialing → expiry check → trial_expired`.

**The two partial unique indexes are load-bearing:**
- `promo_grants_one_active_per_tenant` makes "tenant has two simultaneous active grants" structurally impossible.
- `promo_grants_idempotency` lets the bulk-grant endpoint be re-run safely. Marketing re-imports the cohort? Tenants already on the campaign (active or converted) are skipped at insert time with a unique-violation that the service catches and translates to "skipped".

## 6. API surface

All under `/api/v1/platform/promotional-grants`, gated by `get_current_super_admin` + `platform_admin_rate_limit` (same as existing override endpoint).

### `POST /preview`

Preview the cohort matched by filters without writing.

```json
Request:
{
  "filters": {
    "subscription_statuses": ["trial_expired", "canceled"],
    "churned_within_days": 90
  },
  "exclude_already_granted": true
}

Response (200):
{
  "matched_count": 5,
  "sample_tenants": [
    {"tenant_id": "...", "name": "...", "display_name": "...", "subscription_status": "trial_expired", "churned_at": "..."}
  ],
  "preview_token": "opaque-token-tied-to-this-cohort-snapshot"
}
```

The `preview_token` is opaque, signed with `SECRET_KEY`, has a 30-minute TTL, and encodes the matched tenant IDs. The bulk-create endpoint requires it. This prevents the "filter changed between preview and confirm" footgun cleanly: admin previews 47 tenants, signs up 3 more in the next minute, admin confirms — the bulk operation runs against the 47 they saw, not the 50 the live filter now returns.

### `POST /`

Bulk create grants from a previewed cohort.

```json
Request:
{
  "preview_token": "...",          -- required, must match a recent preview
  "campaign_code": "WINBACK_MAY_2026",
  "campaign_name": "May 2026 Win-Back",
  "granted_tier": "professional",
  "duration_days": 90,
  "force_replace_active": false,
  "reason": "Q2 win-back campaign per marketing plan"
}

Response (200):
{
  "campaign_code": "WINBACK_MAY_2026",
  "granted": 5,
  "skipped_already_granted": 0,
  "failed": 0,
  "grant_ids_sample": ["..."]
}
```

Validation:
- `granted_tier` ∈ `{lite, growth, professional}`. `enterprise` rejected.
- `duration_days` ∈ `[14, 180]`.
- `reason` length ≥ 10.
- `campaign_code` regex `^[A-Z][A-Z0-9_]{2,63}$`.
- Cohort size ≤ 5,000. Larger campaigns must be split.

Implementation: chunked transactions of 500 grants each. Each grant creation goes through `PromotionalGrantService.create_grant()` which applies the elevation (mutates tenant tier/status/billing_cycle, recomputes capabilities, writes `SubscriptionHistory` with `action='promo_granted'`).

### `POST /{grant_id}/revoke`

```json
Request:
{
  "reason": "Tenant requested cancellation"
}

Response (200):
{
  "grant_id": "...",
  "status": "revoked",
  "tenant_id": "...",
  "reverted_to_tier": "trial",
  "reverted_to_status": "trial_expired"
}
```

Revoke immediately runs the revert (restore snapshot, recompute capabilities, write `SubscriptionHistory` with `action='promo_revoked'`).

### `GET /campaigns`

List all campaigns with aggregate stats.

```json
Response:
{
  "campaigns": [
    {
      "campaign_code": "LAUNCH_2026",
      "campaign_name": "Public Launch 2026",
      "granted_tier": "professional",
      "duration_days": 90,
      "first_grant_at": "2026-05-09T10:00:00Z",
      "stats": {
        "total": 47,
        "active": 30,
        "converted": 8,
        "expired": 7,
        "revoked": 2,
        "by_source": {"admin_bulk": 5, "auto_signup": 42}
      }
    }
  ]
}
```

### `GET /campaigns/{campaign_code}`

Returns the campaign aggregate plus paginated list of grants.

### `GET /tenant/{tenant_id}`

Returns all grants for a single tenant ordered by `created_at desc`. Used by the tenant detail page to surface the active grant and grant history.

### `GET /config/auto-signup`

Returns current auto-signup configuration (read-only mirror of the config flags).

### `PUT /config/auto-signup`

```json
Request:
{
  "enabled": true,
  "campaign_code": "LAUNCH_2026",
  "campaign_name": "Public Launch 2026",
  "tier": "professional",
  "duration_days": 90,
  "valid_until": "2026-08-09T00:00:00Z"
}
```

Updates the auto-signup config. Persisted as platform settings rows (not env vars) so admins can flip them without redeploys. Toggling logged to `subscription_history` with a tenant-less platform-event row (or a separate `platform_events` table — see open question 12.1).

## 7. Worker lifecycle

One new ARQ job: `process_promotional_grant_lifecycle`. Scheduled daily at 09:00 UTC alongside `check_trial_expiration_3days` (luran/app/workers/document_processor.py:409). Two sequential passes per run:

### Pass 1 — Expiry reminders

For each window in `[14, 7, 1]`:
- Query: `SELECT * FROM promotional_grants WHERE status='active' AND grant_ends_at::date = (now() + interval 'N days')::date`.
- For each grant, call `EmailNotificationService.send_promo_expiration_reminder(grant_id, days_remaining=N)`.
- Email template `promo_expiring.html` parameterized by `days_remaining`, `granted_tier`, `tenant_display_name`, prefilled subscribe URL with the current granted tier preselected.
- Notification recorded in `email_notifications` with `notification_type='promo_expiring_Nd'` (e.g. `promo_expiring_14d`).

### Pass 2 — Expiry revert

- Query: `SELECT * FROM promotional_grants WHERE status='active' AND grant_ends_at < now()`.
- For each grant, in a per-grant transaction:
  1. Restore tenant fields from the snapshot:
     - `tenant.subscription_tier = grant.previous_tier`
     - `tenant.subscription_status = grant.previous_status`
     - `tenant.billing_cycle = grant.previous_billing_cycle`
     - `tenant.trial_ends_at = grant.previous_trial_ends_at`
     - `tenant.subscription_ends_at = grant.previous_subscription_ends_at`
  2. Recompute capabilities: `_update_capabilities_with_overrides(tenant_id, previous_tier, respect_overrides=False)`.
  3. Mark `grant.status='expired'`, `grant.expired_at = now()`.
  4. Write `SubscriptionHistory` with `action='promo_expired'`, `event_metadata={"grant_id": ..., "campaign_code": ...}`.
  5. Send `promo_expired.html` email with subscribe CTA.

Pass 2 is **idempotent** — re-running on a grant already `expired` is a no-op (the WHERE clause filters it out).

### Conversion detection (Paystack webhook)

In `webhook_handler_service.py`, inside `handle_subscription_payment_success`, after the existing tenant subscription update:

```python
active_grant = (
    db.query(PromotionalGrant)
    .filter(
        PromotionalGrant.tenant_id == tenant.id,
        PromotionalGrant.status == "active",
    )
    .first()
)
if active_grant:
    active_grant.status = "converted"
    active_grant.converted_at = datetime.now(UTC)
    db.add(SubscriptionHistory(
        tenant_id=tenant.id,
        action="promo_converted",
        previous_tier=active_grant.granted_tier,
        new_tier=tenant.subscription_tier,
        previous_status="active",
        new_status=tenant.subscription_status,
        changed_by_type="webhook",
        changed_by_name="paystack",
        reason=f"Tenant converted during {active_grant.campaign_code} grant",
        event_metadata={"grant_id": str(active_grant.id), "campaign_code": active_grant.campaign_code},
    ))
```

Pass 2 query filters `status='active'`, so a `converted` grant is skipped at expiry — no double-revert.

## 8. Auto-signup creation path

A new `AutoSignupGrantConfigService` reads and writes the singleton row from §5. In the tenant signup service (the path that creates the `Tenant` row and sets the standard 30-day trial), after the standard trial setup:

```python
config = AutoSignupGrantConfigService(db).get()
if config.enabled and (
    config.valid_until is None
    or datetime.now(UTC) < config.valid_until
):
    promo_service.create_grant(
        tenant_id=tenant.id,
        campaign_code=config.campaign_code,
        campaign_name=config.campaign_name,
        granted_tier=config.granted_tier,
        duration_days=config.duration_days,
        source="auto_signup",
        granted_by_admin_id=None,
        granted_by_admin_name="system",
        reason=f"Auto-grant on signup for campaign {auto_grant_config.campaign_code}",
        # Synthetic snapshot
        snapshot_override={
            "previous_tier": "trial",
            "previous_status": "trial_expired",
            "previous_trial_ends_at": datetime.now(UTC),
            "previous_billing_cycle": None,
            "previous_subscription_ends_at": None,
        },
    )
```

The signup welcome email and dashboard onboarding state both reflect that the tenant is on the campaign tier for `duration_days` days, with explicit copy: "Your free Professional access is for 90 days. After that, you'll need to subscribe to continue."

When `config.enabled` flips to `false` (or `valid_until` passes), new signups skip this block and land on the standard 30-day trial. Existing auto-signup grants continue running their lifecycle — they'll expire normally per the worker.

## 9. Dashboard UI (luran-dashboard)

### New page: `/platform-admin/promotional-grants`

App Router server component for the campaign list (Server Component fetching from `INTERNAL_API_URL`). "Create new campaign" CTA opens a 4-step wizard (Client Component for form state):

1. **Identity** — campaign_code (validated against regex client-side), campaign_name, reason.
2. **Offer** — granted_tier (Lite/Growth/Professional radio), duration_days slider (14-180, default 90).
3. **Cohort** — filter form: status checkboxes, "churned within N days" input. "Preview" button calls `POST /preview`, displays match count + sample. Stores `preview_token` in form state.
4. **Confirm** — summary card: "Granting **Professional** for **90 days** to **47 tenants** under campaign **WINBACK_MAY_2026**." Type-the-code-to-confirm input. Submit calls `POST /` with the `preview_token`.

Loading/empty/error states all designed per shadcn skeleton conventions; the loading skeleton matches the final layout for each step.

### Auto-signup config panel

Separate page or tab at `/platform-admin/promotional-grants/config`. Form bound to `GET /config/auto-signup` and `PUT /config/auto-signup`. Toggle (enabled/disabled), campaign code input, tier select, duration slider, valid-until date picker. Submit shows confirmation modal: "Auto-signup grants will be enabled. Every new tenant signup until 2026-08-09 will receive 90 days of Professional. Continue?"

### Tenant detail page (existing)

New "Active promotional grant" card if applicable, showing:
- Campaign code + name
- Granted tier
- Days remaining (`grant_ends_at - now()`)
- "Revoke grant" button (modal with reason input → `POST /{grant_id}/revoke`)
- Grant history list (collapsed by default, expand to see expired/revoked/converted grants)

Server Component fetched alongside the existing tenant detail; revalidates on revoke via `revalidateTag('tenant-grants')`.

## 10. Migration strategy

### Backend

Single Alembic migration:
1. Create `promotional_grants` table with all columns and indexes from §5.
2. Create `auto_signup_grant_config` table with the singleton unique index from §5.
3. Seed `auto_signup_grant_config` with one row, `enabled=false`. Admin enables it via the API after deploy.
4. No backfill required at migration time for grants — they are created going forward only.

### Existing 5 trial_expired tenants

Migrating these tenants to the new flow is a **runtime operation**, not a schema migration. After deploy, an admin runs:
1. `POST /preview` with filters `{statuses: ["trial_expired"], churned_within_days: 365}` → returns the 5 tenants.
2. `POST /` with the preview token, `campaign_code='WINBACK_MAY_2026'`, `granted_tier='professional'`, `duration_days=90`, reason explaining the campaign.
3. Email broadcast goes out via Mailchimp (or chosen tool) referencing the same campaign code.

### Auto-signup activation

After the bulk-grant for the 5 tenants is verified, admin enables auto-signup via `PUT /config/auto-signup` with the same campaign code. New signups from that moment forward get the auto-grant.

### Wind-down

When the campaign ends:
1. `PUT /config/auto-signup` with `enabled: false`.
2. New signups return to the standard 30-day trial.
3. Existing grants run their course; the worker handles reminders and expiry reverts as designed.

## 11. Testing strategy

### Backend unit tests

- `PromotionalGrantService.create_grant()`:
  - Single grant with admin source: snapshot captured correctly, tenant elevated, history written.
  - Auto-signup grant: synthetic snapshot applied, tenant elevated.
  - Stacking: second grant for same tenant + same campaign rejected (idempotency); second grant for same tenant + different campaign rejected unless `force_replace_active`.
  - Invalid tier (enterprise) rejected.
  - Duration out of range rejected.
- `PromotionalGrantService.revoke_grant()`:
  - Active grant: revert applied, history written, status='revoked'.
  - Already expired: rejected with explicit error.
- `process_promotional_grant_lifecycle` worker (use `pytest-freezegun`):
  - Reminder windows: T-14, T-7, T-1 each fire on the right day; T-13, T-12, ..., T-2 do not.
  - Re-running on the same day does not double-send (idempotency on `email_notifications.notification_type` + tenant_id + date).
  - Expiry pass: grant past `grant_ends_at` is reverted; snapshot restored; status='expired'.
  - Expiry pass on already-expired grant: no-op.
  - Converted grant is not reverted at expiry.

### Endpoint integration tests

- All endpoints reject non-super-admin auth.
- `POST /preview` returns matching tenants and a valid `preview_token`.
- `POST /` with a stale or forged `preview_token` returns 400.
- Bulk cap of 5,000 enforced.
- Idempotency: running the same bulk twice returns `skipped_already_granted` for the second.
- `force_replace_active`: replaces existing active grant when set, errors when not.

### E2E test (against test DB)

1. Create a tenant with status `trial_expired`.
2. Preview cohort, bulk-grant for `WINBACK_TEST` campaign.
3. Assert tenant tier is `professional`, capabilities recomputed.
4. Fast-forward time to T-1, run worker, assert reminder email sent.
5. Fast-forward past `grant_ends_at`, run worker, assert tenant reverted to `trial_expired`, capabilities back to trial defaults.

### Conversion path test

1. Create a tenant, bulk-grant it.
2. Simulate Paystack `subscription.create` webhook with successful payment.
3. Assert grant `status='converted'`, tenant on the paid plan.
4. Run worker pass-2 — assert no revert.

### Manual verification

- Run the dashboard wizard end-to-end; confirm the 4 steps work, the preview/confirm token contract holds, the campaign list reflects the new campaign.
- Toggle auto-signup config; create a brand-new test tenant via signup; confirm they're auto-elevated.

## 12. Operational notes

### Email templates

- `templates/email/promo_expiring.html` — subject: "Your Professional access ends in {{days_remaining}} days". Includes one-click subscribe URL with tier preselected.
- `templates/email/promo_expired.html` — subject: "Your Professional access has ended". Includes subscribe URL and brief copy on what changed (lower limits).
- `templates/email/promo_granted.html` — subject: "We've added 90 days of Professional to your account" (sent only for `admin_bulk` source — auto-signup gets handled by the welcome email which already covers this).

Templates rendered via Jinja2 (existing pattern in `email_notification_service.py`); no raw HTML injection.

### Logs and alerts

- Worker run summary log: `"Processed promo grants: reminders_sent=X, expired=Y, errors=Z"`.
- Sentry alert if worker errors > 5 per run.
- Daily digest email to platform admins: number of new grants, conversions, revocations, upcoming expiries.

### Security

- All endpoints super-admin-only via `get_current_super_admin` (existing dep).
- Rate-limited via `platform_admin_rate_limit` (existing).
- `preview_token` signed with `SECRET_KEY`, 30-minute TTL — prevents cohort-drift attacks and replay outside the intended timeframe.
- `reason` field on grant creation and revocation captured in audit log; admin actions traceable end-to-end.
- `granted_by_admin_id` is nullable to accommodate `auto_signup` source; queries that filter on admin must allow null.
- No untrusted user input flows into SQL or HTML rendering paths — all parameters are typed via Pydantic and rendered via Jinja with autoescape.

## 13. Open questions / follow-ups

1. **Auto-signup config audit:** the singleton `auto_signup_grant_config` table carries `updated_at`, `updated_by_admin_id`, `updated_by_admin_name` — that's enough for v1 to show "who last toggled the auto-signup, when". Multi-revision history (every change, not just last) would require a separate audit table; defer to a follow-up if it becomes a real need.
2. **Granted-tier-above-current vs. below-current:** for re-engagement, the granted tier is always above `trial`, so the elevation always strictly expands capabilities. For future use, granting a tier *below* the tenant's current paid tier would be a downgrade and is out of scope.
3. **Tenant data preservation across revert:** when a grant ends and the tenant reverts to `trial_expired`, their data (knowledge sources, conversations, flows) is retained but read-only access is unclear. The existing 402 wall blocks API access entirely; we'll inherit that behavior. UX improvement (a "your data is preserved, subscribe to access" view) is a separate spec.
4. **Per-source campaign attribution at the marketing layer:** the campaign code is the join key, but Mailchimp's broadcast and the auto-signup landing page need to use the same code. Coordination is operational; no engineering surface required.

## 14. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Worker fails silently and grants don't expire | Low | Sentry alerting on worker errors; daily digest email surfaces aging active grants. |
| Bulk grant partially applies (5,000 mid-batch failure) | Low | Per-chunk transactions of 500; idempotency index means re-running picks up where it left off. |
| Auto-signup config mis-set leaks 180-day Pro to all signups | Medium | `PUT /config/auto-signup` requires confirmation modal in UI; bounded `duration_days`; daily digest surfaces unexpected grant volume. |
| Conversion webhook fires before grant create webhook on race | Very low | Both writes go through the same DB session; conversion check is a simple SELECT on existing grants. |
| Email templates break / spam folder | Medium | Send via existing `EmailNotificationService` pipeline (already validated for trial reminders); preview templates before launching. |

---

## 15. Acceptance criteria

This feature ships when:
- [ ] All endpoints in §6 implemented and tested per §11.
- [ ] Worker job in §7 deployed, scheduled, and verified via fast-forwarded e2e test.
- [ ] Conversion detection in Paystack webhook implemented and tested.
- [ ] Auto-signup creation path in §8 implemented, behind a config flag default-off.
- [ ] Dashboard pages in §9 ready, including 4-step wizard, auto-signup config panel, tenant-detail card.
- [ ] Alembic migration applied to dev/staging, then prod after acceptance.
- [ ] First campaign (`WINBACK_MAY_2026`) granted to the existing 5 trial-expired tenants.
- [ ] Auto-signup enabled with `LAUNCH_2026` campaign code (or whatever code is chosen).
- [ ] Email reminders verified to send on a fast-forwarded test grant.
- [ ] Operator runbook entry added to `luran-docs` covering: how to create a campaign, how to enable/disable auto-signup, how to revoke an individual grant, where to find conversion stats.
