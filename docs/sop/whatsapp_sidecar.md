# WhatsApp sidecar (multi-session linked-device service) — 2026-10-27

Branch `feature/whatsapp-sidecar` (PR to `develop`, not merged by this track). Code: `whatsapp-sidecar/`. Compose override: `docker-compose.whatsapp.yml`. CI job: `whatsapp-sidecar` in `.github/workflows/ci.yml`. Evidence it grew from: `docs/sop/whatsapp_pairing_spike.md` and `spikes/whatsapp/` (kept untouched except a README pointer). Build plan: `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md` section 8.

## What shipped
The keepable version of the spike: one Node 22 process that holds many WhatsApp linked-device sessions (one Baileys `7.0.0-rc14` socket per `session_key`), links a number through an 8-character pairing code, forwards inbound 1:1 text and owner activity to the Luran backend as signed webhooks, and sends replies back under strict ban-risk rules. It is a **service with an internal API**, not user-facing.

**Verified:** fake-socket unit suite + real Postgres/Redis integration suite (96 tests), image build, container boot/health/SIGTERM. **NOT verified:** anything against real WhatsApp servers through this multi-session code (no live pairing was done in this track), and multi-session RAM. See "Proven vs not proven".

## Why
The spike proved pairing works live but its code was single-session, file-based (7,661 files in 10 minutes), had no duplicate-process protection (a second process on the same credentials looks like an unlink: observed), and was gated to one test number. The first real slice (text only, one tenant granted by hand) needs a service that multiple tenants can share on an 8 GB VPS without surprising RAM growth.

## How (key decisions)
| Area | Decision | Rejected alternative |
|---|---|---|
| Auth store | Postgres schema `whatsapp_sidecar`, tables created idempotently at boot (advisory lock), **not** in the backend Alembic chain. AES-256-GCM, per-session key = HKDF-SHA256(`WHATSAPP_AUTH_MASTER_KEY`, `session_key`), GCM AAD binds `(session_key, kind, id)` so a copied row fails auth. Write-behind: key updates visible immediately, flushed in **one transaction per second** (or at 200 pending). Chat refs are stored as an HMAC, never raw JIDs | File per key (unworkable); a separate DB role (later hardening) |
| Lease | `wa:lease:{session_key}`, SET NX PX ttl 30 s, heartbeat ttl/3. One stable owner token per `Session` object, `acquire()` = renew-or-create for **that** token only, so a session can regain its own evicted/expired key but never another owner's. Heartbeat that cannot renew within `ttl/2` (measured from when the renew was sent) = lost. `verify()` runs before every reconnect; a 401 only counts as a real logout under `verify({strict:true})`, i.e. the key was still ours, not merely re-created after an eviction. Every wipe takes the lease first. All lease/auth/socket operations on a session run one at a time and carry a generation number, so a reconnect that was mid-flight when a logout arrived cannot start a socket afterwards. **Fail closed**: no Redis/lease means no socket | Trusting 401 alone; trusting a lease without re-verifying (Redis is `allkeys-lru` in prod and can evict) |
| Status mapping | 515 -> immediate reconnect (<=5 in a row). 401 + lease provably held -> wipe creds, `logged_out`. 401 without lease -> **no wipe**, retry later. 403 -> `failed`, never retried. 440 -> reconnecting; 3 consecutive -> `failed` (something outside the lease uses the creds), no wipe. Unregistered (pairing) socket that dies -> **not** retried; an expiry sweep turns it into `logged_out` code 408 after `PAIRING_TTL_MS`. Everything else -> `reconnecting` with 2 s..60 s jittered backoff | Auto re-requesting a code; wiping on 440 |
| Capacity | `MAX_SESSIONS` (default 3) counts every non-`logged_out` session; `RSS_REFUSE_MB` (default 400). Either trips `POST /sessions` (and re-pairing a `logged_out` session) to 503 `capacity_exceeded`. Slot is reserved synchronously so concurrent creates cannot overshoot | Counting only connected sessions |
| Boot restore | From the sidecar's own tables. Staggered one per 2 s + up to 1 s jitter, capped by `MAX_SESSIONS` and `RSS_REFUSE_MB`. Overflow sessions are reported `failed` code 503 (credentials kept) and are never silently started. `failed`/`logged_out` rows are not restored. Rows whose pairing never finished are left untouched at boot (another process may still own them) and expired by the sweeper, lease first, as `logged_out` 408 | Restoring everything at once (reconnect storm) |
| Sends | Only to a chat that messaged us within `REPLY_WINDOW_HOURS` (24 h) — persisted so a restart does not forget. Destination must be a `to_ref` we handed out. Read receipt first, then composing, then `1.5 s + rand*2.5 s + 25 ms/char (<=3 s)`, then paused, then the message; id is generated and registered **before** sending so its echo cannot look like the owner. Per-session token bucket (`SEND_PER_MIN`, 6), daily cap (`DAILY_SEND_CAP`, 200/UTC day, counted in Redis so a redeploy does not reset it), strictly serialised per session, 3 s hold after (re)connect | Per-chat-only serialisation |
| Owner detection | `fromMe` messages whose id we did not send via `/send` -> `message.outbound_external` (text, or `""` for owner media). Edits, deletes, reactions, group/status chatter, the "message yourself" chat, duplicates, and anything older than 15 min (history catch-up) are ignored | Treating any fromMe as owner |
| Webhooks | Per-session ordered queue, retries with exponential backoff (1 s .. 60 s, jittered, 8 attempts), same delivery `id` across retries, **fresh signature timestamp per attempt**, `redirect: error`, 10 s timeout. 400/413/422 are permanent. Never logs text, numbers, refs or pairing codes (pino redaction + masked ids only) | Fire-and-forget |
| Baileys config | `Browsers.macOS('Chrome')`, `printQRInTerminal:false`, `syncFullHistory:false`, `shouldSyncHistoryMessage:()=>false`, `markOnlineOnConnect:false`, `emitOwnEvents:false`, `shouldIgnoreJid` for groups/broadcast/newsletter, silent Baileys logger. Pairing code requested **only** on an explicit API call, once per fresh socket, only when unregistered | Reusing a socket for a second code (the live-proven path is fresh socket -> first `qr` -> `requestPairingCode`) |

## Contract as built (deviations from the lead's contract are marked **DEV**)
All calls: `Authorization: Bearer $WHATSAPP_SIDECAR_TOKEN` (SHA-256 both sides + `timingSafeEqual`), else `401 {"error":"unauthorized"}`. Error bodies are `{"error": "<code>", ...}`.

| Call | Success | Errors |
|---|---|---|
| `POST /sessions {session_key, phone_digits}` | `201 {status:"awaiting_pair", pairing_code, expires_in_s:180}` | 400 `bad_request{detail}`; 409 `session_exists`; **DEV** 409 `phone_in_use` (same number already active on another session); 503 `capacity_exceeded{max_sessions}`; **DEV** 503 `lease_unavailable` (Redis down), 409 `session_locked` (another process owns it), 504 `pairing_code_timeout` (30 s), 502 `pairing_failed`; on any failure the session is deleted so the call can be retried |
| `POST /sessions/{key}/pairing-code` | `200 {pairing_code, expires_in_s}` | 404; 409 `not_pairable{status}` unless `awaiting_pair`/`logged_out`; capacity as above; never auto-retried |
| `GET /sessions/{key}` | `200 {status, since_ms, last_disconnect:{code,at}\|null}` | 404. **`since_ms` is a duration** (ms since the status began), as in the spike, not an epoch |
| `DELETE /sessions/{key}` | `200 {status:"logged_out"}` | idempotent, also for unknown keys and already-logged-out sessions. **DEV** 409 `session_locked` / 503 `lease_unavailable` when the lease cannot be taken (another process owns the credentials, or Redis is down): credentials are never wiped blind. Remote logout when connected, then wipes credentials and the lease. The session row stays `logged_out` so a new code can be requested for the same key |
| `POST /sessions/{key}/send {to_ref, text}` | `200 {message_id}` | 400 (text empty or > 4096); 409 `reply_only`; 429 `rate_cap` (**DEV** daily cap adds `scope:"daily"`); 503 `not_connected`; **DEV** 502 `send_failed`, 503 `shutting_down`. `to_ref` must be a `from_ref`/`to_ref` previously emitted by this service |
| `GET /health` | `200 {sessions, max_sessions, rss_mb, rss_refuse_mb}` | `sessions` counts non-`logged_out` |

`last_disconnect.code` is a number: the Baileys code where there is one; synthetic **DEV**: `408` pairing expired, `440` lease lost / held by another process, `503` capacity/lease-unavailable/connect failure, `500` stored credentials cannot be decrypted (wrong master key).

Webhooks: `POST {BACKEND_BASE}/api/v1/webhooks/whatsapp/linked`, header `X-Luran-Signature: t=<unix>,v1=<hex>` = HMAC-SHA256(secret, `"<t>.<raw body>"`), body `{id, at, session_key, type, ...}`:
- `session.status {status, code?}` — emitted on **change** only. `awaiting_pair` is emitted when the code is issued; a pairing that ends unpaired emits `logged_out` code 408.
- `message.inbound {message_id, from_ref, from_phone_digits|null, push_name|null, kind:"text"|"other", text}`. `from_ref` is the opaque `remoteJid` (may be a LID): **reply with exactly this ref**; do not rebuild a JID from `from_phone_digits`. Media is `kind:"other", text:""` and never downloaded (captions are dropped too).
- `message.outbound_external {message_id, to_ref, text}`.
Receivers must dedupe on `id` (retries repeat it) and accept the current and previous secret.

## What's involved
| Path | Role |
|---|---|
| `whatsapp-sidecar/src/index.mjs` | wiring; SIGTERM: stop sends, flush, close sockets, release leases, drain webhooks (15 s hard stop) |
| `src/session.mjs` | per-session state machine, send path, owner detection |
| `src/manager.mjs` | capacity guard, create/delete, restore, sweeper |
| `src/api.mjs`, `src/config.mjs` | HTTP API; env validation (token/secret >= 32 chars, master key >= 32 bytes hex) |
| `src/auth-state.mjs`, `src/db.mjs`, `src/crypto.mjs` | encrypted batched store (`whatsapp_sidecar.sessions`, `auth_blobs`, `inbound_chats`) |
| `src/lease.mjs`, `src/limiter.mjs`, `src/status.mjs`, `src/messages.mjs`, `src/webhook.mjs`, `src/sign.mjs`, `src/wa.mjs`, `src/log.mjs` | lease, pacing, disconnect mapping, message classification, delivery, signing, the single Baileys construction point, log redaction |
| `whatsapp-sidecar/Dockerfile`, `.dockerignore`, `.env.example` | node:22.23.4-slim (Node 20 is end-of-life and unpatched for CVE-2026-111497, the Brotli decoder RCE; pin >= 22.23.4), `npm ci --ignore-scripts --omit=dev`, runs as uid 1000, HEALTHCHECK on `/health`; variable names only |
| `docker-compose.whatsapp.yml` | override: `mem_limit 512m` (`WHATSAPP_SIDECAR_MEM_LIMIT`), `restart: unless-stopped`, `stop_grace_period 20s`, **no `ports:`**, direct `db:5432` (not pgbouncer: transaction pooling breaks the advisory lock), Redis db `/1` |
| `.github/workflows/ci.yml` | new job `whatsapp-sidecar` (service Postgres + Redis; `npm ci --ignore-scripts`, `npm test`, `npm audit signatures`); other jobs untouched |
| Migrations / backend | none. `docker-compose.yml` and `docker-compose.prod.yml` untouched |

Dependencies (exact pins, lockfile committed): `@whiskeysockets/baileys 7.0.0-rc14`, `pino 9.14.0`, `pg 8.23.1`, `ioredis 6.0.0`. `npm audit`: 0 vulnerabilities; `npm audit signatures`: 89 verified signatures, 15 verified attestations. `ioredis` and `pg` are new relative to the spike (MIT, long-lived maintainers, published > 2 weeks ago). Re-vet provenance on every Baileys bump.

## Verification
- `npm test` in `whatsapp-sidecar/`: **96 tests**, 0 failures, with `TEST_DATABASE_URL`/`TEST_REDIS_URL` pointing at throwaway Postgres 15 + Redis 7 containers (`wa-sc-pg` :55462, `wa-sc-redis` :56462); without them the 6 integration tests skip (90 pass).
- Covered: signing (valid/tamper/stale/future/rotation/garbage + pinned vector from the spike's `sign.mjs`); encrypted round trip, no plaintext at rest, AAD binding, wrong master key, batching (500 key writes -> a handful of transactions on real Postgres), flush failure retry, wipe cannot be resurrected, abandon never flushes; lease acquire/loss/heartbeat/eviction/expiry/fail-closed on memory and on real Redis; capacity (count, RSS, concurrency, logged_out frees a slot); staggered/capped restore; reply-only (cold chat, window, persisted across restart, no raw refs in DB); send order (read -> composing -> delay -> paused -> send) and delay bounds; token bucket and daily cap; serialisation; owner detection (own echoes not emitted, edits/deletes/reactions/groups/self-chat/stale ignored); media as `kind:"other"`; status mapping for 401/515/428/403/440; two processes on one credential set; SIGTERM path; HTTP contract shapes and auth on every route; logs free of text, phone, pairing code and token.
- Process-level: booted `src/index.mjs` against the containers (no session created): `/health` shape, 401 without token, clean exit on SIGTERM, exit 2 on missing env; image built, runs as uid 1000, healthy, no published port, 60 MiB container memory idle (RSS 95-101 MB per `/health`).
- Opus security review of store, lease, webhook signing and API auth: see "Review outcome" below.

### Proven vs not proven
| Claim | Status |
|---|---|
| Logic of lease, store, status mapping, reply-only, pacing, signing, capacity, restore | Proven by tests with a fake socket and real Postgres/Redis |
| Pairing code, 515 handling, inbound/outbound, unlink detection through **this** code | **NOT proven live.** The fake socket encodes what the spike observed (first `qr` event then `requestPairingCode`; 515 after pairing; unlink as 401). The lead runs the live end-to-end with Adebayo holding the phone |
| `signalRepository.lidMapping.getPNForLID` / `remoteJidAlt` resolution | Unit-tested against the Baileys 7.0.0-rc14 type definitions only; **not seen live** |
| `messageId` option and `emitOwnEvents:false` behaviour | Per type definitions; not seen live |
| Phone-sent messages arriving as `messages.upsert` `notify`/`append` with `fromMe:true` | Assumed from Baileys behaviour; **not seen live** (this is what owner pause depends on: verify first in the live run) |
| RAM with N sessions | **NOT measured.** One session was 128-152 MB in the spike; idle process with zero sessions is 95-101 MB. `MAX_SESSIONS=3` / `RSS_REFUSE_MB=400` / `mem_limit 512m` are guards, not a sizing |
| Long-run stability (hours), network drop and reconnect, WhatsApp restriction behaviour | Not tested |

## Operate / roll back
- Start: `docker compose -f docker-compose.prod.yml -f docker-compose.whatsapp.yml up -d --build whatsapp-sidecar` with `WHATSAPP_SIDECAR_TOKEN`, `WHATSAPP_WEBHOOK_SECRET`, `WHATSAPP_AUTH_MASTER_KEY` set (names in `whatsapp-sidecar/.env.example`; generate with `openssl rand -hex 32`). The backend must use the same token and webhook secret.
- **Back up `WHATSAPP_AUTH_MASTER_KEY`.** Losing or changing it makes every stored credential undecryptable: sessions come back `failed` (500) and every number must be re-paired. Webhook secret rotation: set the new value on both sides, keep the old as `WHATSAPP_WEBHOOK_SECRET_PREVIOUS` on the verifying side until the sidecar has restarted.
- **One replica only.** Two replicas are survivable (the second sits in `reconnecting` code 440 until the first stops) but pointless. Container restarts: SIGTERM flushes credentials and releases leases; after a hard kill the lease expires within 30 s, during which the new process shows `reconnecting` 440 and then recovers by itself.
- Redis: the prod Redis is `allkeys-lru` 256 MB and shared with the arq queues. Lease keys can be evicted; the owner re-creates its key on the next heartbeat, so eviction does not drop sessions, but sustained memory pressure makes leases and the daily counter flaky. Prefer a dedicated Redis (or at least a `volatile-ttl` instance) before beta.
- Roll back: `docker compose ... stop whatsapp-sidecar`. Credentials stay in `whatsapp_sidecar.*` and restore on the next start without re-pairing. To remove all data: `DROP SCHEMA whatsapp_sidecar CASCADE` (every linked number must then re-pair and should also be removed from the phone's linked devices).
- Kill switch for one tenant: `DELETE /sessions/{key}`. If the session is not connected (e.g. `reconnecting`) the remote logout cannot be sent, so the device stays linked on the phone until the owner removes it; tell the tenant.

## Review outcome
One Opus review (read-only) of credential store, lease, webhook signing and API auth. It found no defect in: AES-GCM use (nonce, AAD, key derivation), SQL parameterisation, signature verification and signed-string/sent-string equality, bearer compare and body limits, reply-only enforcement, rate/daily caps, logging hygiene, Dockerfile/compose exposure. Defects found and **fixed in this PR** (each has a regression test):

| # | Severity | Defect | Fix |
|---|---|---|---|
| 1 | High | A logout (or re-pair) arriving while a reconnect was suspended on Redis/Postgres could still start a socket afterwards (old creds reloaded), and overlapping connects could orphan a heartbeating lease | Per-session serial operation queue + generation counter re-checked after every await; lease assigned the moment it is acquired; a stale `Lease` object is ignored by `_onLeaseLost` |
| 2 | High | Credentials were wiped without holding the lease (boot restore of unfinished pairings, `startPairing`, `logout`) | `_ensureLease()` before any wipe; boot restore no longer wipes (sweeper expires lease-first); `DELETE` returns 409/503 instead of wiping blind |
| 3 | Medium | Heartbeat could declare loss only near key expiry | Loss at `ttl/2`, deadline measured from send time |
| 4 | Medium | Evicted-and-recreated key accepted as proof of ownership for a 401 wipe | `verify({strict:true})` for the wipe decision; a vanished key re-created after `ttl/2` counts as lost |
| 6 | Low | N concurrent `pairing-code` calls on `logged_out` sessions bypassed `MAX_SESSIONS`; | A pairing session counts as active; slot taken synchronously |
| 7 | Low | `create` accepted non-string `session_key`/`phone_digits` | `typeof` checks |
| 8 | Low | `close()` bypassed the write queue; `saveCreds` could wait the full 1 s; `_park` flushed without proving the lease | `close()` flushes via the queue then refuses writes; earlier timers win; `_park` flushes only if the lease verifies, else drops |
| - | Hardening | Baileys/pg/undici error messages can embed JIDs | Every logged error goes through `errName()` (class + code only) in session, manager, api, auth-state and index |

**Accepted / open (documented, not fixed here):**
- *(5, Medium)* The sidecar uses the main Postgres user and the shared Redis. A compromised Baileys parse path could reach the backend database; a compromised backend could rewrite `inbound_chats` or delete `wa:daily:*` keys to reopen the reply window / reset the daily cap. **Before beta:** dedicated Postgres role scoped to schema `whatsapp_sidecar`, and a dedicated Redis (also see lease eviction below).
- *(4, residual)* No fencing token on Postgres writes: with an `allkeys-lru` Redis, an evicted lease could in theory let a duplicate process connect for up to one heartbeat before the first stops. Mitigation today: strict verification, `ttl/2` loss, single replica. Proper fix: a `noeviction` Redis for leases and/or a lease epoch column checked in `putBlobs`/`wipeAuth`.
- Webhook events are queued in memory only (lost if the process dies while the backend is unreachable); cap 1000 events per session.
- `logged_out` sessions stay in memory and in the table until purged.

## Follow-ups
- Live end-to-end through this service (lead + Adebayo): pair, inbound, owner-typed message, reply, restart restore, unlink.
- N-session RAM soak and 24 h stability (`scripts/soak.mjs` is a skeleton).
- Media in/out (text only now); `from_phone_digits` fallback if lid mapping is empty at first contact.
- Dedicated sidecar DB role, a dedicated `noeviction` Redis, lease fencing epoch in Postgres; purge of long-dormant `logged_out` rows; persist more reply-only context (`push_name` etc.) only if the backend needs it.
- Persisted webhook outbox (events queued in memory are lost if the process dies while the backend is down for minutes).
- `inbound_chats` pruning (rows only grow with distinct customers).
