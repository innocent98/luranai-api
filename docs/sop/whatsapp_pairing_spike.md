# WhatsApp pairing-code spike (option A) — 2026-10-27

Branch `feature/whatsapp-quick-connect` (PR to `develop`). Spike code: `spikes/whatsapp/` (isolated; not in `app/`, compose or CI). Build plan: `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md`. Option B memo: `docs/whatsapp-option-b-decision-memo.md`.

## What shipped
A time-boxed spike, not a feature. It proves, on a real dedicated test number, the tenant-style flow *enter number -> pairing code -> linked -> inbound text/image reaches a signed webhook -> reply arrives on the phone*, and measures the sidecar. **Pairing code works.** Stack that worked: a thin Node service on `@whiskeysockets/baileys` `7.0.0-rc14`.

## Why
Competitor D-Zero links a WhatsApp number in minutes. Luran has no WhatsApp code. Before committing a build plan we needed to know whether the 8-character pairing-code flow is real, how heavy a session is, and how it fails.

## Verdict by stack
| Stack | Pairing code? | Evidence | Status |
|---|---|---|---|
| **Baileys direct** (`requestPairingCode`) | **Yes** | Run live end to end (below) | **PROVEN LIVE** |
| **Evolution API** v2.3.7 (stable, 2025-12-05; 2.4.0-rc2 2026-05-17) | Yes in source | `whatsapp.baileys.service.ts` calls `client.requestPairingCode(this.phoneNumber)` inside the QR handler when an instance is created/connected with a `number`; `instance.controller.ts` passes `number` into `connectToWhatsapp`. Response carries `pairingCode`. Earlier "QR only" research was wrong or stale | **SOURCE-VERIFIED, not run** (needs Postgres+Redis; no second pairing was run). Pins `baileys` `7.0.0-rc.9`, behind upstream rc14. License Apache-2.0 "with additional brand-protection conditions" (GitHub shows NOASSERTION): read before embedding |
| **WAHA** | Yes in docs | Official docs: `POST /api/{session}/auth/request-code`, listed for WEBJS, WPP, NOWEB, GOWS. Docs warn pairing code is unreliable with custom device names. Docs state WAHA Plus features (incl. unlimited sessions) merged into Core from 2026.6.1; commercial/multi-tenant licence terms not found | **DOC-VERIFIED, not run** |

**Recommendation: build our own small Baileys sidecar** rather than embed Evolution/WAHA. Reasons: pairing is proven on the exact path we need; we need multi-tenant isolation, Postgres-backed encrypted auth storage and the Luran-signed webhook contract, all of which we would have to bolt onto Evolution/WAHA anyway; Evolution drags its own Postgres/Redis schema and lags Baileys; licences are murkier. Evolution/WAHA stay as a fallback if our sidecar slips.

## Supply-chain vetting (done before any install)
- `@whiskeysockets/baileys` `7.0.0-rc14`: published 2026-07-29, MIT, repo `WhiskeySockets/Baileys` (11.4k stars, pushed daily), npm **SLSA provenance attestation present**. The unscoped `baileys` package has identical maintainers/repo. `lotusbail` on npm is now a `0.0.1-security` placeholder (the reported poisoned fork); not used.
- Notable new transitive deps in rc14: `whatsapp-rust-bridge@0.5.4` (single maintainer `jlucaso`, WASM/Rust, no install scripts, no provenance attestation), `libsignal@6.0.0` (WhiskeySockets, provenance present). Install used `npm install --ignore-scripts --save-exact`; lockfile committed; `npm audit`: 0 vulnerabilities; 54 packages. **Production must pin the lockfile, run `npm ci --ignore-scripts`, and re-vet on every Baileys bump.**

## Live evidence (dedicated test number, one operator holding the phone)
Times are UTC on 2026-10-27. Nothing below contains credentials; auth state, media and env stayed outside the repo and were deleted after the run. Webhook events: `spikes/whatsapp/captures/events.sanitized.jsonl`.

| Step | Result |
|---|---|
| Request pairing code | Returned **23 ms** after the first `qr` event (socket ready), code issued 16:51:35. Config: valid browser tuple `Browsers.macOS('Chrome')`, no QR printing, requested once and only when creds not registered |
| Enter code on phone | Operator completed Link with phone number ~55 s later. Baileys closed with **515 "restart required"** (normal post-pair restart); sidecar reconnected itself and reached `connected` ~5 s later. **No 428, no "code but no prompt" failure seen** |
| Inbound image + caption | Arrived as one image message with caption; **74,825 bytes**, `sha256` identical in the webhook and in the bytes served back by the sidecar (`/media/:id`); HMAC verified |
| Inbound text ("Ping") | Delivered to the webhook, HMAC verified |
| Reply | Receiver auto-replied via sidecar; send returned 200; delay inbound->send **4.9 s** (composing presence + length-scaled jitter). **Operator confirmed the echo arrived on the phone** |
| Webhook signing | All status and message events HMAC-SHA256 (`t=<ts>,v1=<mac>` over `ts.body`) verified by the receiver; unit tests cover tamper, wrong secret, stale timestamp, garbage header |
| Restart survival | 4 restarts of the sidecar process: **3.8 s, 3.0 s, 3.0 s, 3.0 s** from process start to `connected`, no re-pairing, restored from AES-256-GCM encrypted creds |
| Unlink from phone | Operator chose Log out on the device: socket closed with **401 ("conflict")**; sidecar flipped to `logged_out` within ~0.3 s, emitted a signed `session.status` webhook and **wiped the dead creds** (0 files left) |
| Consent gate | Sender had to be explicitly allowed before any reply; first attempts correctly returned 403 |

## Measurements
| Metric | Value | Caveat |
|---|---|---|
| RSS, 1 session connected, settled | **~128-152 MB** (OS RSS 131.7 MB at 6 min; 147.8-152.7 MB after restores) | One session only. Node baseline is part of this. **Per-additional-session marginal RAM was NOT measured**, so do not extrapolate. First production task includes an N-session soak (2/5/10 sessions in one process) |
| RSS awaiting pair (unregistered) | 136.5 MB | |
| Heap | 24-38 MB | |
| Reconnect from process restart | 3.0-3.8 s | Local machine, warm DNS. Staged production restore must stagger |
| Auth store size | **7,661 files / ~30 MB after ~10 min of one session** (pre-keys, `lid-mapping`, tctoken, sessions) | A file-per-key store is unworkable: production needs a DB table with batched writes |
| Unlink detection | ~0.3 s | |
| Run length | ~14 minutes of linked time | **No long-run stability claim can be made** |

Sizing against `docker-compose.prod.yml` (api 900m, worker 700m, db 1200m, qdrant 900m, redis 256m, pgbouncer 128m; ~4.1 GB of limits, VPS size not known to us): a sidecar limit of 512m is plausible for the 2-day demo (1-3 sessions). Beta capacity is **not sized** until the soak runs; the VPS total RAM must be confirmed first.

## Failure modes: seen, and not seen
Seen:
- **515 after pairing** is normal; restart logic must handle it (done in spike).
- **Unlink looks like a 401 "Stream Errored (conflict)"**, not a clean "logged out" message. The same conflict signature can come from a second process using the same creds, so the production sidecar needs a **single-writer lease per connection** (Redis lock) or a duplicate deploy will log the tenant out.
- **File-key explosion** (above).
- **Sender identity is not a phone number.** The inbound sender appeared as an opaque id whose digits did not match the sender's number (the operator themselves did not recognise it). Consistent with WhatsApp's LID privacy ids; raw JID suffix was not logged, so UNVERIFIED. Design consequence: a conversation key cannot assume E.164; resolve LID<->phone through Baileys' lid-mapping and store both when known.
- **Consent gate worked as designed.** The first two inbound messages got a 403 on the auto-reply because the sender had not been allowed; the operator initially did not recognise the sender id (see above) and the gate held until the sender was confirmed as theirs. Production equivalent: reply only to chats that wrote first; never message an unconfirmed id.

Not seen / NOT tested (do not assume safe): HTTP-level 428 "Connection Closed"; code-but-no-prompt; multi-hour stability; network drop and reconnect backoff; voice notes, video, documents, stickers, large media; group chats (ignored in spike); read receipts/delivery status; WhatsApp-side restriction or ban behaviour (cannot be tested without risking the number); multiple sessions in one process; number with 2FA/Business app; Evolution/WAHA runtime.

## Ban-risk mitigations the design MUST include
Option A violates WhatsApp's terms for automated use of an unofficial client; numbers can be restricted or banned at any time. Mitigations (also in the plan):
1. **Reply-only**: send only into chats that messaged us first, within an open conversation; no cold outreach, no broadcasts, no bulk or scheduled sends. Enforced in the adapter, not the UI.
2. **Human-like pacing**: composing presence, delay scaled by message length with jitter (spike: 1.5-4 s + 25 ms/char), per-connection and per-recipient rate caps (spike: 6/min), a daily cap, serialised sends per chat, no parallel bursts after reconnect.
3. **Dedicated number recommendation** in the connect screen: not the owner's personal number, not a number with a long customer history; warm-up guidance for new numbers.
4. **Explicit risk consent** at connect time (version-stamped text, who/when stored): beta, unofficial, possible suspension, Luran not liable for the number, option to move to the official API.
5. **No history sync, no contact scraping, no read of other chats**: `syncFullHistory=false`, groups and status ignored, only 1:1 inbound processed.
6. **Kill switch and detection**: per-tenant and global flag; automatic pause when the session reports logged-out/conflict/restricted; alert the tenant; never auto-retry pairing.
7. **Opt-out handling**: STOP/unsubscribe keyword stops AI replies for that chat.
8. Staged rollout: allowlist of tenants, flagged beta, a clear upgrade path to option B.

## Operate / roll back
Spike only: stop the two Node processes; delete the scratch auth dir. Nothing was deployed and nothing touches production. Re-run steps are in `spikes/whatsapp/README.md`. The test number is now unlinked.

## Follow-ups
- Run the multi-session RAM soak and a 24 h stability run before sizing the beta.
- Decide the LID<->phone identity model before the schema is final.
- Run Evolution or WAHA once for the fallback record (needs a re-pair).
- Re-check pairing against any newer Baileys release before pinning for production.
