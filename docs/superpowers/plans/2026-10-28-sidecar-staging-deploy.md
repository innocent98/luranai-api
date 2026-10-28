# Plan: run the WhatsApp sidecar on STAGING through the existing pipeline

**Date:** 2026-10-28 · **Branch:** `feature/sidecar-staging-deploy` (PR to `develop`, **not merged by this track**: merging deploys to staging through CD) · **Post-ship record:** `docs/sop/whatsapp_sidecar_staging_deploy.md`

**Goal.** Make the sidecar runnable on staging so Adebayo can run a live test: pairing with the test number, a real escalation that rings the Flutter agent app, and an agent reply reaching WhatsApp. Production is not touched.

**Safe to merge before any secret exists.** Until the owner adds the secrets, the staging deploy behaves as today (plus a higher api memory limit) and logs `WhatsApp sidecar: SKIPPED - missing or empty: <names>`.

---

## 1. Findings (evidence)

Legend: **HOST** = read-only `ssh luran` on 2026-10-28 ~11:48 WAT · **REPO** = read from this repo · **REHEARSAL** = ran locally in throwaway containers (not the VPS).

### 1.1 How staging is built and deployed today (REPO + HOST)

| Fact | Evidence |
|---|---|
| Staging and prod are **separate Docker Compose projects** from the **same file** `docker-compose.prod.yml`; the project name comes from `COMPOSE_PROJECT_NAME` in the (encrypted) env | HOST `docker inspect`: staging containers have label `com.docker.compose.project=luran_staging`, working dir `/opt/luran-staging`; prod `luran`, `/opt/luran` |
| Separate bridge networks: `luran_staging_default` and `luran_default` (user-defined bridges are isolated from each other by Docker's default iptables rules) | HOST `docker network ls`; every staging container is only on `luran_staging_default` (172.19.0.x), prod only on `luran_default` (172.18.0.x) |
| Staging api is the only published port: `127.0.0.1:8001->8000` (nginx on the host proxies it). Prod api `127.0.0.1:8000`. db/redis/pgbouncer publish nothing | HOST `docker inspect` ports |
| CD: `cd.yml` `staging-deploy` runs on `workflow_run` of CI success on `develop` (or manual dispatch `environment=staging`). It **builds the api image on the runner** (`docker build`), `docker save`s it, decrypts `.env.staging.enc` to `.env` with `ENV_ENCRYPTION_KEY` (openssl aes-256-cbc), `scp`s image + `docker-compose.prod.yml` + `alembic/` + `.env` + `scripts/backup_cron.sh` to `DEPLOY_PATH`, then over ssh: `docker load`, migrations, `docker compose -f docker-compose.prod.yml up -d`, health loop, image prune | REPO `.github/workflows/cd.yml` lines 30-170 |
| Prod: `main` push -> `production-deploy` **pulls `ghcr.io/<repo>:develop`** (built by `docker.yml` on develop), no rebuild, its own env decrypt and `DEPLOY_PATH` (GitHub **environment**-scoped secrets: `staging` vs `production`) | REPO cd.yml 329-480 |
| GitHub secret NAMES involved: `ENV_ENCRYPTION_KEY`, `VPS_HOST`, `VPS_USERNAME`, `VPS_SSH_KEY`, `VPS_SSH_PASSPHRASE`, `VPS_PORT`, `DEPLOY_PATH`, `API_PORT`, `APP_URL`, `LURAN_E2E_*`, `GITHUB_TOKEN` | REPO |
| `docker.yml` builds and pushes only the API image, on `develop` and `v*` tags; PRs to `main` build without pushing; `main` never pushes | REPO |
| The staging env is the committed, encrypted `.env.staging.enc`; the owner edits it with `scripts/env.sh decrypt/encrypt staging` and commits | REPO `scripts/env.sh`, `docs/deployment/predeploy_guide.md` |
| The deployed `/opt/luran-staging/.env` is mode `644` (world-readable on the host) | HOST `ls -la` (the new prep step makes the shipped file `600`; scp mode preservation is unverified until the first run) |

### 1.2 Real memory and limits (HOST, `docker stats --no-stream`, `docker inspect`)

| Container | Limit | Used | Notes |
|---|---|---|---|
| `luran_staging_api` | **500 MiB** (swap cap 1000 MiB) | 221 MiB (42 min after a restart; 448 MiB seen earlier by the lead = 90%) | `API_MEM_LIMIT` is set in the staging env |
| `luran_staging_worker` | 1 GiB | 150 MiB | |
| `luran_staging_redis` | 64 MiB | 5.7 MiB | `redis-server --maxmemory 48mb --maxmemory-policy allkeys-lru --save 60 1000` |
| `luran_staging_qdrant` | 192 MiB | 29.7 MiB | |
| `luran_staging_db` | 384 MiB | 30.8 MiB | `shared_buffers=128MB`, **`max_connections=40`** |
| `luran_staging_pgbouncer` | 48 MiB | 1.7 MiB | pool 20 (+5 reserve), transaction mode |
| prod api / worker / qdrant / redis / db / pgbouncer | 1.172 GiB / 1.953 GiB / 1 GiB / 384 MiB / 1.367 GiB / 128 MiB | **994 MiB (83%)** / 195 / 164 / 6 / 63 / 3.75 MiB | prod api is close to its own limit |

Host: 7.8 GiB RAM, **6.0 GiB available**, 1.7 GiB used, swap 8 GiB (1.5 MiB used), 4 vCPU, load 0.5, disk 122 GB free of 145 GB (16% used), Docker images 9.1 GB (4.4 GB reclaimable). Sum of all container limits exceeds RAM already; real use is ~1.9 GiB.

### 1.3 Can staging Postgres host the sidecar schema? (REPO + HOST + REHEARSAL)

- The sidecar creates `whatsapp_sidecar.{sessions,auth_blobs,inbound_chats}` at boot (`whatsapp-sidecar/src/db.mjs`) under a **session-level advisory lock** and holds a pool of **4** long-lived connections. It runs its own SQL.
- **pgbouncer: bypass it.** Transaction pooling breaks session-level advisory locks, and pgbouncer authenticates a fixed userlist (`AUTH_TYPE scram-sha-256`) that would not know a new role. The sidecar connects straight to `db:5432` (the existing override already did this).
- **Role: dedicated least-privilege role `whatsapp_sidecar`, not the app role.** The app role is the Postgres superuser (`POSTGRES_USER` of the image). The auth store is AES-GCM encrypted with a key the database never sees, but a compromised sidecar holding a superuser URL would own every tenant's data; with its own role it can reach only its own schema. REHEARSAL on `postgres:15-alpine` proved: `SELECT` on an app table -> `permission denied`, `CREATE TABLE public.x` -> `permission denied for schema public`, and the sidecar boots and runs its DDL as that role. `CONNECT, CREATE ON DATABASE` is required because Postgres checks database-level `CREATE` before honouring `CREATE SCHEMA IF NOT EXISTS`; it allows creating schemas only.
- **Connection budget** (`max_connections=40`, 3 reserved for superuser): pgbouncer up to 25 + sidecar 4 (role `CONNECTION LIMIT 8`) + backup/one-shots ~2 = ~35 of 37. Tight but fits; do not raise `MAX_SESSIONS` and the pool together without raising `POSTGRES_MAX_CONNECTIONS`.

### 1.4 Is staging Redis safe for the lease keys? (REPO + HOST + REHEARSAL)

- Keys: `wa:lease:{session_key}` (~100 B, TTL 30 s, one per session) plus daily send counters. Total well under 1 KiB against a 48 MB `maxmemory`; current use 5.7 MiB. Eviction would need the rest of Redis to fill 48 MB.
- **Fail closed (read in `lease.mjs`, `index.mjs`; covered by the sidecar suite):** the client uses `enableOfflineQueue:false`, `commandTimeout:3000`, so every call fails fast when Redis is down; acquire failure = `lease_unavailable`; a heartbeat that cannot renew within `ttl/2` ends the socket; an evicted key is re-created with `SET NX` and only counts as ours if nobody took it, and a 401 only counts as a real logout when the key was still ours (`verify({strict:true})`).
- **Dedicated logical DB 1** (`redis://redis:6379/1`), default in the override. The backend and arq use DB 0 (`REDIS_URL`); the CD cleanup steps scan DB 0 only. Honest limit: a logical DB separates the namespace, not memory or eviction (`maxmemory` is per instance). Key prefix `wa:` is hard-coded in the sidecar (no configurable prefix, none needed).
- Persistence: `--save 60 1000` snapshots both DBs; a restart restores leases only if still within TTL, otherwise sessions re-acquire on boot.

### 1.5 Network paths (REPO + REHEARSAL)

- backend -> sidecar: `WHATSAPP_SIDECAR_URL=http://whatsapp-sidecar:8787`, compose service-name DNS on `luran_staging_default`. **No `ports:`** on the sidecar (REHEARSAL: `ports=map[]`).
- sidecar -> backend: `BACKEND_BASE=http://api:8000`, webhook `POST /api/v1/webhooks/whatsapp/linked`. It bypasses nginx, so there are no edge limits; the handler is authenticated only by the HMAC, caps the body at 256 KiB, has no `TrustedHost` middleware (grep), and has no IP rate limit by design.
- The sidecar's own HTTP API requires the bearer token on **every** route including `/health` (`api.mjs`: the auth check precedes routing). Inside the container the image `HEALTHCHECK` reads the token from the container env, so nothing prints it.
- Prod cannot reach it: different network, no published port, and the staging project name is guarded in the prep script and in the ssh step.

### 1.6 Push (FCM) for the agent app (names only)

The running staging api has these names set in its environment: `FIREBASE_CREDENTIALS_JSON`, `AGENT_ALERT_ENABLED`, `AGENT_ALERT_WAIT_SECONDS`, `AGENT_ALERT_ANDROID_CHANNEL_ID`. **Values were deliberately not read.** Code defaults: `AGENT_ALERT_ENABLED=True`, wait 90 s, channel `luran_escalation`. Whether the credential is valid and is the Firebase project the staging Android build points at is **unverified** (owner action 5). The escalation ring is wired inside the chat pipeline (`send_message`), which the WhatsApp inbound path calls; agent replies are forwarded to WhatsApp by the `fanout_event` hook. Both are code-level facts, not yet proven on staging.

### 1.7 Other observations (not fixed here)

- The staging `.env` was world-readable (644). The prep step sets `600` on the runner copy before it is shipped (to be confirmed with `ls -l` after the first deploy).
- Prod api sits at 83% of its limit and staging api hit 90%: the "why are the api containers memory-hungry" investigation is out of scope; the raise below is a stopgap.
- The existing "Deploy API to staging" script uses `pg_isready -U "${POSTGRES_USER:-postgres}"` with a variable that is not set in the ssh shell; it works only because `pg_isready` does not require the role to exist. Left alone.

---

## 2. Architecture on the shared VPS

```
GitHub Actions (cd.yml, staging-deploy)                       VPS (one host, 7.8 GiB)
 decrypt .env.staging.enc -> .env                     ┌── project luran_staging  (/opt/luran-staging) ─────────────┐
 prepare_staging_whatsapp_env.sh                      │ network luran_staging_default (no host ports but api:8001) │
   ├─ API_MEM_LIMIT floor 768m                        │  api ─────────► whatsapp-sidecar:8787  (bearer token)      │
   ├─ gate: required names present & well-formed?     │  api ◄───────── whatsapp-sidecar  webhook (HMAC)           │
   ├─ .env.sidecar = master key + DB password         │  whatsapp-sidecar ─► db:5432 (role whatsapp_sidecar, direct)│
   └─ sidecar_enabled=true|false                      │                   └► redis:6379/1 (leases, counters)       │
 build api image ──scp──► deploy API (unchanged)      └────────────────────────────────────────────────────────────┘
 build sidecar image ─scp─► db-init, up --no-deps sidecar, health, smoke      project luran (prod) — separate network, untouched
```

Design decisions:

| Decision | Why | Rejected |
|---|---|---|
| Staging builds the sidecar image on the runner and ships it by `docker save`/`scp`, like the API | Deterministic, no race with `docker.yml`, no GHCR login on the VPS | Pulling from GHCR (race: `docker.yml` and CI run in parallel on develop) |
| `docker.yml` also builds and pushes `ghcr.io/<repo>-whatsapp-sidecar` with the **same tag set** (develop/tags only; PRs to main build only; main never pushes) | Same convention; the artifact a later prod promotion would pull | A prod push (not asked, and the API pattern does not push on main either) |
| `ci.yml` build job builds the sidecar image on every PR/push | Without it nothing in a PR to `develop` proves the Dockerfile builds | Trusting the local build |
| Sidecar-only secrets go to `.env.sidecar`, not `.env` | `.env` is the `env_file` of api **and** worker; the api is the larger attack surface and must not hold the key that decrypts every linked WhatsApp credential | One shared env (leaks the master key and DB password into the api container) |
| Gate on names **and** format; skip, never fail | A crash-looping sidecar (`restart: unless-stopped` + bad token length -> exit 2) and a red staging deploy are worse than a skipped add-on | Fail the deploy when names are missing |
| Sidecar steps are `continue-on-error` + a status step | A broken optional service must not block `staging-smoke` (`needs: staging-deploy`) | Separate job (would re-decrypt and re-prepare) |
| `up -d --no-deps whatsapp-sidecar` only | Nothing else in staging is recreated by the sidecar step | Plain `up -d` with both files |
| Raise the api limit inside the prep step as a **floor** (never lowers a higher owner value) | Lives in repo, reviewable, staging-only; no owner edit needed | Editing the encrypted env (owner action, easy to forget); changing the compose default (also changes prod) |

---

## 3. Resource budget

| Item | Value | Basis |
|---|---|---|
| Sidecar `mem_limit` / `memswap_limit` | **384 MiB / 384 MiB (no swap)** | Highest measured single-session RSS 128-152 MB (local live run, spike); idle process 95-114 MB RSS (`/health` in REHEARSAL: 114 MB, container 122 MiB); 384 MiB is ~2.5x the worst single-session figure. No swap: on OOM the container is killed and restarts, sessions restore from Postgres without re-pairing, instead of swapping on a host production shares |
| `MAX_SESSIONS` / `RSS_REFUSE_MB` | **2 / 300** | One test number plus one spare; `POST /sessions` returns 503 `capacity_exceeded` at either; 300 leaves ~84 MiB below the limit for non-heap memory |
| Backend `WHATSAPP_MAX_CONNECTIONS` | 2 | Checked by the backend before it calls the sidecar |
| CPU | `cpus: 0.5` of 4 | Pairing and sends are I/O bound |
| `pids_limit` / `oom_score_adj` | 256 / 500 | Under host memory pressure the kernel kills the sidecar before production |
| Hardening | `read_only`, tmpfs `/tmp` 16 MiB, `cap_drop: ALL`, `no-new-privileges`, runs as uid 1000 | REHEARSAL: boots and is healthy under all of these |
| Logs | json-file, 10 MiB x 3 | Cap of 30 MiB; the sidecar never logs message text, numbers or pairing codes |
| Restart | `unless-stopped`, `stop_grace_period: 20s` | SIGTERM flushes credentials and releases leases |
| Staging api limit | **500 MiB -> 768 MiB floor** (swap cap becomes 1.5 GiB by Docker's 2x default) | The api reached 448 MiB (90%) at 1 gunicorn worker; 768 MiB puts that peak at 58%. Stopgap, not a diagnosis |
| Disk | sidecar image ~250 MB, 3 kept | 122 GB free |

Host impact: worst-case added commitment = (768 - 500) + 384 = **652 MiB** against 6.0 GiB available (11%). Expected added real use is ~100-150 MiB (the sidecar) since the api limit rises only if the api actually grows. **Multi-session RAM is unmeasured** (one session at most was ever observed; the soak script is a skeleton). The numbers above are guards, not a sizing. First live-test task: record `rss_mb` from `/health` at 0, 1 and 2 sessions (the smoke script prints it).

---

## 4. Failure isolation from production

| Risk | Control |
|---|---|
| Prod pipeline changed | `production-deploy`, `staging-smoke` and `staging-api-e2e` job text is **byte-identical** to `origin/develop` (proof in the SOP); the `cd.yml` diff is additions inside `staging-deploy` only; `docker-compose.prod.yml` is **unchanged** |
| Sidecar lands in prod project | `docker-compose.whatsapp.yml` is only copied by the staging job; its container name uses `${COMPOSE_PROJECT_NAME:?}` (no silent fallback to the prod name `luran`); the prep script and the ssh step both refuse unless `COMPOSE_PROJECT_NAME` is `luran_staging` |
| Network exposure | No published port; staging network only; every route needs the bearer token |
| Memory / CPU / pids | limits above; no swap; `oom_score_adj` 500 |
| Disk / logs | 30 MiB log cap; 3 sidecar images kept |
| Sidecar breaks staging deploy | `continue-on-error` on build, copy and deploy; status step warns |
| Secret exposure | scripts print names and pass/fail only; sidecar-only secrets kept out of the api env; `.env` and `.env.sidecar` shipped `600`; smoke runs inside the container; DB init silences statement logging while it handles the password |
| DB blast radius | dedicated role, no grants on application tables, `CONNECTION LIMIT 8`; smoke FAILs if the role can `SELECT` any `public` table |
| Other staging services restarted | `up -d --no-deps whatsapp-sidecar`; REHEARSAL: a later base-file-only `up -d` only prints an "orphan containers" warning and leaves the sidecar running. **Never add `--remove-orphans` to the existing steps** |

## 5. Rollback

| Level | Action |
|---|---|
| Instant kill switch (no deploy) | `WHATSAPP_ENABLED=false` in the staging env + redeploy: webhook 503, connection create 403, sender stops, fanout hook inert. Data untouched |
| Stop the container | `ssh luran 'docker stop luran_staging_whatsapp_sidecar'`. Credentials stay in Postgres; restart resumes without re-pairing. If the names are still in the env, the next CD run starts it again: remove a required name (or revert) to keep it off |
| Revert the PR | Removes the CD steps; **does not stop an already-running sidecar** (CD never stops it); stop it by hand. The api limit returns to the env value |
| Remove data | `DROP SCHEMA whatsapp_sidecar CASCADE; DROP ROLE whatsapp_sidecar;` (every linked number must re-pair and should be unlinked on the phone) |

---

## 6. Repo wiring (this PR)

| Piece | File |
|---|---|
| Sidecar service hardened + staging defaults; one-shot DB role init | `docker-compose.whatsapp.yml` |
| Role + schema SQL (idempotent) | `scripts/whatsapp_sidecar_db_init.sql` |
| Env prep + gate + split + api floor | `scripts/prepare_staging_whatsapp_env.sh` |
| Post-deploy smoke (pass/fail only) | `scripts/smoke_whatsapp_sidecar.sh` |
| Secret generator (prints nothing) | `scripts/generate_whatsapp_staging_secrets.sh` |
| CD staging steps | `.github/workflows/cd.yml` (`staging-deploy` only) |
| Image publish + PR build check | `.github/workflows/docker.yml`, `.github/workflows/ci.yml` |
| Git-ignore for the generated files | `.gitignore` |

### Env NAMES (owner adds to `.env.staging`)

| Name | Where it goes | Secret | Generated by the helper |
|---|---|---|---|
| `WHATSAPP_ENABLED` (`true`) | api/worker | no | yes |
| `WHATSAPP_SIDECAR_URL` (`http://whatsapp-sidecar:8787`) | api/worker | no | yes |
| `WHATSAPP_MAX_CONNECTIONS` (`2`) | api/worker | no | yes |
| `WHATSAPP_SIDECAR_TOKEN` | api/worker **and** sidecar | yes | yes |
| `WHATSAPP_WEBHOOK_SECRET` | api/worker **and** sidecar | yes | yes |
| `WHATSAPP_CONTACT_PEPPER` | api/worker | yes | yes |
| `WHATSAPP_AUTH_MASTER_KEY` | **sidecar only** (`.env.sidecar`) | yes, **back it up** | yes |
| `WHATSAPP_SIDECAR_DB_PASSWORD` | **sidecar only** | yes | yes |

Deviation from the brief's list, on purpose: the brief named `WHATSAPP_SIDECAR_DATABASE_URL`. The compose file now **builds** that URL for the container from `WHATSAPP_SIDECAR_DB_PASSWORD` and the dedicated role name, so the owner never composes a URL and cannot point the sidecar at the superuser by mistake. Likewise `WHATSAPP_SIDECAR_REDIS_URL` (default `redis://redis:6379/1`), `BACKEND_BASE` (default `http://api:8000`), `MAX_SESSIONS` (2) and `RSS_REFUSE_MB` (300) have staging defaults in the compose file; the owner only sets them to override (`WHATSAPP_SIDECAR_REDIS_URL`, `WHATSAPP_BACKEND_BASE`, `WHATSAPP_MAX_SESSIONS`, `WHATSAPP_RSS_REFUSE_MB`, `WHATSAPP_SIDECAR_MEM_LIMIT`, `WHATSAPP_SIDECAR_CPUS`).

Format rules enforced by the gate (else skipped with the offending NAME): token and webhook secret >= 32 chars; master key hex, even length, >= 64 chars; DB password `[A-Za-z0-9]{24,}`; `COMPOSE_PROJECT_NAME=luran_staging`.

---

## 7. Owner actions (Adebayo), in order

Nothing below needs a secret value in chat. Terminal commands run in a checkout of the repo.

**A. Prepare (can be done any time, nothing deploys)**

1. Generate the values (prints nothing; writes a 0600 file in the repo root, git-ignored):
   ```bash
   scripts/generate_whatsapp_staging_secrets.sh
   ```
2. Put `WHATSAPP_AUTH_MASTER_KEY` from `.whatsapp-staging-secrets.env` in your password manager now. Losing it means every linked number must re-pair.

**B. Land the code**

3. Review the PR and tell the lead "merge #N". Merging is safe without secrets: staging deploys as before, the api limit becomes 768 MiB, and the log shows `WhatsApp sidecar: SKIPPED - missing or empty: ...`. **Do this before step 4**: with the old CD the sidecar-only secrets would be copied into the api container's env; the new prep step always strips them.

**C. Add the secrets (this deploy brings the sidecar up)**

4. Add the names to the encrypted staging env using the existing process:
   ```bash
   ./scripts/env.sh decrypt staging          # writes .env.staging (git-ignored)
   ```
   Open `.env.staging` and `.whatsapp-staging-secrets.env` in your editor, copy the lines across (do not echo them), then:
   ```bash
   ./scripts/env.sh encrypt staging
   ./scripts/env.sh verify staging
   ```
   Delete `.whatsapp-staging-secrets.env` and `.env.staging`. Commit **only** `.env.staging.enc` on a `chore/staging-whatsapp-env` branch, PR to `develop`, tell the lead to merge. CI success on `develop` triggers the staging deploy, which now deploys the sidecar.
5. Watch the run: `Prepare staging env` must say `WhatsApp sidecar: ENABLED`, then `WhatsApp sidecar is healthy.` and seven `PASS` lines from the smoke. A `::warning::` means the API is fine but the sidecar is not; send the lead the step log (it contains no secrets).

**D. Enable one tenant**

6. Grant WhatsApp to ONE staging tenant (platform admin; `<staging-api>` is your staging `APP_URL` + `/api/v1`, `$PLATFORM_TOKEN` a platform-admin access token you already use, `<tenant_id>` the test tenant):
   ```bash
   curl -sS -X PUT "<staging-api>/platform/tenants/<tenant_id>/capabilities" \
     -H "Authorization: Bearer $PLATFORM_TOKEN" -H "Content-Type: application/json" \
     -d '{"can_use_whatsapp": true, "max_whatsapp_numbers": 1}'
   ```
   The platform capability edit records an override, so plan syncs will not undo it.
7. Make sure that tenant has a knowledge base (a tenant without one is human-first and the AI stays silent).

**E. Push for the agent app**

8. Confirm on staging: `AGENT_ALERT_ENABLED` is `true` (code default is true) and `FIREBASE_CREDENTIALS_JSON` is the service account of the **same Firebase project** as the Android build you install. Both names exist on staging today; their values were not inspected.
9. Build the agent app (Flutter) with the **staging API base URL** and install the `.apk` on the Android phone. Log in as an agent of the test tenant, mark yourself available, allow notifications, and confirm the device registered (the app's device-registration call).

**F. Live test (with the lead)**

| # | Step | Expected | Capture |
|---|---|---|---|
| 0 | Before pairing: `ssh luran 'cd /opt/luran-staging && bash scripts/smoke_whatsapp_sidecar.sh luran_staging_whatsapp_sidecar'` | 7 x `PASS`, `sessions=0/2` | the output (no secrets) and `rss_mb` |
| 1 | Dashboard (staging) -> WhatsApp -> enter the test number, accept consent | 8-character pairing code, status `awaiting_pair` | screenshot (hide the code afterwards) |
| 2 | Enter the code on the phone (Linked devices -> Link with phone number) | `connecting` -> `connected` in the dashboard | smoke again: `sessions=1/2`, `rss_mb` |
| 3 | From a second WhatsApp account, message the test number | AI answers from the KB | the dashboard conversation |
| 4 | Ask for a human (an escalation trigger for that tenant) | Conversation escalates; **the agent phone rings** (FCM) within seconds; ticket created | screenshot of the ring, ticket id |
| 5 | In the agent app, take over and type a reply | The reply arrives on the second WhatsApp account | screenshot, time taken |
| 6 | Type on the owner phone itself | AI pauses (120 min), message shown as owner | dashboard |
| 7 | Unlink from the phone | `logged_out` within seconds, banner in dashboard | dashboard |
| 8 | `ssh luran 'docker stats --no-stream luran_staging_whatsapp_sidecar luran_staging_api'` at 0, 1 sessions | within limits | numbers, for the unmeasured-RAM follow-up |

What each step proves and what stays unproven is recorded in the SOP after the run. Not covered by this plan: media, more than 2 sessions, long-run stability, any production path.

---

## 8. Verified vs not verified (at the time of writing)

| Claim | Status |
|---|---|
| Host numbers, networks, limits, redis/db settings, env NAMES (api container), staging project/working dir | **Verified read-only on the real host** |
| Sidecar image builds; boots under `read_only`/`cap_drop ALL`/no swap limit; DB role init idempotent; least privilege holds; smoke script 7/7 PASS; signed webhook accepted/bad signature refused (against a stand-in endpoint that runs the repo's real `signing.verify` and `WebhookEvent` schema); orphan behaviour | **Verified locally in throwaway containers (REHEARSAL), not on the VPS** |
| `prepare_staging_whatsapp_env.sh` gate/split/floor on 5 fixture files | **Verified locally** |
| actionlint (no new findings vs baseline), `yaml.safe_load`, shellcheck (scripts and the embedded ssh script), `docker compose config`, production-job byte identity | **Verified locally** |
| The new CD steps executing on a GitHub runner against the VPS (scp of dotfiles, ssh heredoc, `docker load`, `run --rm` init, health loop) | **Not run: configuration only.** First proof is the first deploy after secrets exist. The skip path is the one that runs on merge |
| Real signed webhook into the real staging backend | **Not run** (needs the deployed backend with `WHATSAPP_ENABLED=true`) |
| FCM ring on staging, APK, agent reply reaching WhatsApp | **Not run** (live test) |
| Multi-session RAM | **Unmeasured** |
