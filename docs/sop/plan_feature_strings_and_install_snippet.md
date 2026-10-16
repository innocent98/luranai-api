# SOP — Plan feature strings, seed drift, and the widget install snippet

**Date:** 2026-10-16 · **Branch:** `fix/plan-feature-strings` → `develop`

## What shipped
1. Migration `pf10planfeatures` rewrites the stale marketing strings in `subscription_plans.features`: Lite "15 → 20 knowledge sources", Growth "50 → 100 knowledge sources".
2. `scripts/seed_data.py` now matches migration `b7d21c9f3a04` (Trial 20 audit pages; Lite 20 sources, 100 audit pages; Growth 100 sources, plus the two feature strings).
3. `GET /widget/install-code` returns a snippet that works: loader tag first, then `Luran.init({ tenantId, apiKey })`, no `async`/`defer`.

## Why
- `b7d21c9f3a04` changed the capability columns but not `features`, so `GET /subscription/plans` advertised 15 and 50 sources while enforcing 20 and 100.
- **Staging was reverting to the old limits on every deploy.** CD runs `seed_data.py` on staging only, and it *updates* existing plan rows, so staging served Lite 15 sources / 200 audit pages, Growth 50, Trial 50 pages (verified on `staging-api` on 2026-10-16) while prod had the migrated values.
- The install snippet set `window.luranSettings` and loaded the script `async defer`. The production loader never read `luranSettings` and only starts on `Luran.init()`, so every tenant who copied the dashboard snippet got no chat button. (luran-widget now also auto-starts from `luranSettings`, which repairs those existing installs; see that repo's SOP.)

## How
- Migration replaces only an exact old string, so admin-edited features are left alone; idempotent; downgrade reverses.
- Consistency test `test_seeded_feature_strings_match_capability_columns` fails whenever a "<n> knowledge sources / team members / flows / automation actions / executions / conversations / MB storage" string disagrees with its column. Mutation-checked: putting "15 knowledge sources" back fails it with a clear message.
- No SRI hash on the snippet's script tag: `widget/v1/widget.js` is a moving URL updated on every widget release, so a pinned hash would break installs.

## Files
`alembic/versions/pf10_plan_feature_strings.py` · `scripts/seed_data.py` · `app/api/v1/endpoints/widget.py` (`get_install_code`) · tests `tests/migrations/test_pf10_plan_feature_strings.py`, `tests/api/v1/test_widget_install_code.py`, `tests/services/test_subscription_tier_sync_respects_manual_override.py` (Lite default 15 → 20).

## Verification
- Full suite against throwaway Postgres 15 + Redis 7 with the CI test env: **1931 passed, 33 skipped**. SQLite automation/flow job: 14 passed. `ruff check app/` + `black --check app/` clean (pinned ruff 0.8.6 / black 24.10.0).
- `alembic upgrade head` → single head `pf10planfeatures`; `downgrade -1` and re-upgrade clean.

## Operate / roll back
- Deploys with normal CD. On staging, the next deploy's seed run restores the correct limits. On prod, `alembic upgrade` rewrites the two strings.
- Roll back: `alembic downgrade n2replyto` (restores old strings) and revert the commit.
- After deploy: `curl https://api.luranai.com/api/v1/subscription/plans` should show "20 knowledge sources" (Lite) and "100 knowledge sources" (Growth); `staging-api` should show Lite 20 / Growth 100 capabilities.

## Follow-ups
- Consider making prod CD also run the plan upsert, or removing the seed's plan update, so there is one writer for plan rows.
- Tenants who pasted the old snippet keep it; they are repaired by the luran-widget auto-init release (needs the prod CDN go).
