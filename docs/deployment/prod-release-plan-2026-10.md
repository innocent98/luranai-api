# Production release plan: activation, plan entitlements, WhatsApp (code, dark)

> **2026-10-29 addendum:** `can_use_whatsapp` is no longer a per-tenant grant defaulting to false: migration `wa30whatsappdefaulton` (after `ref10referral`) makes it a plan capability for every tier. Prod stays dark only because `WHATSAPP_ENABLED` is false there. Add `wa30whatsappdefaulton` to the migration chain and run the pre-deploy check in `docs/sop/whatsapp_default_on.md` before it.

Status: **DRAFT for Adebayo's review, 2026-10-28. Nothing in this document has been run against production.**
Scope: promote everything on `develop` / dashboard `staging` / chat `stage` that production does not have yet. Source of truth for "what is different" is the git diff, checked 2026-10-28:

| Repo | Branch to promote | Ahead of `main` | Notes |
|---|---|---|---|
| `luran` (backend) | `develop` -> `main` | 62 commits, 342 files | 4 new migrations, 3 CI/CD files, `worker.py` |
| `luran-dashboard` | `staging` -> `main` | 15 commits | `main` is "behind" only by earlier merge commits of `staging` |
| `luran-chat` | `stage` -> `main` | 3 commits | sales metadata in the bootstrap transcript |
| `luran_agent` (Flutter) | none known | 0 | no agent-app release is required by this work; see Open questions |
| `luran-widget`, `luran-docs` | none | 0 | |

## 1. What production gets

| Area | What changes for a prod tenant |
|---|---|
| Activation / onboarding | New `GET /tenant/onboarding`, widget-seen tracking (hostname only), KB failure reasons, short-document handling. Dashboard checklist banner. |
| Plan entitlements | One resolver for plan gates. **Lite loses** sales booking/quotes, Slack/CRM notifications, screen recording, ratings export, lookup chains, webhooks; Lite AI actions 5 -> 2; Lite price 9,000 -> 15,000 (annual 150,000). Trial, Professional, Growth keep or gain as per the approved matrix (`docs/sop/plan_entitlements.md`). |
| WhatsApp channel | **Code ships dark.** `WHATSAPP_ENABLED` defaults to `false`, `can_use_whatsapp` defaults to `false` for every tenant, there is no sidecar in prod. The dashboard shows "not available" up front. No tenant can use it until section 6 is done. |
| Media + rating | Only reachable through WhatsApp, so also dark in prod. |
| Platform admin | WhatsApp grant + number limit in the capabilities editor; Runtime Logs tab and endpoint removed. |
| Leads / meeting card | Booked meeting card in lead details; leads detail fixes. |

## 2. How production deploys (read from `.github/workflows/cd.yml` on `develop`)

- **A push to `main` deploys production automatically.** No staging gate, no CI re-run, no image rebuild. The job pulls the moving `ghcr.io/<repo>:develop` image that `docker.yml` last built, so **whatever `develop` built last is what ships**, not the merge commit.
- The script runs, in order: `alembic upgrade 145bebe88ecb`, the four encryption backfills, `alembic upgrade 3d8e2fa91c07`, a backfill, then `alembic upgrade head`; then `compose up -d` and a health loop (12 tries, 10 s).
- Staging deploys on develop pushes only; the sidecar steps exist for **staging only**. Prod has no sidecar steps.
- Backups: nightly cron at 02:00 (confirmed running again 2026-10-16). Avoid promoting around 02:00.

## 3. Migrations production will run

Chain (all single-head, each sets `lock_timeout = 5s`): `pf10planfeatures` (expected current prod head, **verify**) -> `wa10whatsapp` -> `act10onboarding` -> `ent10entitlements` -> `wa20ratingreply`.

| Revision | Effect | Risk / reversibility |
|---|---|---|
| `wa10whatsapp` | new WhatsApp tables, capability columns | additive; downgrade drops the tables |
| `act10onboarding` | widget-seen columns on tenants | additive; downgrade drops columns |
| `ent10entitlements` | gate columns on plans and capabilities, **plan data (Lite price/limits), tenant backfill** | changes behaviour for existing tenants (section 4); downgrade restores Lite price/actions and the old strings; unlimited (`NULL`) storage/number values become 1,000,000 / 0 on the way back |
| `wa20ratingreply` | two nullable columns on `whatsapp_chats` | additive, trivially reversible |

## 4. The decision that matters: existing prod tenants and the new gates

The backfill moves each tenant to its plan's gates unless the field is in `override_flags`. A tenant that today uses a feature its tier now excludes **loses it on deploy**. Before promotion, run these **read-only** against prod (no writes) and bring the result to Adebayo:

1. Tenants per tier, and their `subscription_status`.
2. Any non-Professional/Growth/Enterprise tenant (Lite, and any Trial that will lapse) with: sales booking or quotes enabled, Slack or CRM URL set, screen recording enabled, a lookup chain or webhook action, or more than 2 AI actions.
3. The Growth override tenant's `override_flags` (agreed earlier: it keeps everything).
4. Active paying Lite subscribers on Paystack (the SOP recorded none on 2026-10-27; confirm).

Outcome options for any affected tenant: (a) accept the cut, (b) grandfather by writing the field into `override_flags` **before** the deploy, (c) hold the gate. This is the owner's call per tenant; do not default to (a) silently.

## 5. Owner actions (Adebayo) and pre-flight

| # | Item | Status |
|---|---|---|
| 1 | Paystack Lite plans (monthly 1,500,000 kobo, annual 15,000,000 kobo): edited in staging **and** prod, codes unchanged, no env change | done 2026-10-28 |
| 2 | Test-mode Lite checkout on staging shows 15,000 / 150,000 and the resulting subscription amount | **owed** |
| 3 | Section 4 read-only prod checks reviewed, per-tenant decisions taken | **owed** |
| 4 | GitHub Actions spending limit fixed (one CI job was billing-blocked); CI green on `develop` at the promoted head | **owed** |
| 5 | Production `alembic current` read-only confirms `pf10planfeatures` is the head | **owed** |
| 6 | Manual DB dump taken immediately before promotion (do not rely on the 02:00 cron) | at promotion |
| 7 | `docker stats` snapshot of prod API and worker before promotion (baseline for section 7) | at promotion |
| 8 | Most recent `Docker Build and Push` run on `develop` succeeded and no other PR merges to `develop` until prod is done | at promotion |
| 9 | Consent wording `2026-10-28-draft2` approved; liability wording added to the Terms of Service | needed before section 6 only |
| 10 | Re-private `luranai-api` | **owed** (independent of the release) |

No new required environment variables: every added setting has a default (`app/core/config.py`), so `.env.production.enc` does not need changes for the dark ship.

## 6. Order of operations

1. Pre-flight (section 5) complete.
2. **Backend:** open a PR `develop` -> `main` (previous promotions used a merge-commit PR, e.g. #150). Merging triggers the prod deploy. Watch the run; do not start the dashboard until `/api/v1/health` is 200 and the section 7 checks pass.
3. **Dashboard:** merge `staging` -> `main` right after the backend is verified (the new UI calls `/tenant/entitlements` and `/tenant/onboarding`; against the old backend those would 404). Confirm the dashboard host's deploy trigger first; it is not defined in these repos.
4. **Chat:** merge `stage` -> `main` (independent, can be any time after step 2).
5. Update memory, the checklists and the SOP index in the same pass.

Enabling WhatsApp for real tenants is **not** part of this release. It needs its own PR and its own go: a production sidecar job mirroring the staging one (`docker-compose.whatsapp.yml`, `scripts/prepare_staging_whatsapp_env.sh` pattern with a prod guard instead of the `luran_staging` project check, sidecar DB role init, `scripts/smoke_whatsapp_sidecar.sh`), generated and backed-up secrets in `.env.production.enc` (losing `WHATSAPP_AUTH_MASTER_KEY` forces every number to re-pair), `WHATSAPP_ENABLED=true`, a memory budget on the shared 7.8 GiB host (staging: sidecar limit 384 MiB, about 111 MB RSS with one session, refuse limit 300 MB), the consent/ToS items, then a per-tenant grant by hand.

## 7. Post-deploy verification (production, read-only or already-safe actions)

| Check | Expected |
|---|---|
| `GET /api/v1/health` | 200 |
| `alembic current` in the api container | `wa20ratingreply` (head) |
| `GET /subscription/plans` (public pricing) | Lite 15,000 monthly, 150,000 annual |
| `GET /tenant/entitlements` as a Professional tenant and as a Lite tenant | Pro: gates enabled; Lite: locked with `min_tier: professional` |
| Growth override tenant | everything still on |
| Dashboard WhatsApp page | "not available" up front, no connect form |
| Worker log | arq functions include `send_whatsapp_message` and `send_whatsapp_image`; no crash loop |
| Web chat on a real tenant (text, image upload) | works as before |
| `docker stats` | API and worker within the baseline taken in section 5 item 7 (prod API real app memory was about 367 MB; the high reading is mostly reclaimable kernel cache) |
| Next 02:00 backup | runs and lands (also in Drive) |

## 8. Roll back

| Layer | Action |
|---|---|
| API / worker | the previous `luran-api:<sha>` image is kept (the deploy keeps the 4 newest): set `API_IMAGE` to it and `docker compose -f docker-compose.prod.yml up -d` |
| Schema | only if the old image cannot run on the new schema: `docker compose -f docker-compose.prod.yml run --rm api alembic downgrade <rev>`, newest first (`wa20ratingreply`, `ent10entitlements`, `act10onboarding`, `wa10whatsapp`). `ent10` downgrade restores Lite 9,000 and 5 actions but does not undo tenant overrides you added in section 4. Restore the pre-promotion dump only as a last resort. |
| Dashboard / chat | redeploy the previous `main` commit on the host |
| Paystack | no change needed (the Lite amounts were edited in place; reverting means editing them back) |

## 9. Open questions

- Is the dashboard hosted so that a push to `main` deploys it automatically? (Not visible from the repos.)
- Agent app: no build is needed for the join line or images (server-side), but the app's own work on the `main` branch is separate (`luran_agent` has local unpushed commits, tracked elsewhere).
- Whether to promote now with WhatsApp dark, or hold the whole release until the production sidecar job is ready. Recommendation: promote now; the entitlement and activation work stands on its own and WhatsApp stays off by default.
