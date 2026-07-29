# SOP — Auditor worker memory limits + stalled-scan reaper

**Status:** Implemented on branch `fix/auditor-worker-memory-and-reaper` (off `develop`) · 2026-07-29
**Area:** infra (`.env.production`/`.env.staging` memory profile, host swap) + `app/auditor` (crawler, service), `app/core/config.py`, `app/workers/document_processor.py`

---

## What shipped

1. **Staging worker memory fixed** — `WORKER_MEM_LIMIT` 300m → **1024m** + `AUDITOR_MAX_CONCURRENT_CRAWLS=1`.
2. **Production memory profile raised for breathing room + 3 concurrent crawls** — `WORKER_MEM_LIMIT` 1200m → **2000m**, `AUDITOR_MAX_CONCURRENT_CRAWLS=3`, `API_MEM_LIMIT` 1300→1500m, `DB_MEM_LIMIT` 1200→1400m, `QDRANT_MEM_LIMIT` 900→1024m, `REDIS_MEM_LIMIT`/`MAXMEMORY` 256m/192mb → 384m/256mb, Postgres `shared_buffers` 256→384MB, `effective_cache_size` 512MB→1GB.
3. **8 GB host swapfile** added to the VPS (safety net; RAM+swap = 16 GB addressable, `vm.swappiness=10`).
4. **`--disable-dev-shm-usage`** on the Chromium launch (both stacks have the Docker-default 64 MiB `/dev/shm`).
5. **Stalled-scan reaper** — `reap_stalled_audits_sync` + a 10-min ARQ cron that fails audits stranded in QUEUED/CRAWLING/ANALYZING past `AUDITOR_STALL_REAP_MINUTES` (45).

Commits: `47d50ea` (encrypted env profile) · `949c1ee` (shm flag + reaper + test).

## Why (root cause)

Every scan on **staging** returned `unreachable` / 0 pages (even bot-protection-free sites like `v-studio.org`), while the **same** scans succeeded on localhost (v-studio → 97, 46 pages). Diagnosis via a worker-side repro:

- The **staging worker was capped at 300 MiB** — *less than a single headless Chromium* (this codebase measures one at ~394 MiB; a crawl needs ~554 MiB). The cgroup **SIGKILLed Chromium the instant it launched** (`exit=137`), so every render produced 0 content → classified `unreachable`. Confirmed: `docker exec luran_staging_worker` Chromium launch → `exit=137`; after `docker update --memory 1024m` → `OK example.com exit=0`.
- A separate symptom — an audit stuck in `crawling` for 23 h — was the same cause on the write path: a worker **killed mid-crawl never runs `run_audit`'s `except`**, so the report is never marked FAILED. There was **no reaper**, so it sat forever.
- Prod (1200 MiB) rendered fine throughout; the failing dataset was actually from `staging-api.luranai.com`.

The host has 8 GB RAM shared by **both** full stacks (12 containers); prod+staging caps already summed near 8 GB with **no swap**, so raising prod caps required adding swap first.

## How it works

- **Memory profile** lives in `.env.{production,staging}` (encrypted → `.enc` via `scripts/env.sh`; CD decrypts on deploy). `mem_limit` is a cgroup **kill threshold**, not a reservation.
- **`AUDITOR_MAX_CONCURRENT_CRAWLS`** is the browser semaphore (`crawler._CRAWL_SLOTS`); sized to `WORKER_MEM_LIMIT` (≈350 base + 394×N MiB).
- **Reaper**: `app/auditor/service.py::reap_stalled_audits_sync(db, older_than_minutes=None)` fails non-terminal reports older than `settings.AUDITOR_STALL_REAP_MINUTES`. Registered as `reap_stalled_audits` in `WorkerSettings.functions` + a `cron(minute={0,10,20,30,40,50}, run_at_startup=True)`. The window is above any real crawl but below ARQ's 1h `job_timeout`; if a slow job later commits a terminal state it simply overwrites the FAILED, so early reaping is harmless.

## What's involved (files)

`.env.production(.enc)`, `.env.staging(.enc)`, `app/auditor/crawler.py`, `app/auditor/service.py`, `app/core/config.py`, `app/workers/document_processor.py`, `tests/auditor/test_service_reaper.py`. Host: `/swapfile` + `/etc/fstab` + `vm.swappiness=10`.

## Verification

- Live: staging worker `docker update --memory 1024m` → Chromium `OK exit=0`; 8 GB swap active (`swapon --show`).
- Tests: `reap_stalled_audits_sync` — reaps old CRAWLING/ANALYZING/QUEUED, leaves fresh CRAWLING and old COMPLETED untouched (4 tests). Full `tests/auditor/test_crawler.py` still 45 passing with the shm flag. `ruff check app/` clean. Reaper registered in `WorkerSettings.functions` (import-checked).

## Operate / roll back

- **Deploy to apply persistently:** merge to `develop` (→ staging) and `main`/prod branch (→ production); CD decrypts the new `.enc` and `docker compose up -d` recreates containers with the new limits. The live `docker update` on staging is temporary until the container is recreated.
- **Swap** is host-level and already persisted in `/etc/fstab`.
- Roll back = revert the branch + re-`docker update` limits if needed; swap can be `swapoff`'d.

## Follow-ups

- **8 GB is tight for both stacks at these caps** (≈9.4 GB summed, absorbed by swap). If prod audit volume grows or you need 4+ concurrent crawls, move to a **12–16 GB VPS** — RAM, not swap, is what makes concurrent crawls fast.
- **Rotate the exposed secrets** (live Paystack `sk_live_` key + the other credential surfaced during this session), then re-encrypt `.env.production` and redeploy. Consider a secrets manager so plaintext never lands on disk.
- **Dashboard not showing the quality score** — separate frontend bug in `luran-dashboard` (the API returns `overall_score`); still open.
