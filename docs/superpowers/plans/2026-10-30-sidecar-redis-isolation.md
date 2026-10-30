# Plan: isolate the production WhatsApp sidecar's Redis (and take it off the shared network)

**Date:** 2026-10-30 · **Branch:** `feature/sidecar-redis-isolation` (PR to `develop`, **not merged by this track**: promotion to `main` deploys prod with no staging gate) · **Post-ship record:** `docs/sop/whatsapp_sidecar_redis_isolation.md`

**Problem.** In prod the sidecar uses the shared prod Redis (logical DB 1) on `luran_default`, where Redis and Qdrant have no password (`docs/sop/whatsapp_sidecar_production_deploy.md`, "Rulings made"; staging plan section 4). The sidecar parses untrusted WhatsApp traffic. A compromise could write Redis DB 0 (arq queue; pickle jobs run in the worker, which holds the full env) or read/overwrite Qdrant.

**Goal.** The sidecar gets its own authenticated Redis on a private network, and can no longer reach prod Redis, Qdrant or pgbouncer. It keeps the webhook path to `luran_api` and the direct Postgres path (role `whatsapp_sidecar`). No prod container restarts.

---

## 1. Findings (REPO unless noted)

| Question | Answer | Evidence |
|---|---|---|
| What does the sidecar keep in Redis? | Only `wa:lease:{session_key}` (owner token, TTL 30 s, renewed every 10 s) and `wa:daily:{session_key}:{day}` (send counter, TTL 26 h). ~100 B each. 10 sessions = about 20 live keys, under 5 KiB | `whatsapp-sidecar/src/lease.mjs`, `limiter.mjs`; `grep` for other `redis` use in `src/` finds only `index.mjs` wiring |
| Does the api or worker read or write them? | **No.** `grep -rn 'wa:' app/` finds nothing; the backend never sees `WHATSAPP_SIDECAR_REDIS_URL`. The backend talks to the sidecar over HTTP only (`app/channels/linked_device.py`), from **api and worker** (`app/workers/whatsapp.py` sends and runs the capacity cron) | code |
| Fail behaviour | Fail closed already: offline queue off, 3 s command timeout, lease heartbeat ends the socket if it cannot renew, an evicted lease key is re-taken with `SET NX`, a daily-counter error refuses the send | `index.mjs`, `lease.mjs`, `limiter.mjs` |
| Who must reach the new Redis? | **Only the sidecar** (plus the smoke run inside the sidecar container and the Redis healthcheck inside its own container). Not api, not worker | above |
| What does the sidecar need on the network? | `api:8000` (webhook), `db:5432` (Postgres, bypassing pgbouncer), WhatsApp servers and `res.cloudinary.com` (internet egress) | `docker-compose.whatsapp.yml`, `config.mjs` |
| What is on the VPS now? | `luran_whatsapp_sidecar` is already running (dark) on `luran_default` with `luran_api/worker/db/redis/qdrant/pgbouncer`; prod Redis holds `db0` only (3 keys), `db1` empty. Compose on the VPS is 2.37.1. **HOST, read-only `ssh luran`, 2026-10-30** | `docker ps`, `redis-cli info keyspace` |

### Persistence: yes, but small
Leases are 30 s keys; losing them just means the sidecar waits one TTL. The **daily send counter** is the only state worth keeping: it is the anti-ban guard (`DAILY_SEND_CAP`, 200 per session per day). A Redis restart with no persistence resets it, so a number could send up to 2x its cap in one day. Persist with an RDB snapshot **`save 60 1`** (any change, at most every 60 s) on a named volume: a few KiB written per minute at most, no AOF. The shared Redis's `--save 60 1000` almost never fires at this write rate (about 1 write per second), which is why it is not copied.

### Memory
Dataset under 5 KiB at 10 sessions (the owner may raise the ceiling to 10; do not change the ceiling here). `maxmemory 8mb`, policy **`noeviction`** (a lease or counter must never be silently evicted; at the cap writes error and the sidecar fails closed, which it already does), container `mem_limit` sized from a measurement in the rehearsal, no swap, `maxclients 32`. Command-rate at 10 sessions: about 3 lease renewals per second.

## 2. Design

```
 luran_default (UNCHANGED)              luran_wa_backend  (new, external, bridge, egress ok)
  api  worker  db  pgbouncer             api  worker  db   <── attached live by `docker network connect`
  redis  qdrant                          whatsapp-sidecar  ── internet (WhatsApp, Cloudinary)
        ▲ no sidecar                          │
                                    luran_wa_redis  (new, internal: true, no gateway, no egress)
                                      whatsapp-sidecar ──► whatsapp-redis  (ACL user, password only in .env.sidecar)
```

| Decision | Why | Rejected |
|---|---|---|
| New `whatsapp-redis` service in `docker-compose.whatsapp.production.yml` on an `internal: true` network shared only with the sidecar | Nothing else can address it, and it has no route out | Reusing prod Redis with ACLs (shared memory, shared blast radius, needs a prod Redis restart) |
| Sidecar leaves `luran_default` and joins `luran_wa_redis` + `luran_wa_backend` | Docker cannot filter inside a network; only removing it from `luran_default` removes Redis/Qdrant/pgbouncer reachability | Keeping it on `luran_default` and relying on passwords (Qdrant has no key; one more compromised hop) |
| `luran_wa_backend` is created and populated **at runtime** by `scripts/whatsapp_sidecar_network.sh` (`docker network create`, `docker network connect --alias api|db`) | Attaching api/worker/db through `docker-compose.prod.yml` would recreate **db** (Postgres) and change api/worker config. A runtime connect restarts nothing | Editing the prod compose (recreates `luran_db`, `luran_api`, `luran_worker`) |
| The attach runs on **every** prod deploy, in its own step independent of the image pull | `up -d` recreates api and worker each deploy, which drops the live attachment. Tying it to the sidecar image step would leave a running sidecar cut off whenever the GHCR pull fails | Doing it inside the sidecar deploy step only |
| ACL file instead of `requirepass`: default user `on`, password from env, `~wa:*`, `-@admin -@dangerous +info` | The password is written to a tmpfs file at start, never on a command line, `docker inspect` Cmd or `ps`; the credential is limited to the `wa:` keys and has no CONFIG/DEBUG/REPLICAOF/FLUSHALL | Plain `--requirepass` (password in `ps`, full command set) |
| Password in `.env.sidecar` only (`WHATSAPP_SIDECAR_REDIS_PASSWORD`, `[A-Za-z0-9]{32,}`, stripped from `.env` like the master key) | Never in the api/worker `env_file` | A shared name in `.env` |
| `WHATSAPP_SIDECAR_REDIS_URL` is built in the override from the password; an inherited value is ignored | The owner cannot point the sidecar back at the shared Redis by accident | Honouring the old override variable |
| Prep gate: any sidecar name present but no Redis password => **skip with names**, secrets still stripped | Fail closed: never deploy the old shared-Redis layout again | Defaulting to the shared Redis when the password is missing |

## 3. Which prod containers restart

**None.** `docker-compose.prod.yml` is not touched. The attach is `docker network connect` on running containers. Only `luran_whatsapp_sidecar` (already dark, no sessions) is recreated, once, to move networks, plus the new `luran_whatsapp_redis` starts. Consequence: api and worker are recreated by every prod deploy anyway; each deploy re-attaches them (step below). Between that recreate and the attach (about a minute) api to sidecar calls fail with `SidecarUnavailable` and are retried by arq. Follow-up (not done here): declare `luran_wa_backend` on api and worker in the prod compose to remove the window; they restart each deploy regardless, but **db must not** be added there (it would recreate Postgres).

## 4. Work items (TDD: tests first, in `tests/deploy/`)

1. `prepare_production_whatsapp_env.sh`: new required, sidecar-only name `WHATSAPP_SIDECAR_REDIS_PASSWORD` (alnum >= 32), stripped into `.env.sidecar`. No names at all still a byte-identical no-op.
2. `generate_whatsapp_production_secrets.sh`: writes the new name in the sidecar-only block (hex, 48 chars).
3. `scripts/whatsapp_sidecar_network.sh` (new): idempotent create + attach; refuses unless `luran_api` is project `luran`; prints names only. Tested with a stub `docker`.
4. `docker-compose.whatsapp.production.yml`: `whatsapp-redis` service, two networks, the sidecar's `networks`, URL and `depends_on`.
5. `scripts/smoke_whatsapp_sidecar.sh`: new checks that run in the sidecar container: `redis` / `qdrant` / `pgbouncer` hostnames do not resolve, the sidecar's Redis refuses an unauthenticated command (`NOAUTH`), the default-network Redis is not the configured one. Old checks unchanged.
6. `cd.yml` hunks inside `production-deploy` only: an attach step (copy + ssh) before the sidecar steps; the sidecar deploy step starts `whatsapp-redis` first, waits healthy, prints the new Redis `INFO memory`, and prints that api/worker env carry no `WHATSAPP_SIDECAR_REDIS_PASSWORD`.
7. Rehearsal (throwaway containers, the real script extracted from `cd.yml`), review agent, SOP, checklist, deploy SOP edits.

## 5. Owner steps (detail in the SOP)

Generate the new value with `scripts/generate_whatsapp_production_secrets.sh` (a new file; only the one new line is needed), add `WHATSAPP_SIDECAR_REDIS_PASSWORD` to `.env.production.enc` yourself, merge, promote. Until the name exists the next prod deploy logs `SKIPPED - missing or empty: WHATSAPP_SIDECAR_REDIS_PASSWORD` and leaves the running sidecar alone.
