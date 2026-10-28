# Luran backend — master build checklist

Legend: `[x]` done and verified, `[ ]` not done. Status per module: DONE / IN PROGRESS / PLANNED.
**Scope note:** this file was created on 2026-10-27 by the WhatsApp spike. It currently maps the WhatsApp module and the Activation module. Earlier modules (chat, flows, voice, tickets, billing, etc.) are tracked in `docs/sop/` and `docs/superpowers/plans/` and are **not yet reconciled here**; do that in a dedicated pass rather than guessing status.

Snapshot (Activation module): backend A/B/C built and verified locally (full suite, live end-to-end run); PR open, not merged; dashboard UI and staging verification not done.

Snapshot (Plan entitlements): backend built and verified (full suite, scratch-DB migration cycle, live API captures); PR open, not merged; Paystack plan for the new Lite price NOT created; dashboard not built.

Snapshot (Sidecar on staging): pipeline wiring built and validated locally (PR open, not merged); CD steps not yet run on a real runner; secrets not yet added; live test NOT done.

Snapshot (WhatsApp module): spike DONE; build plan DONE; backend demo slice DONE (PR open, not merged, 2026-10-27); sidecar and dashboard tracks in flight; live end-to-end with a real phone NOT done.

## Activation (signup -> first value) — IN PROGRESS (backend built, PR open to `develop`, not merged; dashboard track in flight)
Why: 2026-10-27 prod aggregates: 13 tenants, 0 payments, 0 of 12 trials ever uploaded a KB, 6 of 10 expired trials never had a conversation. Docs: SOP `docs/sop/activation_onboarding_backend.md`; FE guide `docs/fe-integration-guide-onboarding.md`; captures `docs/fe-integration-captures/activation/`. Branch `feature/activation-onboarding-backend`.

### A. Knowledge upload traps (each reproduced with a failing test first)
- [x] Text upload wrote rows but no vectors: root causes found and fixed (invalid `<uuid>_<n>` point ids, collection never ensured, swallowed `upsert_vector` failure); text path and worker now share `embed_and_store_chunk`
- [x] Short documents work (any text >= 20 chars becomes a chunk; the old chunker dropped the only/final chunk under 60 tokens); below 20 chars -> 422 `document_too_short` before any source exists
- [x] Chunking quality: per-topic chunks (paragraph / Q&A / heading), tiny pieces merged, nothing dropped; threshold 0.35 unchanged. Eval (`scripts/kb_retrieval_eval.py`, real embeddings): 3/8 -> 8/8 and 7/8 -> 8/8 questions answered. Existing KBs untouched (re-index = delete + re-upload)
- [x] Qdrant collection ensured idempotently (race-tolerant) before every ingestion
- [x] FAILED/INACTIVE sources never count toward the cap (pinned by tests); worker crash no longer leaves a PROCESSING source holding a slot
- [x] `failure_reason {code,message}` on sources and job status; `no_text_extracted`, `embedding_failed` (502), `vector_store_unavailable` (503)
- [x] Re-uploading identical content no longer crashes (found by the live run) and replaces the older copy on both paths

### B. Onboarding status and widget-seen
- [x] `GET /api/v1/tenant/onboarding` (knowledge verified against the vector store, widget, test conversation, `ai.blocker` precedence)
- [x] Widget-seen tracking on the public config fetch (hostname only, 10-minute throttle, ignore list `WIDGET_SEEN_IGNORED_HOSTS`)
- [x] Migration `act10onboarding` (single head, `lock_timeout`, up/down proven on scratch Postgres)
- [x] Tenant isolation tests + live proof (tenant B sees zeros; B cannot upload into A)
- [x] Opus security review of the new endpoint and what widget-seen stores (see SOP): no isolation break; text-upload guards added after it (cap, subscription, rate limit, 10 MB)

### C. Consent copy
- [x] Approved copy + `CONSENT_VERSION` `2026-10-28-draft2`; tests updated

### Delivery
- [x] FE integration guide with live captures (36) and an honest verification table
- [x] Local CI parity (ruff 0.8.6, black 24.10.0, full pytest, SQLite regression job)
- [ ] PR merged to `develop` (lead, after review)
- [ ] Dashboard activation UI against this contract (separate session)
- [ ] Verified on staging (nothing here has run on staging or prod)

### Deferred follow-ups (Activation)
- [ ] Repair legacy sources (ACTIVE, rows but no vectors) by re-embedding from `knowledge_chunks.content`, if any exist in prod
- [ ] Text upload now has the cap/subscription/rate-limit guards but no storage-MB check; a hard per-source chunk cap or separate query-embedding limiter would remove the remaining shared-limiter risk
- [ ] Reaper for sources stuck in PROCESSING after a hard-killed worker
- [ ] Route the text upload through the job queue (it embeds inside the request)
- [ ] Align `KnowledgeChunk.vector_id` model nullability with the database
- [ ] Widget-seen: add any dashboard/preview hostnames to `WIDGET_SEEN_IGNORED_HOSTS` in each environment

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
- [x] Live end-to-end against the REAL sidecar and a real phone, run locally 2026-10-27 (SOP `docs/sop/whatsapp_live_e2e.md`); NOT yet on staging (sidecar not deployed there)
- [x] Opus security review of the backend slice done; HIGH/MEDIUM findings fixed (see SOP); LOW leftovers listed as follow-ups
- [x] Lead merge (luranai-api #153, `31ce21b`, 2026-10-27)
- [x] Consent wording replaced 2026-10-28 with the copy Adebayo specified (`CONSENT_VERSION` `2026-10-28-draft2`; no "unofficial"/fear wording; old version rejected). The version string still says `draft2`; bump it whenever the text changes (activation track, PR open)

### Sidecar demo slice (branch `feature/whatsapp-sidecar`, text only) — built 2026-10-27
- [ ] T3 Sidecar (multi-session, PG encrypted store, lease, restore, signed events, compose, CI job) — **built, PR open `feature/whatsapp-sidecar`; fake-socket + real Postgres/Redis tests green; NOT yet proven live** (SOP `docs/sop/whatsapp_sidecar.md`)
  - [x] Multi-session manager, one Baileys socket per `session_key`, exact pin 7.0.0-rc14, `npm ci --ignore-scripts`
  - [x] AES-256-GCM auth store in Postgres (`whatsapp_sidecar` schema), per-session HKDF key, AAD-bound rows, batched write-behind, wipe on logout
  - [x] Redis single-writer lease (token, heartbeat, fail closed, no wipe without a confirmed 401 under lease)
  - [x] Staggered, capped boot restore; graceful SIGTERM (flush, close, release)
  - [x] Reply-only sends (24 h window, persisted), read receipt + composing + jittered delay, token bucket + daily cap, serialised
  - [x] Owner detection: `message.outbound_external` for fromMe messages not sent via `/send`
  - [x] Signed webhooks (ordered, retried, idempotent delivery id), no text/phone/code in logs
  - [x] Capacity guard (`MAX_SESSIONS`, `RSS_REFUSE_MB`) + `GET /health`
  - [x] Dockerfile (node 22.23.4 slim, non-root), `docker-compose.whatsapp.yml` (512m, no published port), CI job `whatsapp-sidecar`
  - [x] Opus security review of store, lease, webhook signing, API auth
  - [x] Live pairing through the new multi-session code (2026-10-27, local, one session; pair, KB answer, photo fallback, owner pause, unlink all proven)
  - [ ] N-session RAM soak (`whatsapp-sidecar/scripts/soak.mjs` is a skeleton)

### Sidecar on staging (branch `feature/sidecar-staging-deploy`, PR to `develop`) — built 2026-10-28
Docs: plan `docs/superpowers/plans/2026-10-28-sidecar-staging-deploy.md` (evidence, budget, owner runbook); SOP `docs/sop/whatsapp_sidecar_staging_deploy.md`. Production untouched.
- [x] Read-only investigation of the real host (limits, usage, networks, redis/db flags, project labels, env names) — 2026-10-28
- [x] Decisions: sidecar bypasses pgbouncer; dedicated least-privilege Postgres role; Redis logical DB 1; sidecar-only secrets kept out of the api env; staging api floor 768 MiB
- [x] Sidecar compose service hardened (384m, no swap, 0.5 CPU, read-only, caps dropped, no ports, log cap) + one-shot DB role init
- [x] `scripts/prepare_staging_whatsapp_env.sh` (gate, split, api floor), `smoke_whatsapp_sidecar.sh`, `generate_whatsapp_staging_secrets.sh`, `whatsapp_sidecar_db_init.sql`
- [x] `cd.yml` staging-deploy steps (skip when names are missing; `continue-on-error`); `docker.yml` GHCR image job; `ci.yml` image build check
- [x] Local validation: actionlint, yaml, shellcheck, `docker compose config`, production-job byte identity, 5 env fixtures, full local rehearsal (7/7 smoke PASS)
- [ ] PR reviewed and merged by the lead (merging deploys staging; safe before secrets: skip path)
- [ ] Owner: generate and back up secrets; add names to `.env.staging.enc` (after the PR is merged); PR + merge
- [ ] First real CD run with the sidecar enabled: health + 7 smoke PASS on the VPS
- [ ] Owner: `can_use_whatsapp` + `max_whatsapp_numbers` for ONE staging tenant; confirm `AGENT_ALERT_ENABLED` + Firebase project; install the staging-API Android build
- [ ] Live test: pair test number on staging, escalation rings the agent app, agent reply reaches WhatsApp; record `rss_mb` at 0/1/2 sessions

### Remaining build tasks (plan section 11)
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

## Plan entitlements (pricing and gates) — IN PROGRESS (backend built, PR open to `develop`, not merged; dashboard track in flight; prod untouched)
Why: plans differed only by volume plus five flags; the sales agent, Slack/CRM, recording, ratings export and WhatsApp numbers were not tied to plans, and limits lived in two tables that had drifted. Matrix approved 2026-10-28 (Lite 15,000 naira). Docs: SOP `docs/sop/plan_entitlements.md` (includes the Paystack owner-action list); FE guide `docs/fe-integration-guide-entitlements.md`; captures `docs/fe-integration-captures/entitlements/`; spec `docs/superpowers/specs/2026-10-27-pricing-and-entitlements-design.md`. Branch `feature/plan-entitlements-backend`.

- [x] One definition of the plan rows: `app/services/plan_catalog.py` (seed and migration agree; `features` strings generated from the capabilities)
- [x] New gate columns on `subscription_plans` AND `tenant_capabilities` (`can_use_sales_close`, `can_use_crm_notifications`, `can_export_ratings`; plan-driven `can_use_screen_recording`, `max_whatsapp_numbers`)
- [x] Parity test: plan columns vs capability columns vs pricing/tenant schemas
- [x] ONE resolver `get_entitlements(tenant)` (plan, then `override_flags`, then promo grant tier) used by every gate and the endpoint
- [x] Alembic `ent10entitlements`: idempotent, reversible, tenant backfill, Growth override tenant ends with everything on (proved on a scratch Postgres, upgrade / downgrade / upgrade)
- [x] Gate: meeting booking + quotes (config save, public meeting endpoints, chat offer/quote branch, sales prompt)
- [x] Gate: Slack + CRM (config save, dispatch, queued worker jobs)
- [x] Gate: screen recording (sign, register, send, widget configs) now plan-driven
- [x] Gate: ratings breakdown (admin list, analytics agent table) + new CSV export `GET /reviews/session/export`; agent self-view and rating collection stay open
- [x] AI actions limit and lookup chains read the resolver; WhatsApp numbers plan-driven (grant stays platform-only)
- [x] Downgrade conflicts for sales options, Slack/CRM and WhatsApp numbers
- [x] `GET /api/v1/tenant/entitlements` (any tenant user, own tenant only)
- [x] Lite price 1,500,000 kobo (annual 15,000,000); docs that state prices updated
- [x] FE guide with live captures; SOP
- [ ] Paystack: create the Lite 15,000 monthly/annual plans and point the env at them BEFORE the new price reaches prod (owner action, see SOP)
- [ ] Chat sales branch and the queued-worker paths verified with a real LLM / real Slack (unit/integration tests only)
- [ ] Dashboard: lock badges, upgrade prompts, 403 `feature_not_in_plan` handling (separate track)
- [ ] Docs site and public pricing page copy (after prod promotion)
- [ ] Prod promotion (migration + seed review) — lead / Adebayo

## Backlog / upcoming
- [ ] Reconcile all earlier modules into this checklist

## Deferred follow-ups (WhatsApp)
- [ ] Sidecar: purge long-dormant `logged_out` session rows; persist the daily send counter beyond Redis; second Baileys-version re-vet on every bump
- [ ] Sidecar: images/media in and out (text only in this slice)
- [ ] Background sweeper for stale `awaiting_pair` rows (today they are expired lazily on the next create); 7-day purge of `whatsapp_inbound_messages`
- [ ] Plan-limit / disconnect emails (currently logged only)
- [ ] Pause/AI-off paths skip the monthly conversation-limit check
- [x] Confirm with the real sidecar that `to_ref` (owner message) and `from_ref` (customer) are the same id for a chat (confirmed live 2026-10-27, one chat)
- [ ] Run Evolution or WAHA once live for the fallback record
- [ ] Decide LID <-> phone identity model before schema freeze
- [x] Tier-driven `max_whatsapp_numbers` on `subscription_plans` (done in Plan entitlements; the `can_use_whatsapp` grant stays platform-only)
- [ ] Connect page: warn when the tenant's 'Who answers first' is human (0-KB default) or the KB is empty; the AI stays silent otherwise and nothing says why. Per-connection first-responder override is still unbuilt
- [ ] KB: `POST /knowledge/upload` (text) stores rows but never writes vectors; short documents fail in the chunker ("No chunks could be created") with no tenant-facing message; one long multi-topic chunk scores under the 0.35 retrieval cutoff
- [ ] WhatsApp: the fixed 'please type' fallback reply is sent but not stored in the transcript
- [ ] WhatsApp: no alert to the Luran team while the AI is owner-paused; conversation list has no WhatsApp badge and shows the sender's WhatsApp name as 'Anonymous visitor'
- [ ] Deploy the sidecar to staging — wiring built 2026-10-28 (see "Sidecar on staging"); waiting on merge, secrets and the live test. Staging API container sat at 90% of its 500 MiB limit on 2026-10-27; floor raised to 768 MiB as a stopgap
- [ ] Investigate why the api containers sit at 83-90% of their limits (prod api 994 MiB / 1.17 GiB on 2026-10-28)
- [ ] Production sidecar story: GHCR promotion, dedicated Redis or `volatile-ttl`, prod DB role, limits review
- [ ] Re-private `luranai-api` after CI/CD runs (made public 2026-10-27 so Actions could run)

