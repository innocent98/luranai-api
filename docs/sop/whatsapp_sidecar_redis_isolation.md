# SOP: WhatsApp sidecar gets its own Redis on a private network (production), 2026-10-30

## What shipped
The production WhatsApp sidecar no longer uses the shared production Redis or the shared Docker network. It has its **own authenticated Redis** (`luran_whatsapp_redis`) on an internal-only network, and it sits on a second network (`luran_wa_backend`) that holds only `luran_api`, `luran_worker` and `luran_db`. It can no longer resolve or reach the shared Redis, Qdrant or pgbouncer. **No production container restarts** (`docker-compose.prod.yml` is not touched). Branch `feature/sidecar-redis-isolation` -> `develop` (PR link in the hand-back). Plan: `docs/superpowers/plans/2026-10-30-sidecar-redis-isolation.md`. Builds on `docs/sop/whatsapp_sidecar_production_deploy.md`. Nothing here has run on the VPS.

## Why
The production sidecar deploy shipped with a ruling recorded in `whatsapp_sidecar_production_deploy.md`: the sidecar shared Redis (logical DB 1) and the default network, where Redis and Qdrant have no password. The sidecar parses untrusted WhatsApp traffic. A compromised sidecar could write the arq job queue in Redis DB 0 (arq's default serializer is pickle, so a forged job runs code in the worker, which holds the whole env) or read and overwrite Qdrant. The ruling said to decide this before `WHATSAPP_ENABLED` is flipped for real tenants. This is that decision.

## How
| Area | Decision |
|---|---|
| What the sidecar keeps in Redis | Only `wa:lease:{session_key}` (owner token, TTL 30 s, heartbeat every 10 s) and `wa:daily:{session_key}:{day}` (send counter, TTL 26 h), about 100 B each. The backend never reads or writes them (`grep` for `wa:` in `app/` finds nothing; the backend talks to the sidecar over HTTP only, from api and worker). So **no backend component needs this Redis** |
| New Redis | Service `whatsapp-redis` in `docker-compose.whatsapp.production.yml` (`redis:7-alpine`, the image prod already runs). Read-only rootfs, caps dropped, no-new-privileges, `user: redis`, no published port, `mem_limit` = `memswap_limit` = **32m**, 0.25 CPU, 32 pids, `oom_score_adj 500`, `maxmemory 8mb`, `maxclients 32`, log cap 10 MiB |
| Authentication | One ACL user (`default`): `on`, password from `WHATSAPP_SIDECAR_REDIS_PASSWORD`, keys `~wa:*` only, `+@all -@admin -@dangerous +info`. The password goes from the container env into a tmpfs ACL file (`--aclfile /tmp/users.acl`), so it is never in a command line, the container `Cmd`, or `ps` (checked in the rehearsal). It **is** in `docker inspect` `Config.Env` (as are the DB password and master key), readable by anyone in the docker group. The credential cannot read or write any non-`wa:` key, `CONFIG`, `FLUSHALL`, `KEYS`, `REPLICAOF`, `DEBUG`. `INFO` is allowed because ioredis uses it as its ready check |
| Persistence | **Yes, small:** `save 60 1` (snapshot after any change, at most once a minute) on the named volume `luran_wa_redis_data`, no AOF. Leases do not need it (30 s keys). The **daily send counter** does: it is the anti-ban cap (`DAILY_SEND_CAP`, 200 per session per day), and without persistence a Redis restart would let a number send up to twice its cap in one day. The shared Redis's `--save 60 1000` almost never fires at this write rate, so it was not copied. Proven: counter survives `docker restart` and remove + recreate |
| Eviction | **`noeviction`**, not `allkeys-lru`. On a dedicated instance nothing should ever be evicted: an evicted lease can cause two writers on one WhatsApp credential (which WhatsApp reads as an unlink), an evicted counter silently lifts the cap. At the cap writes fail and the sidecar already fails closed. `maxmemory 8mb` is about 1000x the real dataset and leaves room under the 32m limit (measured idle RSS 13.4 MiB; docker stats 9.1 MiB) |
| Networks | `luran_wa_redis`: `internal: true` (no gateway, so the Redis has no route out; members: the Redis and the sidecar only). `luran_wa_backend`: ordinary bridge with egress (the sidecar needs WhatsApp and Cloudinary); members: sidecar, `luran_api` (alias `api`), `luran_worker`, `luran_db` (alias `db`). The sidecar's `networks:` list drops the implicit `luran_default`, which is what removes redis/qdrant/pgbouncer from its reach |
| Why a runtime network, not the prod compose | Declaring `luran_wa_backend` on api/worker/db in `docker-compose.prod.yml` would recreate Postgres (and change api/worker config) on the next deploy. `scripts/whatsapp_sidecar_network.sh` creates the network and attaches the three containers with `docker network connect`, which restarts nothing. Compose treats the network as `external`. Cost: see "Operate" (api and worker lose the attachment each time they are recreated) |
| Password handling | New name `WHATSAPP_SIDECAR_REDIS_PASSWORD` (alphanumeric, >= 32 chars). Sidecar-only: `prepare_production_whatsapp_env.sh` strips it from `.env` into `.env.sidecar` exactly like the master key and DB password, so api and worker never hold it. The sidecar's `WHATSAPP_SIDECAR_REDIS_URL` is **built in the override from that password**; an inherited `WHATSAPP_SIDECAR_REDIS_URL` is ignored, so nobody can point the sidecar back at the shared Redis by accident |
| Gate (fail closed) | The password is a required name. Any sidecar name present but the password missing or malformed: the sidecar is **not deployed**, output names only (`missing or empty: WHATSAPP_SIDECAR_REDIS_PASSWORD`), the secrets are still stripped from `.env`. No sidecar names at all: byte- and mode-identical no-op, unchanged. `WHATSAPP_ENABLED` handling untouched |
| Deploy order | `cd.yml` `production-deploy` only. After "Deploy to production": copy + run the network helper with `--if-exists` (own pair of steps with `always() && !cancelled()`: independent of the image pull, of the env gate and of the API step's outcome, because the api/worker recreate drops the attachment even when the image step fails, the gate skips, or the API health loop times out after `up -d`). Then in the sidecar deploy step: network helper again, db-init, `up -d --no-deps whatsapp-redis`, wait healthy (12 x 5 s, else the sidecar is not started), `up -d --no-deps whatsapp-sidecar`, health, limits, the new Redis `INFO memory` lines, smoke |
| Smoke | New opt-in checks (`SMOKE_ISOLATION=1`, passed by production CD only, so staging output is unchanged): `redis`, `qdrant`, `pgbouncer` and their container names (`luran_redis`, ...) do not resolve from the sidecar; from the host, the sidecar is on **exactly** `luran_wa_backend` and `luran_wa_redis`; the sidecar's Redis URL has a password and is not host `redis`; a raw `PING` without credentials gets `-NOAUTH`. With `SMOKE_BACKEND_CONTAINERS="luran_api luran_worker"`: those containers' env holds none of the sidecar-only secret names (checked by name, values never printed) |

### Sizing for the owner's 10-session wish (ceiling values NOT changed here)
Redis load is independent of the session ceiling: at 10 sessions about 20 live keys (under 5 KiB), about 3 lease renewals per second, 1 sidecar connection. `maxmemory 8mb`, 32m and `maxclients 32` stay valid at 10 and well beyond. The things that must move with a higher ceiling are unchanged and are sidecar-side: `WHATSAPP_MAX_SESSIONS`, `WHATSAPP_RSS_REFUSE_MB`, `WHATSAPP_SIDECAR_MEM_LIMIT`, `WHATSAPP_MAX_CONNECTIONS` (see "To raise the ceiling" in `whatsapp_sidecar_production_deploy.md`; still gated on the soak). Postgres connections are not affected by this change.

## What's involved
| File | Change |
|---|---|
| `docker-compose.whatsapp.production.yml` | `whatsapp-redis` service, networks `wa_redis` (internal) and `wa_backend` (external), volume `wa_redis_data`; the sidecar's `networks`, `depends_on: whatsapp-redis`, and the built `WHATSAPP_SIDECAR_REDIS_URL` |
| `scripts/whatsapp_sidecar_network.sh` (new) | Idempotent: refuses unless `luran_api` is compose project `luran`; creates `luran_wa_backend` if missing (refuses an existing one that is internal or not a bridge); attaches `luran_api` (alias `api`), `luran_worker`, `luran_db` (alias `db`) if not attached. Also **fails if any container other than the four expected is on the network** (a hand-run `docker network connect luran_wa_backend luran_redis` would defeat the isolation without changing any DNS result). `--if-exists` never creates the network and does nothing when it is missing. Names only |
| `scripts/prepare_production_whatsapp_env.sh` | `WHATSAPP_SIDECAR_REDIS_PASSWORD` is sidecar-only and required, `[A-Za-z0-9]{32,}` |
| `scripts/generate_whatsapp_production_secrets.sh` | Writes the new name; **`--redis-only`** writes only that one line, for an env that already has the other secrets (so the master key is never regenerated) |
| `scripts/smoke_whatsapp_sidecar.sh` | Opt-in isolation checks (above). Default output unchanged |
| `.github/workflows/cd.yml` | Inside `production-deploy` only (everything before that job is byte-identical to `origin/develop`): two new steps (`sidecar_net_copy`, `sidecar_net`), edits inside the sidecar copy/deploy/status steps |
| `tests/deploy/` | 94 tests (was 55): gate and generator for the new name, the network helper against a stub `docker` with state, static invariants for the compose override and `cd.yml` |
| **Not touched** | `docker-compose.prod.yml`, `docker-compose.whatsapp.yml` (staging), `staging-*` jobs, `docker.yml`, `ci.yml`, `Dockerfile`, any Python under `app/`, the sidecar source |

## Which prod containers restart on deploy
**None.** `luran_api`, `luran_worker`, `luran_db`, `luran_redis`, `luran_qdrant`, `luran_pgbouncer` are only touched by `docker network connect` (no restart). The rehearsal compared container ids, `StartedAt` and restart counts: unchanged. What does change: `luran_whatsapp_sidecar` is recreated once (to move networks; dark, no sessions on prod today) and `luran_whatsapp_redis` is created. The existing prod deploy step already recreates api and worker on every deploy; that is not new.

## Verification
All in throwaway containers on a local Docker (not the VPS). Setup: real `docker-compose.prod.yml` bringing up the real `db`, `redis` and `qdrant` services (project `luran`); `luran_api` (a stand-in that answers the webhook 503 `whatsapp_disabled`), `luran_worker` and `luran_pgbouncer` (inert) as labelled stand-ins; the real sidecar image built from this tree; the real `sidecar_net` and `sidecar_deploy` scripts extracted from `cd.yml` and executed. Transcripts: the session notes folder (`redis-isolation-rehearsal/`).

| Claim | Result |
|---|---|
| Before (old layout, same stack) | Sidecar on `luran_default`; resolves `redis`, `qdrant`, `pgbouncer`; an unauthenticated `PING` to the shared Redis returns `+PONG` |
| Sidecar healthy after the new deploy | `WhatsApp Redis is healthy.`, `WhatsApp sidecar is healthy.`, smoke **18 PASS + 1 SKIP** (the SKIP is the bad-signature check, not testable while `WHATSAPP_ENABLED` is off) |
| Networks | sidecar: `luran_wa_backend luran_wa_redis` only; Redis: `luran_wa_redis` only (`internal=true`); `luran_default` members unchanged (no sidecar); no published ports; read-only, caps dropped |
| Sidecar cannot reach shared services | `redis`, `qdrant`, `pgbouncer` -> `ENOTFOUND`; raw TCP to the shared Redis, Qdrant and pgbouncer by **IP** -> timeout |
| Sidecar still reaches what it needs | `api:8000` (webhook probe: 503 `whatsapp_disabled`) and `db:5432` (role `whatsapp_sidecar`, no SELECT on `public`, no CREATE) |
| Its Redis requires the password | No password -> `NOAUTH`; wrong password -> `WRONGPASS`; the Redis container has no route out (`wget 1.1.1.1` -> network unreachable) |
| Real lease and counter code work under the ACL | The repo's real `Lease`/`RedisLeaseBackend` (Lua EVAL) and `RedisCounter`: acquire, strict verify, second holder refused, release, `INCR`+`EXPIRE` all allowed. Refused with `NOPERM`: `SET other:key`, `GET arq:queue`, an EVAL touching a non-`wa:` key, `FLUSHALL`, `CONFIG GET`, `KEYS *`, `REPLICAOF` |
| api/worker hold no Redis password | The real `docker-compose.prod.yml` rendered with the shipped `.env`: api and worker contain none of the three sidecar-only names nor `WHATSAPP_SIDECAR_REDIS_URL`, and no secret value; `docker inspect` of the stand-ins does not contain the password; the password is not in the Redis Cmd/Entrypoint or `/proc/1/cmdline` |
| Rerun is a no-op | Second run of both real scripts: same container ids, same `StartedAt`, 0 restarts; the network helper prints `already on` x3 |
| Recreated api loses the network, helper repairs it | Recreating `luran_api`: the smoke's webhook check FAILs; after the helper it PASSes |
| Persistence | Counter value survives `docker restart` and remove + recreate (volume kept); last `bgsave` ok |
| Memory | Redis idle RSS 13.4 MiB, docker stats 9.1 MiB, dataset 0.6 KiB; sidecar 70 MiB idle |
| Intruder / drift | A hand-attached `luran_redis` on `luran_wa_backend`: the helper exits 1 naming it (also in `--if-exists` mode); the sidecar hand-attached to `luran_default`: the smoke FAILs the exact-networks check and all six DNS checks |
| Fail closed | `docker compose config` without the password -> `required variable WHATSAPP_SIDECAR_REDIS_PASSWORD is missing a value`; gate tests: old names without the password skip with the name only; the generator output passes the gate |
| Static | 94 pytest tests in `tests/deploy/`; `ruff check app/ tests/deploy`, `black --check app/ tests/deploy` (project toolchain, ruff 0.8.6 / black 24.10.0); shellcheck on all WhatsApp scripts and on both extracted `cd.yml` scripts; actionlint on `cd.yml` shows only the 2 pre-existing `SC2086` infos |

### Review
Opus review (devops-engineer agent, read-only) of the final diff. Fixed:
1. **High:** the network steps were gated on the sidecar env gate and had no `always()`, so an API step that timed out after `up -d`, or a later deploy where the gate skipped, left an already-running isolated sidecar cut off from the recreated api/worker. Now `always() && !cancelled()`, gated on the VPS instead (`--if-exists`: nothing to do when the network does not exist), with a test.
2. **Medium:** the isolation proof was service-name DNS only, which a hand-run `docker network connect luran_wa_backend luran_redis` would pass. The helper now fails on foreign members; the smoke asserts the sidecar's exact network set from the host and DNS-checks the container names too. Both demonstrated in the rehearsal.
3. **Medium:** old sidecar names with no Redis password skipped quietly (green run), leaving an already-running pre-isolation sidecar on the shared Redis. The gate now prints `::warning::` (names only) whenever names exist and it declines to deploy.
4. **Low:** an unhealthy new Redis was left restart-looping; the deploy step now stops it unless the running sidecar already uses it.
5. **Low (documentation):** the password is **not** on any command line or in the container `Cmd`, but it **is** in `docker inspect` `Config.Env` of the Redis container (`WA_REDIS_PASSWORD`) and the sidecar (`WHATSAPP_SIDECAR_REDIS_URL`), readable by anyone in the docker group, same as the other sidecar secrets. The earlier wording is corrected above.

Not changed, on purpose: the daily-cap reset at cutover (the review's finding 4: `wa:daily:*` lived in the shared Redis DB 1 and the new Redis starts empty). The prod sidecar is dark; the shared Redis showed only `db0` on 2026-10-30. Owner pre-check below; a non-empty DB 1 means copy the keys first. The floating `redis:7-alpine` tag is the same as prod's Redis and stays a follow-up. Reviewer-confirmed fine: ACL covers everything the sidecar uses, password charset safe in printf/ACL/URL, volume ownership with `user: redis` + read-only, compose merge drops the implicit default network, `grep -q` under pipefail, the smoke keeps staging's exit status.

### NOT verified (nothing ran on the VPS or a GitHub runner)
- The new `cd.yml` steps on a real runner (scp of the new script, ssh, `docker load`), and the GHCR image promotion.
- That Linux bridge isolation on the VPS behaves as on the rehearsal host (Docker Desktop VM). It is Docker's standard `DOCKER-ISOLATION` behaviour; the check after deploy is the smoke's DNS and the by-IP probes below.
- `docker network connect --alias` on the VPS's Docker engine against the live prod containers (only Compose 2.37.1 was read from the VPS, read-only).
- The existing VPS sidecar's current state: a read-only look on 2026-10-30 showed `luran_whatsapp_sidecar` already running on `luran_default` (the earlier SOP said nothing had run on the VPS); the live `.env.sidecar` contents were not read. The first deploy with the password migrates it.
- A real WhatsApp session through the new Redis (the real lease and counter classes were exercised against it, no socket), and memory at 3 or 10 sessions.
- Real `luran_api` and `luran_worker` images (stand-ins were used; their env was proven from the real compose file instead).

## Owner actions (Adebayo), in order
Nothing needs a secret value in chat.
1. **Lead merges this PR to `develop`.** Do not put the new name into `.env.production.enc` on a branch that reaches `main` without this PR's scripts: an older `prepare_production_whatsapp_env.sh` strips only the master key and DB password, so the Redis password would land in the api and worker env (the ordering trap from the production SOP, one more name).
2. Generate only the new value (prints nothing; writes `.whatsapp-production-secrets.env` with one line, mode 0600; it refuses to overwrite, so delete or move an old file first or pass a path):
   ```bash
   scripts/generate_whatsapp_production_secrets.sh --redis-only
   ```
3. `./scripts/env.sh decrypt production`, copy the one `WHATSAPP_SIDECAR_REDIS_PASSWORD=` line across in your editor (do not echo it), `./scripts/env.sh encrypt production`, `./scripts/env.sh verify production`; delete both plain files. Commit only `.env.production.enc` on `chore/production-whatsapp-redis-env`, PR to `develop`. Keep `WHATSAPP_ENABLED` as it is.
4. Promote `develop` to `main` in a quiet window (not near 02:00). Until the name exists, any promotion logs `SKIPPED - missing or empty: WHATSAPP_SIDECAR_REDIS_PASSWORD`, the running sidecar is left alone, and the api/worker env is clean.
5. Read the run. `Prepare production env` must say `ENABLED for deploy`. Then: `network luran_wa_backend: created` and three `attached`, `WhatsApp Redis is healthy.`, `WhatsApp sidecar is healthy.`, `maxmemory_human:8.00M`, `maxmemory_policy:noeviction`, then the smoke: **18 `PASS` and 1 `SKIP`** (all `isolation:` lines PASS). A `::warning::` means the API is fine and the sidecar is not; send the lead the step log (no secrets).
6. Spot checks on the VPS (read-only):
   ```bash
   ssh luran 'docker inspect luran_whatsapp_sidecar --format "{{range \$k,\$_ := .NetworkSettings.Networks}}{{\$k}} {{end}}"'
   ```
   expect `luran_wa_backend luran_wa_redis` and no `luran_default`. And by hand any time:
   ```bash
   ssh luran 'cd /opt/luran && SMOKE_ISOLATION=1 SMOKE_BACKEND_CONTAINERS="luran_api luran_worker" SMOKE_BACKEND=disabled bash scripts/smoke_whatsapp_sidecar.sh luran_whatsapp_sidecar'
   ```
7. **Before the first deploy that migrates the sidecar** (and only matters if WhatsApp was ever enabled): `ssh luran 'docker exec luran_redis redis-cli -n 1 dbsize'` should be `0` (it held nothing on 2026-10-30; only `db0` existed). If not, the `wa:daily:*` send counters live there and the new Redis starts empty, so each number could send up to twice its daily cap on cutover day: migrate those keys first or cut over with `WHATSAPP_ENABLED=false`.
8. Only then continue with the production SOP's flip of `WHATSAPP_ENABLED` and the first tenant.

## Operate / roll back
Operate: smoke (step 6), `docker logs luran_whatsapp_redis`, `docker stats --no-stream luran_whatsapp_redis luran_whatsapp_sidecar`.

**Gotcha, read this:** `luran_api` and `luran_worker` lose the `luran_wa_backend` attachment whenever compose recreates them. The production deploy does that every time and the helper steps re-attach right after. A **manual** `docker compose ... up -d` on the VPS, or any recreate, needs `cd /opt/luran && bash scripts/whatsapp_sidecar_network.sh`. Symptoms if forgotten: sidecar smoke FAILs `backend reachable`, webhooks do not arrive, outbound sends log `SidecarUnavailable` and retry. Between a deploy's api recreate and the re-attach step (about a minute) the same applies.

Roll back, smallest first: (1) `WHATSAPP_ENABLED=false` + deploy: nothing reaches the sidecar (unchanged kill switch). (2) `ssh luran 'docker stop luran_whatsapp_sidecar luran_whatsapp_redis'`: credentials stay in Postgres, the counter stays on its volume; the next deploy starts both again while the names stay in the env. (3) Revert the PR: removes the CD steps and the override; a running sidecar and Redis are **not** stopped by CD, and the next deploy that still has the old files puts the sidecar back on `luran_default` with the shared Redis (the old password-less layout) only if the env still passes the old gate; stop the new Redis by hand. (4) Remove the isolation entirely:
```bash
docker rm -f luran_whatsapp_redis
docker volume rm luran_wa_redis_data
for c in luran_api luran_worker luran_db luran_whatsapp_sidecar; do docker network disconnect luran_wa_backend "$c" || true; done
docker network rm luran_wa_redis luran_wa_backend
```
Rotate the password: change `WHATSAPP_SIDECAR_REDIS_PASSWORD` in the encrypted env and deploy; both the Redis (ACL file is rewritten on start) and the sidecar URL are recreated by the same step. Never add `--remove-orphans` to any production compose step (it deletes the sidecar and its Redis). `docker compose down` does not remove `luran_wa_backend` (external).

## Rulings made (cost if wrong)
- **Runtime `docker network connect` instead of editing `docker-compose.prod.yml`.** *If wrong:* a manual recreate of api/worker silently cuts the sidecar off until the helper runs (loud in the smoke, retried by arq). The alternative recreates Postgres on the next deploy. The permanent fix for api and worker alone is in Follow-ups.
- **`noeviction` and 8 MB.** *If wrong (too small):* writes fail at 8 MB, the sidecar fails closed and stops sending; at about 5 KiB real data this cannot happen without abuse of a stolen credential. The alternative (`allkeys-lru`) lets an attacker with the credential evict leases.
- **`save 60 1`.** *If wrong:* at most 60 s of counter increments lost on a hard crash; a clean stop saves on SIGTERM.
- **Sidecar keeps internet egress and reaches `api:8000`, `db:5432`.** Needed (WhatsApp, Cloudinary, webhook, Postgres). A compromised sidecar can still attack those two; the DB role is least-privilege and the webhook is HMAC-checked. Egress filtering is a follow-up.
- **The base file's `depends_on: redis` stays in the merged config.** Inert: every production step uses `--no-deps`, and the sidecar has no network to that Redis. Removing it needs Compose's `!reset` tag, which breaks the YAML the tests parse and depends on the VPS compose version.

## Follow-ups
- Declare `luran_wa_backend` on `api` and `worker` in `docker-compose.prod.yml` (they are recreated on every deploy anyway) to close the re-attach window. **Do not add it to `db`** (that recreates Postgres); the helper keeps handling `db`.
- Authentication for the shared production Redis (arq queue; pickle jobs) and an API key for Qdrant: this change removes the sidecar from their blast radius, it does not protect them from a compromised api or worker.
- Egress filtering for the sidecar (allow-list WhatsApp, Cloudinary, `api`, `db`), for example a `DOCKER-USER` rule.
- Pin `redis:7-alpine` (and the sidecar image) by digest; same moving-tag caveat as prod's Redis.
- After the first live run, record the real `INFO memory` and `used_memory_rss` at 3 and 10 sessions in this SOP.
