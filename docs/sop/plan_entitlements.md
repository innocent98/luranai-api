# SOP: Plan entitlements (plan gates, one plan definition, Lite at 15,000 naira)

## What shipped
Branch `feature/plan-entitlements-backend` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges). Prod untouched.

1. **One plan definition**: `app/services/plan_catalog.py`. The seed, the migration and the tests all agree with it; the `features` marketing strings are generated from the capability values.
2. **New gates, plan-driven**: `sales_close` (meeting booking + quotes), `crm_notifications` (Slack + CRM webhook), `screen_recording`, `ratings_export` (admin ratings breakdown + CSV), plus plan-driven AI actions, lookup chains, webhooks, analytics and WhatsApp numbers (voice stays a platform grant; **WhatsApp became plan-driven on 2026-10-29**, see `docs/sop/whatsapp_default_on.md`).
3. **One resolver**: `get_entitlements(tenant)` in `app/services/entitlements.py`. Every enforcement point and the endpoint use it.
4. **`GET /api/v1/tenant/entitlements`** per the 2026-10-28 contract, and the **403 `feature_not_in_plan`** gate error.
5. **Alembic `ent10entitlements`**: columns on both tables, plan data, tenant backfill. Idempotent and reversible.
6. **Lite price** 1,500,000 kobo/month (annual 15,000,000), Lite AI actions 5 -> 2.
7. **New** `GET /api/v1/reviews/session/export` (CSV) so the `ratings_export` feature has something to gate (see Decisions).
8. Downgrade conflicts for sales options, Slack/CRM and WhatsApp numbers.

**Conversation limit meter** (what counts as a conversation, the 6-hour same-conversation window, the single shared check): `docs/sop/conversation_counting.md` (2026-10-29). Plan *numbers* are unchanged by it.

FE contract: `docs/fe-integration-guide-entitlements.md` (all payloads from `docs/fe-integration-captures/entitlements/`). Master checklist: "Plan entitlements".

## Why
Plans differed only by volume plus five flags; the sales agent, Slack/CRM, recording, ratings export and WhatsApp number counts were not tied to plans. Limits lived in two places (`subscription_plans` and `tenant_capabilities`) and had drifted before (2026-10-16 plan-string drift; a staging seed reverting limits). The matrix and the Lite price were approved on 2026-10-28 (spec `docs/superpowers/specs/2026-10-27-pricing-and-entitlements-design.md`). Now is the cheapest moment to add gates: prod has no Lite or Professional tenant and zero payments.

## How

### Resolution (`get_entitlements`)
Per capability, in order:
1. the **plan row** of the tenant's effective tier (an active promotional grant's tier wins; the grant service also writes the tier onto the tenant, so they agree),
2. then the tenant's **`override_flags`**: a field listed there keeps its stored `tenant_capabilities` value (this is what a platform-admin capability edit records),
3. no plan row (empty plans table, unknown tier) -> stored capability values; neither -> everything off.

4. **Lapsed accounts** (the accounts the staff API answers 402 for: status `trial_expired`/`canceled`/`past_due`, a `trialing` account past `trial_ends_at`, or an `active` one past `subscription_ends_at`; `is_lapsed` mirrors `require_active_subscription` without its side effects, because the stored status only flips on the next 402 check) have every feature off whatever the tier. Added after the Opus review: several gated routes (ratings list/export, sales config save, public meeting endpoints) have no 402 check of their own, so an expired trial would otherwise have kept the trial's gates. A promotional grant whose end date has passed stops counting at once: the tier resolves from the grant's `previous_tier`, and because the grant wrote its end date onto the tenant the account reads as lapsed until the expiry worker reverts it (a second automated security review pointed out the state-drift window; this closes it).

A stored capability that differs from the plan **without** an override entry is ignored: a stale copy cannot widen or narrow a plan (the drift incident). `tenant_capabilities` stays as a synced copy for code that reads a single row.

`min_tier` is computed from the plan rows as the lowest tier from which the feature stays included for every higher tier (`sales_close`: professional; `analytics`: lite; `ai_actions`, `whatsapp`: trial; voice: null).

### One writer for tier syncs
`apply_plan_to_capabilities` (entitlements.py) replaces two near-identical copies in `SubscriptionService` and `PlatformSubscriptionService`; upgrade, downgrade, webhook-confirmed payment, platform override and promotional grant/expiry all go through it. It copies exactly the columns both tables share (`plan_capability_fields()`), so a new shared column needs no edit here. New tenants (`TenantService.create_tenant`, `CompanyRegistrationService`) get their capabilities from the trial plan row via `new_tenant_capabilities` instead of two hard-coded literals.

### Parity (drift guard)
`SubscriptionPlan.get_capabilities()`, `to_dict()` and `PlanService.update_plan` now derive from the table's columns (no hand lists). `tests/entitlements/test_plan_parity.py` fails when: a plan column has no `tenant_capabilities` twin (or the reverse) outside the two explicit exception lists (`PLAN_ONLY_CAPABILITY_FIELDS`, `TENANT_ONLY_CAPABILITY_FIELDS`); types or nullability differ; the pricing/tenant schemas miss a column; the seeded rows differ from the catalog; a `features` string promises something the flags do not give; the latest plan migration froze different numbers than the catalog.

### Enforcement points (each has tests; each path was searched for a second way in)
| Feature | Where | Behaviour when not entitled |
|---|---|---|
| `sales_close` | `PATCH /sales/config` (`sales.py`) | 403 gate error when a save switches booking/quotes on (not for leaving an already-on option) |
| | public `GET /chat/meetings/availability`, `POST /chat/meetings` (`meetings.py`) | empty picker / `BOOKING_DISABLED` 403, identical to booking switched off |
| | chat sales branch (`chat.py`): `offer_meeting`, quote creation, QUOTE notification; the AI prompt (`SalesConfigView`) | silently skipped; lead capture, scoring and the email notice unchanged |
| `crm_notifications` | config save (as above); `NotificationService.dispatch` (no Slack/CRM job or log row); `send_slack_notification` / `sync_lead_to_crm` (a job queued before a downgrade is marked FAILED "plan does not include ..." and nothing is sent) | email and in-app notifications are never gated |
| `screen_recording` | `screen_recording_allowed` (used by video sign/register, public widget config, customer conversations) + chat send with a video attachment + admin widget config | existing `screen_recording_disabled` 403 / `allow_screen_recording: false` |
| `ratings_export` | `GET /reviews/session` (tenant admin looking beyond their own ratings), new `GET /reviews/session/export`, ratings fields of `/analytics/agents[/{id}]` | 403 gate error on the two review endpoints; analytics rating fields zeroed. Agent self-view (`/agent/me/ratings`, `/reviews/session` as agent) and rating collection untouched |
| `ai_actions` | `AutomationService._check_action_limit`, `ChatAutomationOrchestrator` | existing limit error / skip |
| `lookup_chains`, `analytics` | orchestrator, `check_analytics_permission` | existing behaviour, now read via the resolver |
| `whatsapp` | `WhatsAppService` create: plan/override flag `can_use_whatsapp` + plan number limit (`None` = unlimited) + the `WHATSAPP_ENABLED` kill switch (**was a platform grant until 2026-10-29**) | existing `whatsapp_not_enabled` / `connection_limit` |
| downgrade | `SubscriptionValidationService`: `sales_close`, `crm_notifications`, `whatsapp_numbers` violations | downgrade blocked with the standard `data_exceeds_limits` body |

Runtime paths never raise to the end customer; only config saves and staff-only endpoints use the gate error, which is registered in `app/main.py` as a flat 403 body.

### Decisions and trade-offs
* **Gate error shape**: a dedicated exception + app handler gives the flat contract body (not `{detail: ...}`) and is not 402.
* **"Enabling" means false -> true / empty -> set**: re-sending an option that is already on is allowed, so a downgraded tenant (or one whose promotion ended) can still save other settings. Runtime skips the behaviour instead. Alternative rejected: reject any save that contains a true gated value (it would lock the whole form).
* **Ratings export**: the codebase had no CSV endpoint and no dashboard use of `/reviews/session`. I added a small tenant-admin-only CSV endpoint (10,000-row cap, formula-neutralised comments) so the contract's `ratings_export` has an export to gate. The lead should confirm this is wanted; removing it leaves the breakdown gate intact.
* **Analytics ratings fields are zeroed, not 403**: the analytics page is a separate gate and should keep working on Lite.
* **`widget_allow_screen_recording` (the tenant's own switch)** is not rejected on save: it defaults to on for everyone, so rejecting it would break unrelated saves; the runtime gate and `can_use_screen_recording` carry the decision.
* **`whatsapp.enabled` reported the grant** (decision of 2026-10-28). **Reversed 2026-10-29:** it now follows the plan/override like every other feature (true for all tiers unless an admin revoked it), and the entry gains `platform_available` (the `WHATSAPP_ENABLED` kill switch). See `docs/sop/whatsapp_default_on.md`.
* **Seeded Trial includes the sales features, Slack/CRM, recording, ratings export** (contract); `min_tier` for them is `professional` (what the tenant keeps after the trial).
* **`tenant_capabilities.max_knowledge_mb` made nullable** (found by the parity test): the Enterprise plan has unlimited storage (`NULL`) and syncing it onto a NOT NULL column would have failed on the first Enterprise upgrade. Application code already handled `NULL`.
* **ORM gotcha**: an explicit `None` on a column with a Python default is replaced by the default on INSERT. `plan.max_whatsapp_numbers` therefore has no default, and `new_tenant_capabilities` writes `sqlalchemy.null()` for unlimited values.

### Migration `ent10entitlements` (down_revision `act10onboarding`, single head)
* adds the columns (existence-checked), sets plan data, then backfills tenants. Backfill touches only the synced fields and only where the field is **not** in `override_flags`; `max_whatsapp_numbers` is raised from 0/NULL, never lowered; Lite tenants' `max_automation_actions` moves 5 -> 2, other tiers' action limits are untouched.
* The **Growth override tenant**: Growth plan has every gate on, so it ends up with all on (`test_growth_override_tenant_ends_up_with_everything_on`; also proved on the scratch DB below).
* Feature strings are replaced only when the stored list equals the previous seed list; a hand-edited list is left alone and the migration prints `WARNING: plan '<tier>' features were edited by hand; left unchanged`. A fresh database built only from migrations (not from prod) shows that warning for `growth`; the seed then writes the right strings.
* Downgrade drops the columns, restores Lite price/actions, the screen-recording default and the old strings; tenants' recording is set back to on (the previous universal default), WhatsApp numbers without the grant go back to 0, and unlimited storage/numbers (`NULL`) become 1,000,000 / 0 because the old columns were NOT NULL.

## What's involved (paths)
* `app/services/plan_catalog.py` (new), `app/services/entitlements.py` (new), `app/schemas/entitlements.py` (new), `app/sales/config_view.py` (new)
* models: `app/db/models/subscription_plan.py`, `app/db/models/tenant.py`; schemas `app/schemas/pricing.py`, `app/schemas/tenant.py`
* services: `plan_service.py`, `subscription_service.py`, `platform_subscription_service.py`, `tenant_service.py`, `company_registration_service.py`, `notification_service.py`, `subscription_validation_service.py`, `automation_service.py`, `chat_automation_orchestrator.py`, `whatsapp_service.py`, `screen_recording/gating.py`
* endpoints: `tenant.py` (new route), `sales.py`, `meetings.py`, `chat.py`, `reviews.py` (+ export), `analytics.py`, `widget.py`; worker `workers/sales_notifications.py`; `app/main.py` (handler)
* `alembic/versions/ent10_plan_entitlements.py`; `scripts/seed_data.py` (now reads the catalog), `scripts/seed_staging_e2e.py` (gates from the catalog), `scripts/capture_entitlements.py`
* docs updated: `docs/SUBSCRIPTION_PLANS.md`, `docs/FRONTEND_API_DOCUMENTATION.md` (price table), `docs/PAYSTACK_SETUP_GUIDE.md` (amount note), `luran-knowledge-docs/*` (Lite price, limits, gates), historical banner on `docs/SUBSCRIPTION_PRICING_MODEL.md` and `docs/DATABASE_PRICING_IMPLEMENTATION.md`. `PRICING_IMPLEMENTATION_SUMMARY.md`, `TOKEN_PRICING_STRATEGY.md` and `docs/SUBSCRIPTION_TESTING_GUIDE.md` carry 2026-01/02 numbers that were already stale before this change and were left as historical.

## Paystack: how amounts are charged, and the owner-action list
**Finding (read from `app/services/paystack_service.py`).** A checkout calls Paystack `transaction/initialize` with BOTH `amount` (taken from the database via `PlanService.get_pricing`) AND `plan` (a Paystack plan code). The plan code comes from the env vars `PAYSTACK_PLAN_{LITE,PROFESSIONAL,GROWTH}_{MONTHLY,ANNUAL}` (`app/core/config.py`), falling back to the hard-coded `PaystackService.DEFAULT_PLAN_CODES`. `resolve_plan_code` lists the Paystack plans and returns the configured code whenever that code exists, **without comparing its amount to the database**; it only searches by amount + interval + tier name when the configured code is missing.

By Paystack's documented behaviour (from memory of the API reference; I could not fetch the page from here, so **confirm in test mode**), when a `plan` is supplied the plan's own amount and interval apply and the `amount` field is ignored. So the charge and every renewal follow the **Paystack plan**, not the database row. Changing `monthly_price_kobo` alone would show 15,000 naira in Luran while Paystack keeps charging whatever the old Lite plan says (9,000).

I changed no payment configuration or code. Before the 15,000 price can ship to prod:
1. In Paystack (test mode first, then live) create two NEW plans (do not edit the old ones: editing can reprice existing subscribers): **Luran Lite Monthly**, amount **1500000** kobo (15,000 naira), interval monthly; **Luran Lite Annual**, amount **15000000** kobo (150,000 naira), interval annually. Currency NGN.
2. Set `PAYSTACK_PLAN_LITE_MONTHLY` and `PAYSTACK_PLAN_LITE_ANNUAL` to the new plan codes in the env file / secrets of every environment (staging first, then prod; keys are listed in `.env.example`). Without them the code uses the old hard-coded Lite codes. Restart the API.
3. Only then run the migration and the seed in that environment. Ship order: Paystack plans -> env -> deploy (migration) -> seed review. Never the other way round.
4. On staging, in Paystack test mode, start a Lite checkout and confirm the page shows 15,000 (and 150,000 annual) and the resulting transaction/subscription amount; this is the one check that proves the "plan overrides amount" behaviour.
5. If any Lite subscriber exists before the cut-over (none in prod on 2026-10-27), they stay on the old Paystack plan until moved by hand.
6. Optional hardening (not done; payments are out of scope): make `resolve_plan_code` compare the plan's amount with the database amount and refuse to start a checkout on a mismatch, so this class of drift cannot happen silently.
7. `docs/PAYSTACK_SETUP_GUIDE.md` lists the original plan amounts; its Lite entries now carry a note pointing here.

## Verification
| Check | Result |
|---|---|
| `ruff check app/` (0.8.6), `black --check app/` (24.10.0) | clean |
| Full suite `pytest tests/` against Postgres 15 + Redis 7 containers, CI env loaded programmatically | 2245 passed, 33 skipped (baseline on the untouched tree: 2134 passed, 33 skipped) |
| SQLite regression job (`tests/api/v1/test_chat_flow_router.py`, `test_flow_action_call_regressions.py`) | 14 passed (`DATABASE_URL=sqlite:///:memory:`) |
| New tests: `tests/entitlements/` (resolver, parity, every gate, endpoint, tier sync, WhatsApp limit) and `tests/migrations/test_ent10_entitlements.py` | pass; the same files error on the pre-change tree (nothing to import) |
| Automated real `upgrade()` / `downgrade()` / `upgrade` round trip on a scratch Postgres database (`test_real_upgrade_downgrade_upgrade_round_trip`: nullability, defaults, dropped columns, tenant values) | pass |
| Alembic on a scratch Postgres by hand: single head `ent10entitlements`; seeded the OLD plans and five tenants (trial, lite, lite+override, growth+override, professional with a hand-granted WhatsApp number), `upgrade head`, `downgrade -1`, `upgrade head` again | upgraded values correct; downgrade restored the old price/actions/strings; second backfill changed 0 rows; also `upgrade head` from an empty database |
| Live API run (`scripts/capture_entitlements.py`, 28 captures) | gate errors, entitlements for 6 tenant situations, public meeting endpoints on a Lite tenant, downgrade conflicts, public plans |
| NOT proven with a running app | chat sales branch with a real LLM; Slack/CRM delivery; Cloudinary recording paths; WhatsApp limit through the real sidecar; Paystack charge amount; promotional-grant flow through HTTP |

## Review (Opus, security-critical parts) and what was done
* Gate bypass: no bypass found on any HTTP/WebSocket/voice/WhatsApp/worker path (all chat entry points reuse the one chat send path; `QuoteService`, `MeetingService`, `NotificationService.dispatch` have no other callers). One should-fix: lapsed accounts kept the trial's gates on routes with no 402 check -> **fixed** (resolver, tests, capture 28, FE guide). One nit (grant end window) -> **fixed**.
* Tenant isolation: no findings.
* Migration: tests only exercised the data steps -> **real round-trip test added**. Not changed: downgrade is lossy where documented.
* Open: staff session detail (`chat.py` `session_rating_briefs`) still shows each conversation's own ratings to a Lite admin; treated as outside "breakdown" (list + export + analytics columns). Lead to confirm.
* Before deploying: read-only prod query of the Growth override tenant's `subscription_tier` and `override_flags`. The migration and resolver key off `subscription_tier`; if that tenant's tier is not `growth` (Growth-level capabilities set by hand on a lower tier) it would resolve to the lower tier's gates.

## Operate / roll back
* Deploy: `alembic upgrade head` (needs `ENCRYPTION_KEY` in the environment, as for earlier migrations), then review plan `features` (the migration warns if hand-edited), then the Paystack owner steps above. Staging reseed (`scripts/seed_data.py`) now writes the catalog values, including the new Lite price: do the Paystack steps first on that environment.
* Change a plan value later: edit `plan_catalog.py`, add a new migration for deployed databases, and repoint `test_latest_plan_migration_froze_the_same_numbers_as_the_catalog`. A platform-admin plan edit through the API changes the database row only (the next seed will revert it to the catalog).
* Roll back: `alembic downgrade -1` (data caveats above); the code is backwards-compatible with the old schema only after the downgrade, so roll code and migration back together.
* One tenant needs a gate outside its plan: a platform-admin capability edit (records an override).

## Follow-ups
* Paystack owner actions above (blocking for the price in prod).
* Dashboard: lock badges, upgrade prompts, `feature_not_in_plan` handling (separate track); the docs site and pricing page copy after prod promotion.
* Validate the chat sales branch and Slack/CRM with a real LLM / Slack on staging.
* Confirm the CSV export and the `whatsapp.enabled`-means-grant decision with the lead.
* Optional `resolve_plan_code` amount check (above); consider a platform-admin "re-sync tenants from plan" action after a plan edit (today a plan edit reaches tenants through the resolver, but the stored copy only syncs on a tier change).
* `flow_service` (webhook nodes) and `knowledge_service` (URL sources) still read plan + capabilities their own way; they were not part of the new gates and behave as before.
