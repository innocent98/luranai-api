# SOP: WhatsApp sidecar on staging (pipeline wiring), 2026-10-28

## What shipped
The pipeline and compose wiring that deploys the WhatsApp sidecar (`whatsapp-sidecar/`) to the **staging** stack through the existing CD, **only when its secrets exist**; otherwise it is skipped with a log line and staging deploys exactly as before. Also raises the staging api memory limit to 768 MiB. Production is untouched. PR: `feature/sidecar-staging-deploy` -> `develop` (link in the hand-back). Plan, evidence and owner runbook: `docs/superpowers/plans/2026-10-28-sidecar-staging-deploy.md`. Sidecar itself: `docs/sop/whatsapp_sidecar.md`.

## Why
The sidecar had only run on one Mac. The live test (pair a real number on staging, escalate so the Flutter agent app rings, reply from the app to WhatsApp) needs it on staging, on the shared 7.8 GiB VPS, without any chance of affecting production (separate compose project/network) and without a red staging deploy when the secrets are not there yet.

## How
| Area | Decision |
|---|---|
| Gate | `scripts/prepare_staging_whatsapp_env.sh` runs on the CD runner after the env is decrypted. Required names present and well-formed (token/secret >= 32 chars, master key hex >= 64, DB password `[A-Za-z0-9]{24,}`, `COMPOSE_PROJECT_NAME=luran_staging`) -> `sidecar_enabled=true`; else `false` plus `WhatsApp sidecar: SKIPPED - <reason with NAMES only>` |
| Secrets | `WHATSAPP_AUTH_MASTER_KEY` and `WHATSAPP_SIDECAR_DB_PASSWORD` are moved to `.env.sidecar`; `.env` (the api/worker `env_file`) never carries them. Both files ship `600`. Token and webhook secret are shared by backend and sidecar, so they stay in `.env` |
| Image | Staging builds on the runner and ships via `docker save`/`scp` (like the API). `docker.yml` also publishes `ghcr.io/<repo>-whatsapp-sidecar` with the same tags (develop/tags; PRs to main build only). `ci.yml` builds it on every PR/push |
| Deploy | scp image + override + SQL + smoke script + `.env.sidecar`; ssh: guard project name, `docker load`, `run --rm --no-deps whatsapp-db-init`, `up -d --no-deps whatsapp-sidecar`, health loop on the image HEALTHCHECK, smoke, prune to the 3 newest images. Steps are `continue-on-error`; a status step warns |
| DB | Dedicated role `whatsapp_sidecar` (no grants on application tables, **no database-level CREATE**: the deploy pre-creates the schema and `db.mjs` skips `CREATE SCHEMA` when it exists; `CONNECTION LIMIT 8`), connection **direct to `db:5432`** (not pgbouncer: advisory lock + unknown role), created idempotently by the one-shot `whatsapp-db-init` |
| Redis | Logical DB 1 (`redis://redis:6379/1`), keys `wa:*`; fails closed (see plan 1.4) |
| Limits | sidecar 384 MiB, no swap, 0.5 CPU, 256 pids, `oom_score_adj 500`, read-only rootfs, caps dropped, `MAX_SESSIONS=2`, `RSS_REFUSE_MB=300`; api floor 768 MiB (raise-only) |
| Network | no published ports; `http://whatsapp-sidecar:8787` on `luran_staging_default`; webhook to `http://api:8000` |

## What's involved
`docker-compose.whatsapp.yml` (rewritten), `whatsapp-sidecar/src/db.mjs` (schema-exists check at bootstrap), `scripts/prepare_staging_whatsapp_env.sh`, `scripts/whatsapp_sidecar_db_init.sql`, `scripts/smoke_whatsapp_sidecar.sh`, `scripts/generate_whatsapp_staging_secrets.sh`, `.github/workflows/cd.yml` (changes inside `staging-deploy` only, +107/-1), `.github/workflows/docker.yml` (new job), `.github/workflows/ci.yml` (one build step), `.gitignore`. **Only edit to an existing condition:** `staging-deploy`'s `if` now also requires `workflow_run.event == 'push'` and the same repository (a fork PR from a branch named `develop` could otherwise trigger a staging deploy). **Not touched:** `docker-compose.prod.yml`, `Dockerfile`, any Python, `production-deploy`, `staging-smoke`, `staging-api-e2e`.

Env NAMES: backend+sidecar `WHATSAPP_SIDECAR_TOKEN`, `WHATSAPP_WEBHOOK_SECRET`; backend `WHATSAPP_ENABLED`, `WHATSAPP_SIDECAR_URL`, `WHATSAPP_MAX_CONNECTIONS`, `WHATSAPP_CONTACT_PEPPER`; sidecar-only `WHATSAPP_AUTH_MASTER_KEY`, `WHATSAPP_SIDECAR_DB_PASSWORD`. Optional overrides: `WHATSAPP_SIDECAR_REDIS_URL`, `WHATSAPP_BACKEND_BASE`, `WHATSAPP_MAX_SESSIONS`, `WHATSAPP_RSS_REFUSE_MB`, `WHATSAPP_SIDECAR_MEM_LIMIT`, `WHATSAPP_SIDECAR_CPUS`, `WHATSAPP_WEBHOOK_SECRET_PREVIOUS`. (`WHATSAPP_SIDECAR_DATABASE_URL` is built by the compose file; the owner does not set it.)

## Verification
| Check | Result |
|---|---|
| Host facts (limits, usage, networks, redis/db flags, project labels, env names) | Read-only `ssh luran`, 2026-10-28: see plan section 1 |
| `actionlint .github/workflows/*.yml` | Same 2 pre-existing `SC2086` infos (API build step) as `origin/develop`; none from this change |
| `yaml.safe_load` on 3 workflows + 2 compose files | OK |
| `shellcheck` on 3 scripts and on the extracted ssh-action script | OK |
| `docker compose config` with dummy env, with and without `--profile init` | OK (`whatsapp-db-init` only appears with the profile) |
| Production job byte identity vs `origin/develop` | `production-deploy` 8216 bytes, `staging-smoke` 2854, `staging-api-e2e` 3148: **identical**; header (`name/on/permissions`) identical; `cd.yml` diff = 3 hunks, +107/-1 (the one changed line is the staging-deploy `if`), all before line 432 where `production-deploy` starts; `docker-compose.prod.yml` unchanged |
| Prep script on 9 fixtures (nothing set / all set / prod project name + short token / partial / CRLF + bytes / indented, `export`, spaced `=`, `1.5GiB`, prefix-sharing key / `2048k` / `1gb` / unparsable) | skip with names only; enable; skip; skip; enable; api floor raised or kept; `.env` and `.env.sidecar` are `600`; sidecar-only names removed from `.env` in every case |
| Local rehearsal (throwaway `postgres:15-alpine` with the staging flags, `redis:7-alpine` with `--maxmemory 48mb allkeys-lru`, the real image, the real compose files, a stand-in endpoint running the repo's real `signing.verify` + `WebhookEvent`) | role init OK and idempotent; sidecar healthy under read-only/cap_drop/no-swap; `ports=map[]`; smoke 7/7 PASS; adding a `PUBLIC` grant on a probe table flips the least-privilege check to FAIL; direct `SELECT` and `CREATE TABLE public.x` as the role -> `permission denied`; a base-file-only `up -d` leaves the sidecar running (orphan warning only); generator prints 0 bytes, makes a 600 file, refuses to overwrite |
| Sidecar test suite after the `db.mjs` change, against real Postgres 15 + Redis 7 | 96/96 pass |
| Opus security review of the commit | 7 findings: 5 fixed (database CREATE schema-hijack, password on psql argv, whitespace-anchored key parsing, memory-size grammar, stop on unhealthy), the pre-existing fork-PR `workflow_run` trigger hardened, 1 accepted and documented (shared network with unauthenticated Redis/Qdrant, plan section 4). No finding on production isolation, secret printing, smoke or generator |
| **Not run** | The CD steps on a real runner/VPS (first proof = first deploy after secrets exist); a real signed webhook into the staging backend; FCM ring; agent reply reaching WhatsApp; multi-session RAM |

## Operate / roll back
Operate: smoke any time with `ssh luran 'cd /opt/luran-staging && bash scripts/smoke_whatsapp_sidecar.sh luran_staging_whatsapp_sidecar'` (prints pass/fail, `sessions=n/max`, `rss_mb`; writes one 30 s Redis probe key and sends one webhook for a non-existent session, which the backend ignores). Logs: `docker logs luran_staging_whatsapp_sidecar` (no message text, numbers or pairing codes).
Roll back (details in plan section 5): `WHATSAPP_ENABLED=false` (kill switch) -> `docker stop luran_staging_whatsapp_sidecar` -> revert the PR (does **not** stop a running sidecar) -> `DROP SCHEMA whatsapp_sidecar CASCADE; DROP ROLE whatsapp_sidecar;`. **Gotchas:** never add `--remove-orphans` to the existing staging steps; CD never stops the sidecar when names are later removed; rotating `WHATSAPP_SIDECAR_DB_PASSWORD` re-syncs the role on the next deploy (`ALTER ROLE`), rotating `WHATSAPP_AUTH_MASTER_KEY` makes every stored credential undecryptable (re-pair).

## Follow-ups
- Run the live test (owner steps F in the plan) and record the results here; then update `docs/sop/whatsapp_sidecar.md` RAM row with measured 0/1/2-session figures.
- Memory soak at 2+ sessions (`whatsapp-sidecar/scripts/soak.mjs` is a skeleton).
- Redis/Qdrant have no auth and the sidecar shares their network (plan section 4): dedicated Redis on a private network + ACLs before any production use.
- Check the `staging` GitHub environment protection rules (required reviewers / branch limits), the repo being public.
- Production: built 2026-10-29, see `docs/sop/whatsapp_sidecar_production_deploy.md` (GHCR image, prod DB role, limits). The dedicated-Redis / network-isolation item above is still open and is flagged there as a ruling.
- Investigate why the api containers sit at 83-90% of their limits (the 768 MiB floor is a stopgap).
- `pg_isready -U "${POSTGRES_USER:-postgres}"` in the existing CD ssh script uses an unset variable (works by luck).
- CD does not stop the sidecar when its secrets are removed; add an explicit disable flag if that becomes a real workflow.
- The world-readable `644` mode of the pre-existing server `.env` is fixed on the next deploy by the prep step; verify with `ls -l /opt/luran-staging/.env`.
