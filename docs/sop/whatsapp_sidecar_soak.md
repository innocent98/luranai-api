# SOP: WhatsApp sidecar N-session RAM soak (tooling + runbook), 2026-10-28

## What shipped
The tooling and the runbook for the measurement the production sidecar sizing is waiting on: **how much memory does the sidecar need at 1, 2, 4 and 6 paired numbers, under image bursts, over 24 h, and through a restart?** Branch `test/whatsapp-sidecar-soak` -> `develop` (PR link in the hand-back). **No soak has been run.** It needs N dedicated test numbers and a person holding the phones; this change only builds, unit-tests and documents the tooling. Every capacity number in this repo is still the single 1-session reading (78 MiB idle, about 111 MB RSS peak at the deploy smoke).

| Script (`whatsapp-sidecar/scripts/`) | Job |
|---|---|
| `soak.mjs` | Collector. `check` (one sample, writes nothing), `run` (CSV every N seconds), `mark <step>` (label what the phones are doing now) |
| `soak-analyze.mjs` | Reads the CSV, prints the report and the recommended limits (`--json` for machine output) |
| `soak-lib.mjs` | All the pure logic (CSV schema, probe parsing, statistics, analysis). Unit tested |
| `soak-make-images.mjs` | Writes valid PNGs of a chosen size (default 1, 4, 9.5 MiB) for the outbound image-burst |
| `soak-synthetic.mjs` | Dry run of the whole pipeline against the sidecar's fake socket. **NOT representative of real Baileys sessions** |

## Why
`docs/sop/memory_and_capacity_snapshot_2026-10-28.md` to-do 4: "Build the sidecar soak ... size `WHATSAPP_SIDECAR_MEM_LIMIT` and `WHATSAPP_MAX_SESSIONS` for production at measured + about 50%. Required before the production sidecar PR." The old `soak.mjs` only polled `/health` for `rss_mb`. That number is the Node process alone; the OOM killer judges the **container cgroup**, which also holds the 32m `/tmp` tmpfs, page cache and the short-lived `node` process of the image HEALTHCHECK. A limit sized from RSS alone would be too low by exactly the parts that kill the container.

## How (decisions)
| Decision | Why | Rejected |
|---|---|---|
| One `docker exec` per sample running **bash + cat only**, which reads the cgroup files and calls `/health` and `/sessions/<key>` over bash `/dev/tcp` | The probe runs inside the measured cgroup, so a second `node` process would add 40+ MiB to every reading. The container already holds the bearer token in its env, so the operator's shell, argv and the CSV never see it (the token is expanded by the `printf` builtin) | `docker exec ... node -e fetch` (inflates the reading); publishing a port or putting the token on the laptop |
| Per-session status (`GET /sessions/<key>`) in every sample, via `--keys` | `/health.sessions` counts **every non-`logged_out` session, including `connecting`, `reconnecting` and `failed`**. Restore time measured on it would stop the clock the moment the manager registers the rows, not when they are connected | Using `/health.sessions` alone (kept only as a flagged fallback) |
| Step marker via `<out>.step` file, re-read every sample; `mark` writes it | Works from a second terminal, survives a collector restart, no IPC. Unknown step names are refused so a typo cannot silently drop data | Stdin commands; a marker column edited by hand afterwards |
| Samples are only counted at a step if the live session count equals the step's N; `pairing` / `transition` steps and the first 25% of each step are dropped | A sample taken while a phone was still pairing is not "N sessions" | Trusting the human's label |
| Recommendation = **max container `memory.current` over the whole run x 1.5, rounded up to 64 MiB**; `MAX_SESSIONS` = the highest count actually soaked | "Measured + about 50%" as asked. Anything above the highest soaked N would be a guess, so it is only ever shown in the projection table | Recommending a count from the fit |
| If the kernel OOM-killed the container during the run (`oom_kill > 0` or `State.OOMKilled`), **no recommendation is produced** | The peak was capped by the limit; it is not the real need | Reporting the capped peak |
| Synthetic data gets a banner and **no** sizing | Fake sessions cost almost nothing; a number from them would be dangerously low | Printing it with a footnote |

## What's involved
- `whatsapp-sidecar/scripts/soak*.mjs` (5 files). Zero dependencies; `node:` built-ins only. `soak-synthetic.mjs` imports `src/` and `test/` fixtures, so it runs from a repo checkout only (the image copies `src/` alone).
- `whatsapp-sidecar/test/soak-lib.test.mjs`, `soak-collect.test.mjs`, `soak-synthetic.test.mjs` (27 tests, picked up by the existing `npm test`).
- Production code (`src/`) is **not touched**. Compose, CI and CD are not touched.
- Docs: this SOP, the checklist (T3 / T12 / production sidecar / memory section), `whatsapp_sidecar.md`, the memory snapshot to-do, `whatsapp-sidecar/README.md`.

CSV columns: `iso_time, elapsed_s, source, step, sessions, max_sessions, connected, statuses, rss_mb, rss_refuse_mb, cg_current_b, cg_max_b, cg_anon_b, cg_file_b, cg_shmem_b, cg_slab_reclaimable_b, cg_oom_kill, cg_limit_hits, started_at, oom_killed, restart_count, error`. `cg_shmem_b` is the `/tmp` tmpfs. A failed sample is a row with an `error` code (`docker_failed`, `health_401`, `cgroup_unreadable`, ...) and empty metrics, never raw stderr. The CSV holds no tokens, phone numbers or session keys.

## The run (Adebayo, on staging, with N dedicated test numbers)

**Rules.** Staging only. Dedicated test numbers only, never a customer number. WhatsApp can restrict a number that behaves like automation: use fresh numbers you can lose, and keep the message rates below modest (a few per minute per number). Needs: **6 test numbers** for the full 1/2/4/6 ladder, one more WhatsApp account to play the "customer" (send messages and photos), Node >= 22 and Docker on whatever machine runs the collector.

### 0. Lift the guards for the duration of the soak (reversible)
The staging defaults (2 sessions, `RSS_REFUSE_MB` 300, 384 MiB) would stop the experiment before it measures anything: at 300 MB RSS the sidecar **refuses new sessions and skips restores** (`failed` 503), and a too-small limit OOM-kills the container and caps the peak. Raise them on staging only:

```bash
ssh luran 'cd /opt/luran-staging && WHATSAPP_MAX_SESSIONS=8 WHATSAPP_RSS_REFUSE_MB=600 WHATSAPP_SIDECAR_MEM_LIMIT=768m docker compose --env-file .env --env-file .env.sidecar -f docker-compose.prod.yml -f docker-compose.whatsapp.yml up -d --no-deps whatsapp-sidecar'
```

(Command shape is the one in the header of `docker-compose.whatsapp.yml`; shell variables win over the env files. Not executed by the author of this SOP; run `docker inspect` afterwards, below, to confirm.) The next CD deploy of `develop` puts the defaults back.
The **backend** has its own cap and the plan has per-tenant limits (1/1/2/5 numbers): `WHATSAPP_MAX_CONNECTIONS` (`app/core/config.py:343`, 2 on staging) must be >= N, and one tenant cannot hold 6 numbers, so use several staging test tenants. That is an edit to `.env.staging.enc` (`scripts/env.sh decrypt|encrypt staging`, then a PR), so plan it ahead of the day; revert it after.
Check the sidecar took the new values (prints limits, no secrets):

```bash
ssh luran "docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' luran_staging_whatsapp_sidecar; docker exec luran_staging_whatsapp_sidecar sh -c 'echo MAX_SESSIONS=\$MAX_SESSIONS RSS_REFUSE_MB=\$RSS_REFUSE_MB'"
```

### 1. Set up the collector
Easiest is your laptop with the Docker CLI pointed at the VPS (no Node needed on the VPS). Add `ControlMaster auto`, `ControlPath ~/.ssh/cm-%r@%h:%p` and `ControlPersist 10m` for host `luran` in `~/.ssh/config` so each sample reuses one connection. Keep the laptop awake for 25+ hours (`caffeinate -i` before the command), or copy the `scripts/` folder to an always-on machine that has Node >= 22 and run it under `tmux`; if you run it on the VPS itself, drop the `DOCKER_HOST` line.

```bash
export DOCKER_HOST=ssh://luran
cd whatsapp-sidecar
C=luran_staging_whatsapp_sidecar
OUT=soak-staging-2026-MM-DD.csv       # keep it out of git
node scripts/soak.mjs check --container $C       # before anything is paired: expect sessions=0 and cg_* values, exit 0
```

The session keys are generated by the backend per connection (`secrets.token_urlsafe(24)`), so you only know them once a number is paired. The sidecar logs them with every status change (`"session":"<key>"`, derived from `src/session.mjs`; confirm on the first pairing):

```bash
docker logs $C 2>&1 | grep -o '"session":"[A-Za-z0-9_-]*"' | sort -u
```

Start the collector once, in its own terminal, and leave it. Restart it with the full key list as numbers are added (the CSV is appended; the step file persists):

```bash
node scripts/soak.mjs run --container $C --keys KEY1,KEY2,KEY3 --out $OUT --interval 30 --storm-interval 2
```

Samples taken before a key exists show it as `http_404`; that is expected and harmless (`connected` only counts connected sessions).

### 2. The steps (one `mark` per phase, in a second terminal)

| # | Do | `mark` | Hold for |
|---|---|---|---|
| 1 | Sidecar running, **zero** sessions | `baseline` | 10 min |
| 2 | Pair number 1 in the dashboard (Channels -> WhatsApp). Mark `pairing` first, so the minutes of QR/code entry are ignored. Restart the collector with the new key. When `check` shows `connected=1`, wait **10 min** to let it settle | then `sessions-1` | 15 min |
| 3 | Same for numbers 2 (-> `sessions-2`), then 3 and 4 (-> `sessions-4`), then 5 and 6 (-> `sessions-6`). If you stop at 4 or 5 sessions, label the last step with the real number | `sessions-N` | 15 min each |
| 4 | **Text load** at the highest N: from the customer phone send a short message to each test number about once a minute for 10 min, so the AI replies and the webhook/send path runs | `text-load` | 10 min |
| 5 | **Image burst** at the highest N (below) | `image-burst` | 3 rounds, 10 min |
| 6 | Leave everything alone, phones connected, no traffic | `hold` | **24 h** (the 1 h and 24 h marks are cut from this one step) |
| 7 | **Reconnect storm**: `mark storm`, then restart the sidecar and watch it come back | `storm` | until `connected` is back to N, plus 2 min |
| 8 | Back to quiet. Stop the collector with Ctrl-C | `transition` | |

Reconnect storm commands (step 7):

```bash
node scripts/soak.mjs mark storm --out $OUT
docker restart $C          # SIGTERM, 20 s grace; sessions restore from Postgres without re-pairing
node scripts/soak.mjs check --container $C --keys KEY1,KEY2,... # repeat until connected=N
node scripts/soak.mjs mark transition --out $OUT
```

The collector samples every 2 s while the step is `storm` and records the gap while the container is down as failed rows. Restore is staggered by design (2 s plus up to 1 s jitter per session), so 6 sessions take about 12-18 s of stagger plus Baileys connect time.

### 3. Triggering the image burst (what a human can and cannot do)
Limits to keep in mind: images over **10 MiB** are rejected, only jpeg/png/webp/gif are downloaded, the in-memory store holds at most 32 MiB (`MEDIA_STORE_MAX_MB`), at most two downloads/fetches run at once, one image upload at a time, and the `/tmp` tmpfs is 32m and **counts toward the container limit**.

- **Inbound (customer -> test number).** From the customer phone, send **photos, not "documents"** (a document is `kind:"other"` and is never downloaded). Send 4-5 photos in a row to each paired number at the same time (ask a second person, or send to all numbers within the same minute), then repeat 3 rounds, choosing the highest quality / "HD" option. **Be aware WhatsApp recompresses photos on the phone, so what arrives is typically well under 10 MB; you cannot make a phone deliver a 9 MB `imageMessage`.** (This is an assumption from how WhatsApp clients behave, not measured here.) This path therefore measures realistic bursts, not the cap. The cap path is covered by `test/media*.test.mjs`, which prove the memory is bounded.
- **Outbound (agent -> customer), the only way to push near-10 MB through the sidecar.** Generate files and send them as an agent reply with an image attachment from the agent app or dashboard:

  ```bash
  node scripts/soak-make-images.mjs ./soak-images 1 4 9.5      # valid PNGs, incompressible, 9.5 MiB < 10 MiB cap
  ```

  The backend uploads the image to the media host (Cloudinary) and asks the sidecar to fetch it from the allowlisted host and send it. **Unknown, check before the day:** whether the agent app/dashboard and the media host accept a 9.5 MiB attachment and deliver it unmodified (Cloudinary can re-encode on delivery). Whatever size actually reached the sidecar is what you measured; write it down next to the burst. Expect `peak tmpfs/shmem` in the report to reach about **2 x the image size** while an upload runs (Baileys writes an encrypted and an original copy): 9.5 MiB gives about 19 MiB of the 32 MiB tmpfs. Do one oversized file as well (`... 11`) once: it should be refused (`413 image_too_large`) and must not move the memory.
- Do bursts **at the highest N**. The report warns when they were taken at a lower count and, in that case, adds the burst delta onto the highest steady level (flagged `COMPOSITE`).

## Reading the result

```bash
node scripts/soak-analyze.mjs $OUT --current-limit-mib 384         # add --json for machine output; --expect N if the storm table cannot tell
```

| Section | Meaning | Trust |
|---|---|---|
| 1 Baseline | RSS and container memory with 0 sessions (idle process; the spike measured 95-101 MB) | **Measured** |
| 2 Steady levels | Per step: median / p95 / max after the first 25% is dropped. Samples whose session count did not match the step are dropped and counted in the warnings | **Measured** |
| 3 Marginal cost | Least-squares slope through the step medians (MiB per session), with R2. Below 0.9 the cost is not linear: do not extrapolate | **Fit** through measured points |
| 4 Bursts | Peak RSS, peak container memory and peak tmpfs per burst step, and the delta over the steady level at the same session count | **Measured** |
| 5 Hold | Start, 1 h mark, 24 h mark, end, slope in MiB/h with R2, verdict `GROWING` if growth > max(10 MiB, 5%) with a positive, well-fitting slope (**heuristic**). A mark the hold did not reach is printed `NOT REACHED` | **Measured**, verdict is a heuristic |
| 6 Storm | Seconds from the `mark storm` to all N sessions `connected`, and from container start; peak RSS and container memory after the restart. Resolution = the storm interval | **Measured** |
| 7 Safety counters | Peaks, the limit in force, `oom_kill`, limit hits, distinct container starts | **Measured** |
| 8 Recommendation | `WHATSAPP_SIDECAR_MEM_LIMIT` = peak x 1.5 rounded up to 64 MiB; `WHATSAPP_MAX_SESSIONS` = highest soaked N; `WHATSAPP_RSS_REFUSE_MB` derived at today's 300/384 ratio. Then a projection table: rows up to the highest soaked N are inside the measured range, rows above it are **EXTRAPOLATED** from the fitted slope, and one line says how many sessions today's 384 MiB would carry (also extrapolated beyond the soaked N) | Peak: **measured**. Multiplier: **policy**. Rows above N: **extrapolated** |

Decide only from a report with: `oom_kill 0`, no warning about the capacity guard or failed sessions, a hold of at least 24 h, a burst at the highest N, and a storm that restored every session. Each missing piece is printed as a warning. The report never says "safe"; it says what was observed.

Things to look at by hand: `cg_slab_reclaimable_b` over the hold (the API healthcheck leak in `memory_and_capacity_snapshot_2026-10-28.md` was found exactly this way; the sidecar's HEALTHCHECK is a Node process per 30 s, so watch for the same pattern), and whether `cg_current_b` peaks coincide with the 30 s HEALTHCHECK `node` process (they are real footprint and are left in on purpose; `rss_mb` is the sidecar process alone, so the container peak is higher).

## Verification
| Check | Result |
|---|---|
| Unit tests, synthetic CSV fixtures (`soak-lib`, `soak-collect`, `soak-synthetic`: 27 tests): step vocabulary, CSV round trip and injection of separators, probe parsing, error codes instead of raw text, linear fit, baseline / marginal cost / burst / hold marks / storm restore time / recommendation on a scenario with known answers, OOM invalidates, synthetic gets no sizing, capacity-guard and failed-session warnings, composite burst, weak storm, no-restart-seen, degenerate inputs | Pass |
| Probe script executed for real by local `bash` against a local HTTP stub (token only in the `Authorization` header, never in the output; 401 and dead-port paths exit 0) | Pass |
| Probe script executed inside `node:22.23.4-slim` under cgroup v2 with the staging flags (read-only rootfs, `/tmp` tmpfs 32m, 384m, no swap), `docker exec` + `bash -c`, against a stand-in server: real `memory.current/max/stat/events` values parsed, 3 samples via `run`, `check` and `mark` CLIs, stopped container -> `docker_failed` row, token never in output | Pass (stand-in server, **not the real sidecar**) |
| `soak-synthetic.mjs` end to end: real `SessionManager` + real HTTP API + fake sockets, every step incl. inbound images and a simulated restart through the real `restoreAll` | Pass; banner and "NONE: synthetic data" shown |
| CI parity for the `whatsapp-sidecar` job: `npm ci --ignore-scripts`; `npm test` with Postgres 15 + Redis 7 containers (same images as CI); `npm audit signatures` | 190/190 pass (163 existing + 27 new); 15 packages with verified attestations |
| **Not run** | Anything against real Baileys sessions or real WhatsApp servers; the real sidecar's `/health` and `/sessions/<key>` through `docker exec` (the stand-in matches the code in `src/api.mjs` / `src/manager.mjs`, not the running image); the log line used to discover session keys; whether the product accepts and delivers a 9.5 MiB image |

## Operate / roll back
- Nothing to deploy: the scripts are not in the image (`Dockerfile` copies `src/` only). Remove them by reverting the PR.
- After the soak: re-run the staging deploy (or the `up -d` above without the three variables) to restore 2 / 300 / 384m; revert the `WHATSAPP_MAX_CONNECTIONS` edit; remove the test connections in the dashboard; delete the CSV and `.step` file from wherever you ran it. The CSV contains no secrets, but it is yours to keep outside git.
- If the collector shows `docker_failed` for long stretches, the sidecar is down or the ssh connection dropped; the run is still valid, the gap is simply missing time (the hold marks tolerate +/- 5 minutes).

## Follow-ups
- **Run the soak** and attach the report to `memory_and_capacity_snapshot_2026-10-28.md`; only then write the production sidecar sizing (checklist: "Production sidecar job").
- Add the key discovery as a sidecar `GET /sessions` (list) if the log grep proves awkward; today `/health` cannot list keys.
- A second run after the Baileys version bump, since memory per session is a property of the library version.
- If the product does not accept 9.5 MiB attachments, record the real ceiling in `whatsapp_sidecar.md` "Images".
