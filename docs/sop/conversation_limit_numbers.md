# SOP: New monthly conversation limits (Trial 100 / Lite 1,000 / Professional 5,000 / Growth 10,000), 2026-10-29

## What shipped
Branch `feature/conversation-limit-numbers` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges). Production untouched.

| Plan | Was | Now |
|---|---|---|
| Trial | 200 | **100** |
| Lite | 2,000 | **1,000** |
| Professional | 10,000 | **5,000** |
| Growth | 25,000 | **10,000** |
| Enterprise | unlimited | unlimited (unchanged) |

1. **One source of truth**: `app/services/plan_catalog.py` (`_CAPABILITIES`). The "N conversations/month" feature strings are generated from it by `build_feature_strings`, so the seed, the public plans endpoint and new-tenant capabilities all follow.
2. **Alembic `cl10conversationlimits`** (down_revision `wa30whatsappdefaulton`, single head): moves the four plan rows, their one feature string each, and the tenants that still sit on the old plan number.
3. **Warning, not a cut**: the migration prints one `WARNING` per tenant whose usage this month is already above its new limit; the existing downgrade validation (`SubscriptionValidationService._check_conversations`) already warns against the plan's capabilities, so it now warns at the new numbers with no code change.
4. **Enterprise pricing helper** (`app/utils/enterprise_pricing.py`): Growth is now 10,000, so the Growth-vs-Enterprise comparison uses 10,000 and has a new `"custom"` answer for the 10,001-24,999 gap (see decisions).
5. **Read-only prod usage query**: `docs/handoff/prod-conversation-usage-readonly.sql`.

FE contract: section "Monthly conversation limits (2026-10-29)" of `docs/fe-integration-guide-entitlements.md`; captures `docs/fe-integration-captures/conversation-limits/` (script `scripts/capture_conversation_limits.py`).

## Why
Owner decision 2026-10-29. Cost is not the reason: a conversation is about 7,050 tokens on gpt-6-luna ($0.10/M in, $0.50/M out), under 6 naira even if all of it were output. Peak real usage was 17 conversations in a month (2026-10-27 aggregates), so the limits are packaging: a price per conversation that rises with the tier's price (Lite 15,000 naira / 1,000 = 15; Professional 40,000 / 5,000 = 8; Growth 80,000 / 10,000 = 8). Step 2 of the counting work: the meter was fixed first (`docs/sop/conversation_counting.md`).

## How (decisions, and what each costs if wrong)
| Decision | Why / rejected alternative | Cost if wrong |
|---|---|---|
| **(a) Tenants already over the new limit this month: apply the new limit and warn; never skip or delete.** The migration prints a `WARNING` naming each such tenant (id, tier, count, limit). Existing conversations stay and keep working; only opening a NEW counted conversation is refused (`402 conversation_limit_reached`) until the 1st of next month (UTC). The existing `_check_conversations` warning covers a later downgrade. | Silently cutting is what the owner ruled out; deleting is irreversible. Rejected: skipping over-limit tenants in the migration (they would keep the old number forever, drift from the plan, and a later plan sync would cut them with no warning at all). Rejected: a new in-app notice (scope; the prod set is tiny). | A tenant over the new limit is blocked from NEW conversations for the rest of the month with no in-app notice. **Mitigation: run `docs/handoff/prod-conversation-usage-readonly.sql` on prod BEFORE deploying and set an override or tell the tenant for anyone with `over_new_limit_by > 0`.** |
| **(b) The one active Growth tenant on prod has no conversation override**, so it moves 25,000 -> 10,000 with the plan. **Decided 2026-10-29 (Adebayo): do NOT keep it at 25,000; no override is set.** If he ever reverses: to keep it at 25,000, set an override before or after deploy: `PUT /platform/tenants/{id}/capabilities {"max_conversations_per_month": 25000}` (live capture `04-...`). The migration leaves an overridden tenant alone. | The migration only moves tenants that are on the old plan number and not overridden, which is exactly the "plan-synced" population. | If the tenant really uses more than 10,000 a month it is blocked mid-month. If you want it grandfathered and forget the override, it drops silently. **An override is not permanent**: it survives an upgrade/activation sync but a downgrade, a promotional grant starting or ending, or a platform tier change that applies the tier capabilities resets it to the plan number and clears the flag (live DB captures `05-07`). |
| **(c) The 2 trialing tenants drop 200 -> 100** through the same path as every other tenant (same warning if they are already above 100 this month). | A trial is 30 days; the new number is the approved one. Rejected: grandfathering the two trials (no mechanism that is not an override, and the tenants are expiring anyway). | A trial that has used more than 100 conversations is blocked from new ones for the rest of the month; it can upgrade to raise the limit. |
| Backfill only rows whose stored value is **exactly the old plan number** and not in `override_flags` (narrower than ent10, which wrote the plan value wherever there was no override) | A value that is neither the old nor the new number is something somebody set by hand without the override flag; overwriting it would be a silent change. | A stale odd value stays until its next tier sync. The read-only SQL shows `limit_after_migration` for every tenant so none is a surprise. |
| Plan rows: change the limit only if it equals the old number; swap the feature string only if the list holds the exact old string (the same element-level rule as wa30) | Keeps the WhatsApp line wa30 rewrote safe, and leaves hand-edited plans alone with a `WARNING`. | A hand-edited plan keeps its number and string; fix it in the platform pricing editor. |
| Enterprise stays `NULL` (unlimited); automation-executions limits (25,000 on Growth) and every other limit are **not** touched | Out of the decision's scope; "25,000" on Growth executions is a different meter. | n/a |
| **Enterprise / custom quote starts at 10,001 (Adebayo, 2026-10-29).** The helper keeps the published per-conversation schedule at 25,000+ and answers `"custom"` for 10,001-24,999 (Enterprise, quoted by hand). Catalog copy moved to `"Custom pricing for 10,001+ conversations/month"` and `"Custom pricing for high-scale operations (10,001+ conversations a month)"`; `cl10` rewrites both on the enterprise row. | No price was invented for 10,001-24,999; he said where Enterprise starts, not what it costs there. | A customer needing 10,001-24,999 conversations has no standard plan; sales sees "custom". Growth now costs 8 naira a conversation at its limit while Enterprise at 25,000 costs 3, so Growth looks expensive next to Enterprise. |
| Fixed in passing: `compare_with_fixed_tiers` returned no `recommendation` for the Growth case, which the response model requires (the endpoint would have 500'd); it now returns one. | Same function, same lines. | n/a |

## Conversation definition (added 2026-10-29, owner wording)
Users must not read a "conversation" as a message or a chat bubble. One canonical text, identical everywhere:

> A conversation is one continuous exchange between a customer and your AI or team, from their first message until they have been quiet for 6 hours. It is not counted per message: one conversation can hold any number of messages, images and replies. If the same customer comes back within 6 hours it is still the same conversation; after 6 hours it is a new one. A conversation handed to a human agent stays one conversation until it is resolved. Conversations are counted per calendar month (UTC) and the count resets on the 1st. WhatsApp follows the same rule per chat.

| Where it lives | How it stays identical |
|---|---|
| `app/services/conversation_counting.py` `CONVERSATION_DEFINITION` (hours filled from the setting) | Source of the 402 `definition` field (additive; `message` is unchanged so clients that match it keep working). |
| Knowledge base: `luran-knowledge-docs/subscription_plans_and_pricing.txt`, `LURAN_FAQ.md` (FAQ 18), `luran_platform_knowledge.txt` | Verbatim, each under a "What counts as a conversation?" heading. |
| `app/templates/emails/trial_expiring.html`, `docs/SUBSCRIPTION_PLANS.md`, `docs/SUBSCRIPTION_PRICING_MODEL.md`, the 2026-10-27 spec addendum, `docs/fe-integration-guide-entitlements.md`, this SOP | Verbatim. |
| `tests/conversation_counting/test_definition_copy.py` | Fails if any of those files stops containing the text, or if `CONVERSATION_REUSE_WINDOW_HOURS` is no longer 6. |

* **The 6 hours is a setting** (`CONVERSATION_REUSE_WINDOW_HOURS`, default 6). If it changes, the copy must change in every place above, plus the frontends; the test fails until it does.
* **Feature strings and plan descriptions were not changed** ("1,000 conversations/month" stays). They are matched exactly by the cl10, wa30 and pf10 migrations (hand-edit guard) and by clients, and a parenthetical would wrap on the pricing cards. The definition sits next to the number instead (FE guide: show it beside every limit and usage count). Cost if wrong: a pricing card without the sentence still shows the bare number; the frontend PR can add it.
* The wording is a product statement, not a spec of the code: it matches the behaviour in `docs/sop/conversation_counting.md` (sliding window from the last message, escalated/active-with-agent sessions continued whatever their age, `created_at >= first of month UTC`).

## What's involved
| Area | Path |
|---|---|
| Plan values (source of truth) | `app/services/plan_catalog.py` (`_CAPABILITIES` trial/lite/professional/growth) |
| Migration | `alembic/versions/cl10_conversation_limit_numbers.py` (`cl10conversationlimits`, parent `wa30whatsappdefaulton`) |
| Pricing helper | `app/utils/enterprise_pricing.py` (`MIN_ENTERPRISE_CONVERSATIONS`, Growth cap 10,000, `custom` answer), `app/api/v1/endpoints/platform/pricing.py` (docstring) |
| Email | `app/templates/emails/trial_expiring.html` (Lite 1,000, Professional 5,000) |
| Seeds and dev scripts | `scripts/seed_staging_e2e.py` (pro, lite and quota tenants), `create_test_user.py` |
| Tests | `tests/migrations/test_cl10_conversation_limits.py` (43), `tests/utils/test_enterprise_pricing_compare.py` (9), `tests/entitlements/test_plan_parity.py` (repointed: ent10 + wa30 + cl10 swaps) |
| Evidence | `scripts/capture_conversation_limits.py` -> `docs/fe-integration-captures/conversation-limits/`; `docs/fe-integration-captures/entitlements/01-public-plans.json` refreshed from the live app |
| Docs | `docs/SUBSCRIPTION_PLANS.md`, `docs/fe-integration-guide-entitlements.md`, `docs/FRONTEND_API_DOCUMENTATION.md`, knowledge base `luran-knowledge-docs/subscription_plans_and_pricing.txt`, superseded banners on `PRICING_IMPLEMENTATION_SUMMARY.md`, `TOKEN_PRICING_STRATEGY.md`, `docs/DATABASE_PRICING_IMPLEMENTATION.md`, addendum on the 2026-10-27 spec |
| Owner tool | `docs/handoff/prod-conversation-usage-readonly.sql` |

Deliberately not changed: example values that are arbitrary admin-edit samples, not plan numbers (`app/schemas/pricing.py` example 1,500; `app/services/plan_service.py` docstring; `docs/SUBSCRIPTION_PRICING_MODEL.md` SQL samples; `docs/README.md`); historical migrations; the whatsapp/entitlements captures of earlier features.

## Verification
* `pytest tests/migrations/test_cl10_conversation_limits.py`: 43 passed, including the real `upgrade()` / `downgrade()` / `upgrade` on a scratch Postgres (seeded with old plan rows, hand-set, overridden, unlimited, unknown-tier and over-limit tenants: after up the numbers are new, overrides and odd values untouched, 101 sessions still present and the WARNING printed; after down the state equals the pre-state byte for byte; up again equals the first up).
* Affected suites (entitlements, subscription validation, conversation counting, wa30/ent10/pf10 migrations, whatsapp, plan/pricing users): 326 passed locally; CI runs the full suite.
* `alembic heads` shows one head, `cl10conversationlimits`; `alembic upgrade head` ran on a fresh scratch DB.
* Live: local app on the migrated scratch DB. Public plans show 100 / 1,000 / 5,000 / 10,000 and matching strings; a new Trial tenant holds 100 and a synced Growth tenant 10,000; the admin override returns 200 and is flagged; it survives an upgrade-style sync and is cleared by a downgrade-style sync; the pricing compare returns `growth` / `custom` / `enterprise` at 10,000 / 15,000 / 30,000. Captures are the evidence.
* **Not verified live:** the Paystack leg of a downgrade (so the downgrade `warnings` string is test-only); the `402` at the new numbers (shape unchanged, live in the conversation-counting captures); anything on staging or prod.

## Operate / roll back
Order for prod, after Adebayo's "merge #N" and the usual promotion checks:
1. Run `docs/handoff/prod-conversation-usage-readonly.sql` (read-only) and decide about every row with `over_new_limit_by > 0`, and about the Growth tenant (decision b).
2. If keeping a tenant at its old number, set the override first (platform capabilities editor), so the migration skips it.
3. Deploy; the migration runs with the API container. Read the migration output for `WARNING` lines (over-limit tenants; hand-edited plans).
4. `GET /api/v1/subscription/plans` should show the new numbers; the dashboard and pricing page must read them from there (FE guide).

Roll back: `alembic downgrade wa30whatsappdefaulton` restores the old numbers and strings under the mirror-image rules (only where the value is exactly the new number and not overridden). Tenants edited by hand in between are left alone. No data is deleted either way.

## Follow-ups
* ~~Decision: Growth tenant override~~ **Decided 2026-10-29 (Adebayo): no override.** The active prod Growth tenant moves 25,000 -> 10,000 with the plan.
* ~~Decision: who covers 10,001-24,999~~ **Decided 2026-10-29 (Adebayo): Enterprise / custom quote starts at 10,001.** The catalog description and the pricing feature line now say "10,001+" (`cl10` rewrites both on the enterprise row, only where the text is exactly the old string, reversibly); the pricing helper keeps `recommended_tier: "custom"` for 10,001-24,999 with the reason "Enterprise (custom quote)"; the published per-conversation schedule still starts at 25,000 (no price was invented for 10,001-24,999).
* No in-app notice when a tenant is over its limit or close to it (the widget gets the `402` only). Candidate: a dashboard banner at 80% and 100%.
* The dashboard / `luran-chat` repos were only grepped, not changed: see the PR body for the hardcoded-number list.
* Prod promotion rides the existing promotion PR; nothing here ships to prod by itself.
