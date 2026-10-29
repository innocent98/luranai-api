# Memory and capacity snapshot, 2026-10-28

**What this is.** A measured snapshot of container memory on the shared VPS (7.8 GiB, production + staging) taken from a `docker stats` paste by Adebayo at about 16:45 WAT, with what it does and does not tell us. It is a record and a to-do, not a fix. Nothing was changed.

## The numbers (MEM USAGE / LIMIT, as pasted)

| Container | Usage | Limit | % |
|---|---|---|---|
| `luran_api` (prod, 2 workers, long uptime) | **999.9 MiB** | 1.172 GiB | **83%** |
| `luran_worker` (prod) | 194.8 MiB | 1.953 GiB | 10% |
| `luran_db` / `luran_qdrant` / `luran_redis` / `luran_pgbouncer` (prod) | 64 / 164 / 6 / 3.7 MiB | 1.367 GiB / 1 GiB / 384 MiB / 128 MiB | 2-16% |
| `luran_staging_api` (1 worker, minutes after a deploy, new code) | 222.2 MiB | 768 MiB | 29% |
| `luran_staging_worker` | 329 MiB | 1 GiB | 32% |
| `luran_staging_whatsapp_sidecar` (1 paired session, idle) | 78.2 MiB | 384 MiB | 20% |
| staging db / qdrant / redis / pgbouncer | 42 / 32 / 6.6 / 1.7 MiB | 384 / 192 / 64 / 48 MiB | 3-16% |

Totals: about 1.4 GiB in use by production, about 0.7 GiB by staging, **about 2.1 GiB of 7.8 GiB** (page cache comes on top). The limits add up to about 9 GiB, more than the host, which is fine while real use stays near 2 GiB and is the reason the sidecar container carries a high OOM-kill priority (`docker-compose.whatsapp.yml`: the kernel kills it before anything else).

## Reading the prod API at 83%

- The two API containers are **not comparable**: staging has 1 worker and was started minutes before the reading; prod has 2 workers and has been up long enough to accumulate cache. Staging's number will climb; check it again tomorrow.
- Earlier cgroup measurement of the prod API (before the WhatsApp work, which is not in prod): anonymous memory about 367 MB, reclaimable kernel slab about 600 MB, `oom_kill` 0. The kernel gives reclaimable cache back under pressure, so 83% of the limit is a reading to understand, not an outage. Each gunicorn worker is about 220-235 MB from imports alone.
- What is **not** known: today's split of that 999.9 MiB, and why the slab cache grows. A new clue: the prod API shows **1.94 GB of block reads** since it started, against 360 kB on the staging API. Container uptime was not in the paste, so this may be image layers read at start-up across restarts; it still needs an answer.
- Nothing in this release touches that path: WhatsApp is not deployed to prod. The Runtime Logs endpoint (a suspected contributor) disappears from prod with the promotion in `docs/deployment/prod-release-plan-2026-10.md`.

**When to worry (any one of these):** `oom_kill` above 0 in the container's `memory.events`; `anon` above about 700 MB; the API container restarting; response latency climbing with swap or cache thrash on the host.

**Command to split the number (run on the VPS, prints no secrets):**

```bash
docker exec luran_api sh -c 'grep -E "^(anon|file|slab|slab_reclaimable|slab_unreclaimable|sock|kernel_stack) " /sys/fs/cgroup/memory.stat; cat /sys/fs/cgroup/memory.events'
```

## Measured split (Adebayo, ~16:57 WAT, cgroup `memory.stat`)

| | `luran_api` (prod) | `luran_staging_api` |
|---|---|---|
| `anon` (real application memory) | **378 MB** | 226 MB |
| `file` (page cache) | 29 MB | 4.6 MB |
| `slab_reclaimable` | **641 MB** | 5.7 MB |
| `slab_unreclaimable` | 1.2 MB | 0.7 MB |
| `memory.events` `max` / `oom` / `oom_kill` | 0 / 0 / 0 | 0 / 0 / 0 |

Reading: prod's 999.9 MiB is 378 MB of application plus 641 MB of **reclaimable kernel slab** (directory-entry and inode caches). The container has **never reached its limit** (`max 0`) and nothing was killed. Application memory is flat against the earlier reading (367 MB -> 378 MB). The slab grew roughly 40 MB since the earlier reading (about 600 MB). So: no leak in the app, nothing urgent. What is still unexplained is what builds millions of cached directory/inode entries in the prod API and not in staging (staging is also much younger).

Candidate for the 1.94 GB of block reads: the removed Runtime Logs endpoint read the whole log file line by line on each call (`_read_last_lines` in `app/api/v1/endpoints/platform/audit.py` on `main`). It would explain disk reads, but a single file does not explain 641 MB of slab, so treat it as a partial explanation only. The prod API also bind-mounts `./logs:/app/logs`.

Next measurements (read-only, on the VPS):

```bash
docker exec luran_api ls -lh /app/logs
sudo grep -E "^(dentry|inode_cache|ext4_inode_cache|proc_inode_cache|kernfs_node_cache|shmem_inode_cache|buffer_head) " /proc/slabinfo
cat /proc/sys/fs/dentry-state
```

and repeat the cgroup command in 24 hours; the difference in `slab_reclaimable` is the growth rate (baseline: 641,424,816 bytes at 2026-10-28 ~16:57 WAT).

## Root cause of the slab growth (found 2026-10-28 ~17:30 WAT, read-only, measured)

**The 30-second container healthcheck leaks about 22 KB of kernel directory-entry cache per run.** Nothing else is involved.

| Evidence | Result |
|---|---|
| `/app/logs` on the prod API | empty (`total 0`): the Runtime Logs / log-file theory is **refuted** |
| Host `dentry-state` (17:03) | 4,064,732 dentries, **3,938,827 negative** (97%); only ~96k ext4 inodes. These are names that were looked up and do not exist |
| Growth rate, prod API, 3 samples | ~3-4 dentries/s (about 200-250 B/s), flat regardless of traffic (load 0.06) |
| Same rate on the **idle staging API** | ~4-6 dentries/s: so it is the API image, not prod traffic. The worker container grows ~0.1/s |
| 0.5 s sampling on prod | growth arrives in **steps 31 s apart** (+23 KB each) = the healthcheck interval |
| Manual exec, 5 runs each | `sh -c true` 0 B · `python -c pass` 0 B · `import urllib.request` **~21 KB/run** · the exact healthcheck command **~22.7 KB/run** |
| A/B, 8 runs each, two rounds | default `python`: 170,560 B and 193,296 B · `python -B`: **0 B and 0 B** |
| Arithmetic | 22 KB x 2,880 runs/day = ~63 MB/day; the container is 10 days old: ~630 MB vs measured 641 MB |

**Mechanism.** The official `python:3.11-slim` image deletes every stdlib `.pyc`; the `__pycache__` directories are empty and root-owned, and the app runs as `appuser` (uid 1000). Each new Python process that imports a non-frozen stdlib module compiles it from source and tries to write the `.pyc` through a temp name that contains a per-process id; the write fails with `EACCES`, but the failed lookup leaves a **negative dentry with a unique name**. About 110 modules are imported by `urllib.request`, so ~110 new dentries per healthcheck, none ever reused. Gunicorn itself only does this at boot.

**Impact.** Low. The cache is reclaimable (`oom_kill` 0, `max` 0, `anon` flat at 378 MB), so no outage, but it makes the container look 83% full for no reason and would only be reclaimed under pressure. Side effect: every healthcheck also recompiles ~110 stdlib modules from source (wasted CPU and disk reads, a plausible part of the 1.94 GB of block reads; **not verified**, check `io.stat`).

**Proposed fix (not applied; changes the prod container, so it rides the next promotion):**

1. Healthcheck in `docker-compose.prod.yml:58` and `docker-compose.yml:36`: `python -B -c ...` (verified 0 B/run by the A/B above). One word, no image change.
2. Optional hardening in the `Dockerfile`, final stage, before `USER appuser`: `RUN python -m compileall -q /usr/local/lib/python3.11` so stdlib `.pyc` files exist (removes the recompile cost and the leak for any `docker exec python`), at the price of some image size. Needs a build to measure.
3. Verify after deploy: cgroup `slab_reclaimable` stays flat over 24 h; the existing 641 MB drains only under memory pressure or at the next container recreate (do **not** `drop_caches` on the shared host just to look better).

## What the sidecar can carry today

| Fact | Value | Source |
|---|---|---|
| Memory with one paired session, idle | 78 MiB (peaked about 111 MB RSS during the last deploy smoke) | paste + CD smoke line |
| Container limit | 384 MiB (`WHATSAPP_SIDECAR_MEM_LIMIT`) | `docker-compose.whatsapp.yml` |
| Refuse new sessions above | RSS 300 MB (`WHATSAPP_RSS_REFUSE_MB`) | compose / `config.mjs` |
| Session cap | 2 on staging (`WHATSAPP_MAX_SESSIONS`, `WHATSAPP_MAX_CONNECTIONS=2` on the API) | compose default / staging env |
| Image buffers | in-memory store up to 32 MB, one upload at a time, `/tmp` tmpfs 32 MB counts toward the limit | compose, contract |

**Honest answer on "how many businesses":** one number is one session. Staging is configured for **2 concurrent numbers** and that is the only figure that has been exercised (1 session live; 2 not yet). Beyond 2 there is **no measurement**: the soak tooling is now built (`docs/sop/whatsapp_sidecar_soak.md`, 2026-10-28) but **has not been run**. A reasoned (not measured) expectation is that 384 MiB holds several idle sessions, but each extra session's cost and the image-burst headroom (up to roughly 60 MB of buffers on top) are unknown, so no capacity number should be promised to anyone, and the production sidecar must not be sized by guesswork.

Other limits that matter before RAM: WhatsApp's own tolerance (replies are paced and capped per number by design), the API worker pool for LLM calls (1 gunicorn worker on staging, 2 on prod; calls are I/O bound), and the plan limit of 1/1/2/5 numbers per tenant.

## To-do (in order)

1. ~~Run the command on prod and staging~~ done 2026-10-28 (see root cause above). Re-run once after the healthcheck fix is deployed and confirm `slab_reclaimable` is flat.
2. Ship the `python -B` healthcheck fix (PR to `develop`, rides the prod promotion). Then check whether the 1.94 GB of block reads (`io.stat` `rbytes`) was the healthcheck recompiling stdlib; container uptime is 10 days, so ~190 MB/day.
3. Decide worker count and `--preload` (shared imports) with the measurement from item 1; keep 2 prod workers and the 1.172 GiB limit until then.
4. ~~Build the sidecar soak~~ tooling and runbook built 2026-10-28 (`docs/sop/whatsapp_sidecar_soak.md`). **Still to do: run it** (1, 2, 4, 6 sessions with message and image load, 24 h hold, restart storm; needs 6 test numbers), then size `WHATSAPP_SIDECAR_MEM_LIMIT` and `WHATSAPP_MAX_SESSIONS` for production at measured + about 50% (`soak-analyze.mjs` prints the recommendation and marks what is extrapolated). Required before the production sidecar PR. *Update 2026-10-29:* the production PR uses an interim sizing from the 21 h, 2-session staging measurement (see `whatsapp_sidecar_production_deploy.md`); the soak is still needed before raising `MAX_SESSIONS` above 3.
5. Keep an eye on the host total: prod + staging in use should stay well under 5 GiB.
