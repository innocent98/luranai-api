# Luran backend — master build checklist

Legend: `[x]` done and verified, `[ ]` not done. Status per module: DONE / IN PROGRESS / PLANNED.
**Scope note:** this file was created on 2026-10-27 by the WhatsApp spike. It currently maps only the WhatsApp module. Earlier modules (chat, flows, voice, tickets, billing, etc.) are tracked in `docs/sop/` and `docs/superpowers/plans/` and are **not yet reconciled here**; do that in a dedicated pass rather than guessing status.

Snapshot (WhatsApp module): spike DONE; build plan DONE; build PLANNED (0 of 13 tasks).

## WhatsApp channel (quick connect) — IN PROGRESS (spike + plan merged-ready, build not started)
Docs: spike `docs/sop/whatsapp_pairing_spike.md`; plan `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md`; option B memo `docs/whatsapp-option-b-decision-memo.md`.

### Spike and research (2026-10-27)
- [x] Pairing code proven live with Baileys 7.0.0-rc14 on a dedicated test number (code in 23 ms, linked, restart-survival 3.0-3.8 s, unlink detected ~0.3 s)
- [x] Signed (HMAC) webhook, inbound text + image bytes, reply delivered to phone, encrypted-at-rest creds
- [x] Evolution API and WAHA checked: pairing code present in source/docs (not run live)
- [x] Supply-chain vetting of Baileys and new deps
- [x] RAM per session measured for ONE session (~128-152 MB)
- [ ] RAM for N sessions in one process, 24 h stability (plan task T12)
- [x] Option B research, official NGN rate card, cost scenarios (memo)

### Build tasks (plan section 11)
- [ ] T1 Channel abstraction
- [ ] T2 Migration + models + enums + capability columns
- [ ] T3 Sidecar (multi-session, PG encrypted store, lease, restore, signed events, compose, CI job) — **built, PR open `feature/whatsapp-sidecar`; fake-socket + real Postgres/Redis tests green; NOT yet proven live** (SOP `docs/sop/whatsapp_sidecar.md`)
  - [x] Multi-session manager, one Baileys socket per `session_key`, exact pin 7.0.0-rc14, `npm ci --ignore-scripts`
  - [x] AES-256-GCM auth store in Postgres (`whatsapp_sidecar` schema), per-session HKDF key, AAD-bound rows, batched write-behind, wipe on logout
  - [x] Redis single-writer lease (token, heartbeat, fail closed, no wipe without a confirmed 401 under lease)
  - [x] Staggered, capped boot restore; graceful SIGTERM (flush, close, release)
  - [x] Reply-only sends (24 h window, persisted), read receipt + composing + jittered delay, token bucket + daily cap, serialised
  - [x] Owner detection: `message.outbound_external` for fromMe messages not sent via `/send`
  - [x] Signed webhooks (ordered, retried, idempotent delivery id), no text/phone/code in logs
  - [x] Capacity guard (`MAX_SESSIONS`, `RSS_REFUSE_MB`) + `GET /health`
  - [x] Dockerfile (node 20 slim, non-root), `docker-compose.whatsapp.yml` (512m, no published port), CI job `whatsapp-sidecar`
  - [x] Opus security review of store, lease, webhook signing, API auth
  - [ ] Live pairing through the new multi-session code (lead arranges with Adebayo holding the phone)
  - [ ] N-session RAM soak (`whatsapp-sidecar/scripts/soak.mjs` is a skeleton)
- [ ] T4 Connection endpoints + consent + gating
- [ ] T5 Inbound webhook + dedupe + arq bridge + session rotation
- [ ] T6 Structured-message text renderer
- [ ] T7 Outbound sender + pacing + `fanout_event` hook
- [ ] T8 Media in/out
- [ ] T9 Status machine, disconnect alerts, kill switch, first-responder override
- [ ] T10 Dashboard Channels -> WhatsApp (luran-dashboard repo)
- [ ] T11 FE integration guide (live captures), SOP, checklist update
- [ ] T12 Soak and deploy-restore drill
- [ ] T13 Opus security review (webhook signature, credential storage, tenant isolation)
- [ ] T14 Option B Cloud API adapter (blocked on Meta Tech Provider approval)

### Option B (Meta) track — owner actions
- [ ] Meta Business portfolio + Business Verification (Adebayo)
- [ ] Meta app (Business type, WhatsApp use case) + App Review videos + privacy/ToS URLs
- [ ] Separate +234 test number and USD card; Coexistence test on +234
- [ ] Legal read on Terms 4.1(c) and 4.7

## Backlog / upcoming
- [ ] Reconcile all earlier modules into this checklist

## Deferred follow-ups (WhatsApp)
- [ ] Sidecar: purge long-dormant `logged_out` session rows; persist the daily send counter beyond Redis; second Baileys-version re-vet on every bump
- [ ] Sidecar: images/media in and out (text only in this slice)
- [ ] Run Evolution or WAHA once live for the fallback record
- [ ] Decide LID <-> phone identity model before schema freeze
- [ ] Tier-driven `max_whatsapp_numbers` on `subscription_plans` (phase 2 after beta)
