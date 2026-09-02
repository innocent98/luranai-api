# SOP — Slim CI/CD topology + api worker right-sizing

**Status:** Shipped on branch `chore/slim-cicd-and-workers` (off `develop`, not yet merged) · 2026-09-02
**Area:** `.github/workflows/ci.yml`, `.github/workflows/docker.yml`, `.github/workflows/cd.yml`,
`Dockerfile`, `docker-compose.prod.yml`

> **Live-run caveat:** GitHub Actions is disabled account-wide (billing) at ship time, so none of
> the workflow changes have been executed on a real runner. They were validated with `actionlint`,
> a `yaml.safe_load` parse, and a manual trigger/`if`/`needs` graph read-through. **Watch the first
> real `main` auto-deploy to production closely** once billing is restored.

---

## What shipped

Three coordinated changes that cut redundant pipeline work and stop over-provisioning the api:

1. **api worker right-sizing + recycling.** Prod api gunicorn workers 4 → 2 (staging 1 via its own
   env), and workers now recycle every ~1000 requests to bound slow leaks.
2. **Playwright removed from the deploy path + heavy E2E made opt-in.** The `dashboard-staging-e2e`
   job (cross-repo Playwright dispatch) is deleted; `staging-api-e2e` no longer gates every deploy —
   it runs only on manual dispatch.
3. **No more develop→main double-run; `main` auto-promotes to production.** `develop` is now the
   single validation gate. Merging to `main` no longer re-runs CI or rebuilds the image — the
   production deploy **promotes the exact image already built for that commit on develop**, pulled
   from GHCR by its long SHA tag.

## Why

- **Worker sizing:** the api loads no ML models and is async/I/O-bound; all heavy work (audits,
  crawls, embeddings, doc processing) runs in the separate `worker` container (`python worker.py`,
  ARQ). 4 gunicorn workers × the api mem_limit was wasted RSS. 2 (prod) / 1 (staging) is plenty, and
  `--max-requests` recycling caps any slow memory growth without a full restart.
- **Playwright / E2E:** Playwright belongs to the FE repo and manual QA, not the backend deploy
  gate. Running it (plus a full live API E2E) on *every* develop push made staging deploys slow and
  flaky for no backend-correctness signal.
- **Double-run:** merging develop→main re-ran lint+test+automation+build+staging-deploy — all
  already green on develop — then rebuilt a byte-different image for prod. Wasteful and it meant prod
  ran an image that was never the one tested on staging. Promotion-by-SHA fixes both: prod runs the
  identical image bytes, with zero re-test latency.

## How — the new topology

| Branch / event | ci.yml | docker.yml | cd.yml |
|---|---|---|---|
| **push `develop`** | full CI (lint, automation, test, build, trivy) | build + push image (incl. `sha-<full>` tag) | `staging-deploy` on CI success (`workflow_run`) |
| **PR → main/develop** | full CI | build (no push) | — |
| **push `main`** (develop→main merge) | **nothing** | **nothing** | `production-deploy` — pull `sha-<full>` from GHCR, no rebuild, deploy prod |
| **manual dispatch** | — | — | `environment=staging` → staging-deploy (+ `staging-api-e2e`); `environment=production` → production-deploy |

### Before → after

**Before:** `develop` push → CI → staging-deploy → **staging-api-e2e** → **dashboard-staging-e2e
(Playwright)**. `main` push → CI (re-run) → **rebuild image** → staging-deploy → both E2E gates →
production-deploy. Two full pipelines per release; prod ran a freshly-rebuilt (untested) image.

**After:** `develop` push → CI → staging-deploy (E2E only on demand). `main` push → **promote the
develop image to prod, no CI, no rebuild**. One pipeline per release; prod runs the exact bytes
staging validated.

### Image promotion mechanism (the load-bearing detail)

- `docker.yml` now emits **two** SHA tags: the existing short `type=sha` (human-readable) **and a
  new `type=sha,format=long`** → `ghcr.io/innocent98/luranai-api:sha-<40-char-sha>`.
- `cd.yml` `production-deploy` runs on `push: main`, resolves `IMAGE_TAG=${{ github.sha }}`, logs
  into GHCR (`packages: read`), `docker pull`s `…:sha-<github.sha>`, retags it `luran-api:<sha>`,
  saves the tarball, and scp/loads it on the VPS exactly like before. **No `docker build`.**
- **This requires develop→main to be a fast-forward** so `main`'s head SHA equals the develop commit
  that `docker.yml` built. A merge/squash commit produces a new SHA with no prebuilt image; the pull
  fails loudly with remediation text (rebase main onto develop / re-run develop CI) — it never
  silently rebuilds.

### Worker changes

- `Dockerfile` CMD gains `--max-requests 1000 --max-requests-jitter 200` (all other gunicorn flags
  unchanged).
- `docker-compose.prod.yml` api `API_WORKERS` default `4 → 2`, with a comment noting staging sets
  `API_WORKERS=1` in its own owner-managed encrypted `.env` (encrypted envs untouched).

## What's involved

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | push trigger `[main, develop]` → `[develop]`; PRs unchanged |
| `.github/workflows/docker.yml` | push trigger `[main, develop]` → `[develop]`; added `type=sha,format=long` tag |
| `.github/workflows/cd.yml` | added `push: [main]` trigger; `workflow_run` branches `[main, develop]` → `[develop]`; staging-deploy `if` gated to develop / dispatch-staging; `staging-api-e2e` made dispatch-only; **deleted `dashboard-staging-e2e`**; rewrote `production-deploy` to pull+promote (no rebuild), trigger on push:main / dispatch-production, dropped staging-E2E `needs`, added `packages: read` |
| `Dockerfile` | gunicorn `--max-requests` / `--max-requests-jitter` |
| `docker-compose.prod.yml` | api `API_WORKERS` default 4 → 2 + comment |

## Verification

- `actionlint` on all three workflows: **no errors/warnings from these changes.** Only pre-existing
  info-level `SC2086` notes remain on the staging build step (count actually dropped 5 → 2 by
  deleting the prod rebuild step). Live workflow execution unverified (Actions billing).
- `yaml.safe_load` parses ci/docker/cd cleanly.
- `docker compose -f docker-compose.prod.yml --env-file <test> config`: no warnings / no unset-var
  messages; api `API_WORKERS` resolves to `"2"` by default.
- No Python touched → CI ruff/black/pytest unaffected (not re-run locally for that reason).

## Operate / roll back

- **Roll back the pipeline:** `git revert` this branch's merge, or restore the three workflow files
  from `develop`'s pre-merge commit. No infra state to unwind.
- **Roll back a bad prod promotion:** the previous image tarball is no longer kept on the runner, but
  every SHA image lives in GHCR — redeploy the prior good commit by dispatching `production` (or push
  a revert to main). The VPS also still holds the previously-loaded `luran-api:<prev-sha>` image
  until `docker image prune -f` runs.
- **Secrets/GitHub setup to confirm before first `main` deploy:** production Environment secrets
  (`VPS_*`, `DEPLOY_PATH`, `API_PORT`, `ENV_ENCRYPTION_KEY`, `APP_URL`) already exist for the old
  production-deploy job — unchanged. New requirement: the repo `GITHUB_TOKEN` must have GHCR
  `packages: read` on the image package (it does, since `docker.yml` pushes with `packages: write` to
  the same repo-linked package).

## Follow-ups / watch items

- **First real `main`→prod auto-deploy must be watched live** (Actions billing currently blocks any
  run). Confirm: the `sha-<full>` tag exists in GHCR for the merged commit, the pull succeeds, health
  check passes.
- **Enforce fast-forward-only merges into `main`** (branch setting or team convention) so the
  promotion SHA always resolves. Otherwise the deploy fails safe but does not ship.
- Consider keeping the previous image tarball on the VPS for an even faster local rollback (currently
  relies on GHCR / un-pruned local image).
- `staging-api-e2e` now redeploys staging when dispatched (it `needs: staging-deploy`). If QA wants
  to run E2E against already-live staging without a redeploy, that would need a decoupled job — not
  built here.
