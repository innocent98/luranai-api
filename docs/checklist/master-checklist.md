# Luran backend — master build checklist

Legend: `[x]` done and verified, `[ ]` not done. Status per module: DONE / IN PROGRESS / PLANNED.
**Scope note:** this file was created on 2026-10-27 by the WhatsApp spike. It currently maps only the WhatsApp module. Earlier modules (chat, flows, voice, tickets, billing, etc.) are tracked in `docs/sop/` and `docs/superpowers/plans/` and are **not yet reconciled here**; do that in a dedicated pass rather than guessing status.

Snapshot (WhatsApp module): spike DONE; build plan DONE; backend demo slice DONE (PR open, not merged, 2026-10-27); sidecar and dashboard tracks in flight; live end-to-end with a real phone NOT done.

## WhatsApp channel (quick connect) — IN PROGRESS (spike + plan merged; backend demo slice built, awaiting merge; sidecar + dashboard in flight)
Docs: spike `docs/sop/whatsapp_pairing_spike.md`; plan `docs/superpowers/plans/2026-10-27-whatsapp-channel-backend.md`; option B memo `docs/whatsapp-option-b-decision-memo.md`.

### Spike and research (2026-10-27)
- [x] Pairing code proven live with Baileys 7.0.0-rc14 on a dedicated test number (code in 23 ms, linked, restart-survival 3.0-3.8 s, unlink detected ~0.3 s)
- [x] Signed (HMAC) webhook, inbound text + image bytes, reply delivered to phone, encrypted-at-rest creds
- [x] Evolution API and WAHA checked: pairing code present in source/docs (not run live)
- [x] Supply-chain vetting of Baileys and new deps
- [x] RAM per session measured for ONE session (~128-152 MB)
- [ ] RAM for N sessions in one process, 24 h stability (plan task T12)
- [x] Option B research, official NGN rate card, cost scenarios (memo)

### Backend demo slice (branch `feature/whatsapp-channel-backend`, text only, fake sidecar) — built 2026-10-27, PR open
- [x] T1 Channel abstraction (`app/channels/`: types, registry, `linked_device` sidecar client, `cloud_api` stub slot)
- [x] T2 Migration `wa10whatsapp` + models + enums + capability columns (single head; upgrade/downgrade proven on scratch Postgres)
- [x] T4 Connection endpoints + consent (versioned DRAFT text) + gating (`can_use_whatsapp`, `max_whatsapp_numbers`, `WHATSAPP_ENABLED`) + capacity guard (`WHATSAPP_MAX_CONNECTIONS`) + pairing code never stored/logged
- [x] T5 Signed inbound webhook + dedupe + arq bridge into `send_message` + session rotation + 402 path + `source='whatsapp'` actually persisted
- [x] T7 (text only) Outbound sender (hard stop, reply-only, retry/backoff) + single `fanout_event` hook
- [x] T9 (status only) `session.status` handling, kill switch, per-connection `ai_enabled`
- [x] Owner-types-from-phone pause (amendment a) and capacity guard (amendment b)
- [x] Account purge deletes WhatsApp rows; sidecar sessions logged out (best effort) before a tenant purge
- [x] Fake sidecar (`tests/fakes/fake_sidecar.py`) + live capture harness; FE guide with live captures; SOP
- [ ] Live end-to-end against the REAL sidecar and a real phone (lead + Adebayo)
- [ ] Opus-class review fixes merged / lead merge
- [ ] Adebayo approves the consent wording (text is a DRAFT; bump `CONSENT_VERSION` when it changes)

### Remaining build tasks (plan section 11)
- [ ] T3 Sidecar (multi-session, PG encrypted store, lease, restore, signed events, compose, CI job) (sidecar track)
- [ ] T6 Structured-message text renderer
- [ ] T7 (rest) Outbound pacing is sidecar-side; image send
- [ ] T8 Media in/out
- [ ] T9 (rest) Disconnect/plan-limit email alerts, first-responder override per connection
- [ ] T10 Dashboard Channels -> WhatsApp (luran-dashboard repo; dashboard track)
- [ ] T11 FE integration guide re-capture against the real sidecar
- [ ] T12 Soak and deploy-restore drill
- [ ] T13 Opus security review of the sidecar + beta fixes (backend demo review is done, see SOP)
- [ ] T14 Option B Cloud API adapter (blocked on Meta Tech Provider approval)
- [ ] T15 Per-tenant contact exclusion list (AI never answers personal contacts), beta, not in the demo

### Option B (Meta) track — owner actions
- [ ] Meta Business portfolio + Business Verification (Adebayo)
- [ ] Meta app (Business type, WhatsApp use case) + App Review videos + privacy/ToS URLs
- [ ] Separate +234 test number and USD card; Coexistence test on +234
- [ ] Legal read on Terms 4.1(c) and 4.7

## Backlog / upcoming
- [ ] Reconcile all earlier modules into this checklist

## Deferred follow-ups (WhatsApp)
- [ ] Sweeper for stale `awaiting_pair` rows (they count against the capacity guard)
- [ ] Plan-limit / disconnect emails (currently logged only)
- [ ] Pause/AI-off paths skip the monthly conversation-limit check
- [ ] Confirm with the real sidecar that `to_ref` (owner message) and `from_ref` (customer) are the same id for a chat
- [ ] Run Evolution or WAHA once live for the fallback record
- [ ] Decide LID <-> phone identity model before schema freeze
- [ ] Tier-driven `max_whatsapp_numbers` on `subscription_plans` (phase 2 after beta)
