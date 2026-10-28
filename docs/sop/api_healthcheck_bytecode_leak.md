# API healthcheck negative-dentry leak (`python -B`)

**What shipped.** The API container healthcheck now runs `python -B -c ...` in `docker-compose.yml` and `docker-compose.prod.yml`. PR: see the PR for branch `fix/api-healthcheck-bytecode-leak`.

## Why

The prod API container showed 999.9 MiB of a 1.172 GiB limit, of which 641 MB was reclaimable kernel slab (directory-entry cache) and only 378 MB was application memory. Nothing was killed (`oom_kill` 0), but the number was unexplained and growing about 63 MB/day.

**Root cause.** The official `python:3.11-slim` image deletes all stdlib `.pyc` files, and the stdlib `__pycache__` directories are root-owned and empty. The app runs as `appuser` (uid 1000). Every healthcheck run (every 30 s) starts a new Python, imports about 110 stdlib modules through `urllib.request`, compiles them from source and tries to write a `.pyc` under a temp name that includes a per-process id. The write fails with `EACCES`, but the failed lookup leaves a negative dentry with a unique name that is never reused: about 22 KB per run, 2,880 runs/day.

## How

`-B` (do not write bytecode) skips the failed write, so no unique names are created. Chosen over the alternatives:

| Option | Verdict |
|---|---|
| `python -B` in the healthcheck | **Chosen.** One word, no image change, measured 0 B/run. |
| `PYTHONDONTWRITEBYTECODE=1` in the image | Also stops the leak for any `docker exec python`, but changes caching behaviour for the whole app. Not needed for this fix. |
| `python -m compileall` of the stdlib in the Dockerfile | Removes the leak and the per-run recompile, costs image size, needs a build to measure. Optional follow-up. |
| `drop_caches` on the host | Rejected: cosmetic, shared host (prod + staging). |

## What's involved

- `docker-compose.yml` (healthcheck `test`, api service) and `docker-compose.prod.yml` (same). The two files are the only Python-based healthchecks.
- The worker and every non-Python healthcheck are unaffected.

## Verification

Measured read-only on the VPS on 2026-10-28 (cgroup `memory.stat` `slab_reclaimable` of `luran_api`), full table in `memory_and_capacity_snapshot_2026-10-28.md`:

- growth arrives in steps 31 s apart (the healthcheck interval), about 23 KB each;
- exact healthcheck command: ~22.7 KB/run; `python -c pass`: 0 B; `sh -c true`: 0 B;
- A/B, 8 runs per round, two rounds: default `python` 170,560 B and 193,296 B; `python -B` 0 B and 0 B.

Local: both compose files pass `docker compose config -q`, and the parsed `healthcheck.test` starts `["CMD", "python", "-B", "-c"]`. No application code changed.

## Operate / roll back

- Takes effect when the API container is recreated (staging on the next `develop` deploy, prod on the next promotion). After deploy, confirm over 24 h that `slab_reclaimable` is flat:

  ```bash
  docker exec luran_api sh -c 'grep -E "^(anon|slab_reclaimable) " /sys/fs/cgroup/memory.stat'
  ```

- The existing ~640 MB drains only under memory pressure or when the container is recreated; recreation on deploy resets it.
- Roll back by removing `"-B"` from both files. Behaviour is otherwise identical.

## Follow-ups

- Check whether the 1.94 GB of block reads on the prod API (`io.stat` `rbytes`) was the healthcheck recompiling stdlib each run.
- Optional: `compileall` the stdlib in the Dockerfile before `USER appuser`.
- Remaining memory to-dos (worker count / `--preload`, sidecar soak) stay in `memory_and_capacity_snapshot_2026-10-28.md`.
