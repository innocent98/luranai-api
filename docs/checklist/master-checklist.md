# Luran backend — master build checklist

Legend: `[x]` done and verified, `[ ]` not done. Status per module: DONE / IN PROGRESS / PLANNED.
**Scope note:** this file was created on 2026-10-27 by the WhatsApp spike. It currently maps the WhatsApp, Activation, Plan entitlements and Referral program modules. Earlier modules (chat, flows, voice, tickets, billing, etc.) are tracked in `docs/sop/` and `docs/superpowers/plans/` and are **not yet reconciled here**; do that in a dedicated pass rather than guessing status.

Snapshot (Activation module): backend (#158) and dashboard (#89) merged to develop/staging and running on staging 2026-10-28; prod NOT promoted.

Snapshot (Plan entitlements): backend #159 + dashboard #90 merged and running on staging; Lite Paystack plans edited to 15,000 / 150,000 in staging AND prod by Adebayo (codes unchanged); test-mode Lite checkout on staging and the Growth-override read-only prod check still owed before promotion.

Snapshot (Sidecar on staging): deployed and healthy on staging since 2026-10-28 (CD gate + smoke PASS); prod has NO sidecar yet (the production wiring is built on branch `feature/sidecar-production-deploy`, PR to `develop`, 2026-10-29, nothing run on the VPS; see the "Sidecar on production" section); see `docs/deployment/prod-release-plan-2026-10.md`.

Snapshot (Referral program): backend Tasks 1-10 built and tested on branch `feature/referral-backend` (2026-10-29); PR to `develop` NOT yet opened, whole-branch CI parity still owed (the final Opus review ran and its findings are fixed, 2026-10-29); payouts default to the `manual` provider; dashboard stream, `luran_ref` cookie script, login-page fallback, privacy-policy text and a Paystack test-mode transfer run are NOT done. Counts for this module (2026-10-29): 26 ticked, 23 open (see its section). Prod untouched.

Snapshot (WhatsApp contact exclusion, T15): backend built on `feature/whatsapp-contact-exclusion` (2026-10-29), PR to `develop` open, NOT merged; migration `wa40contactexclusion`; per-connection list of ignored contacts (webhook + worker + sender checks), live-verified against the running app with the fake sidecar; dashboard UI is a separate PR; gate for the public `WHATSAPP_ENABLED` flip. Production untouched.

Snapshot (Conversation limit numbers): built on `feature/conversation-limit-numbers` (2026-10-29), PR to `develop` open, NOT merged; migration `cl10conversationlimits`; two owner decisions open (Growth prod tenant, the 10,001-24,999 gap); prod untouched.

Snapshot (Conversation counting): built on `feature/conversation-counting-window` (2026-10-29), PR to `develop` open, NOT merged; 6-hour same-conversation window + one shared limit check; no plan numbers changed, no migration; `luran-chat` must adopt a changed `session_id`. Prod untouched.

Snapshot (WhatsApp default-on): backend built on `feature/whatsapp-default-on` (2026-10-29), PR to `develop` open, NOT merged; owner reversed the per-tenant grant (WhatsApp is now plan-driven for all tenants, `WHATSAPP_ENABLED` stays the prod kill switch, default false); capacity alert emails `PLATFORM_ALERT_EMAILS`. Dashboard work, production sidecar, `PLATFORM_ALERT_EMAILS` value and the pre-deploy override count are open. Prod untouched.

Snapshot (WhatsApp module): spike, build, sidecar, dashboard, media and rating ALL merged to develop/staging; deployed on STAGING and live-tested with real phones 2026-10-28 (text, images both ways, escalation + agent app, join line, rating reply, resolve notice). Production NOT touched: see `docs/deployment/prod-release-plan-2026-10.md`.

Snapshot (WhatsApp media + rating): backend #168 (`c9cc846`) and sidecar #167 (`93154b6`) merged and deployed on staging 2026-10-28; live-proven on real phones the same day (image with and without caption to the AI, customer image to the agent app, agent image to WhatsApp, rating request + `5` recorded, resolve notice). Voice notes, video, documents, stickers still unsupported.

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

### Media + rating, backend half (branch `feature/whatsapp-media-rating-backend`) — built 2026-10-28, merged as #168, live-proven on staging 2026-10-28
Docs: SOP `docs/sop/whatsapp_media_and_rating.md`; FE guide `docs/fe-integration-guide-whatsapp-channel.md` section 8; captures `docs/fe-integration-captures/whatsapp-media-rating/`; contract `docs/specs/2026-10-28-whatsapp-media-rating-contract.md` (outside the repo).
- [x] Inbound image: sidecar media fetch (streamed, capped, size + sha256 + mime check) -> shared image pipeline (`screen_image_bytes` / `store_screened_image`, fail-closed moderation, Cloudinary, 30-day retention) -> AI with vision
  - [x] Store-only (no AI) when owner-paused, AI off, escalated or an agent is assigned; fanned out to agents
  - [x] Moderation block: nothing stored, one fixed sentence, never the reason; unreadable / oversize / sha mismatch / expired media: placeholder + polite reply
  - [x] Plan/subscription + conversation-cap guard before any fetch, moderation or upload; redelivery is a no-op
- [x] Fallback fix: the "text only" reply fires only for `kind:"other"` and only when no human owns the chat
- [x] Outbound agent images via sidecar `send {image}` from our own stored Cloudinary URL (id-only jobs, host allowlist, 400/413/415 permanent)
- [x] Rating request forwarded to WhatsApp (`rating_requested` frame), remembered on the chat (migration `wa20ratingreply`), `1`-`5` reply recorded through `ReviewService` (same attribution as the widget), single thank-you, replay/expiry/foreign-request safe
- [x] Fake sidecar media endpoint + image send; stubs for moderation/Cloudinary; live capture rig
- [x] Join line on takeover (`You're now chatting with <first name>.`, once per agent, skipped while owner-paused) — Adebayo's decision 2026-10-28
- [x] Opus security review: NO BLOCKERS; findings 1, 2, 4, 6, 7, 9 fixed; 3, 5, 8, 10, 11 listed in the SOP as follow-ups
- [x] Live staging run with the real sidecar and a real number: image both ways, caption to the AI, rating reply, join line, resolve notice (Adebayo, 2026-10-28)
- [ ] Live: a voice note / sticker while a human owns the chat (placeholder shown to agents, no reply) — not yet exercised

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
  - [ ] N-session RAM soak — **tooling + runbook built 2026-10-28 (`test/whatsapp-sidecar-soak`, SOP `docs/sop/whatsapp_sidecar_soak.md`); the real run is NOT done** (needs 6 dedicated test numbers + a person with the phones)

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
- [x] First real CD run with the sidecar enabled: health + smoke PASS on the VPS (manual dispatch 2026-10-28; again after #167/#168, run 40713708796)
- [ ] Owner: `can_use_whatsapp` + `max_whatsapp_numbers` for ONE staging tenant; confirm `AGENT_ALERT_ENABLED` + Firebase project; install the staging-API Android build
- [x] Live test: pair test number on staging, escalation rings the agent app, agent reply reaches WhatsApp (2026-10-28)
- [ ] Record `rss_mb` at 0/1/2 sessions (one session: ~111 MB of the 300 MB refuse limit on the last smoke)

### Remaining build tasks (plan section 11)
- [ ] T6 Structured-message text renderer
- [ ] T7 (rest) Outbound pacing is sidecar-side; image send (sidecar side built 2026-10-28, not proven live, see T8)
- [ ] T8 Media in/out — **sidecar half built 2026-10-28** (branch `feature/whatsapp-media-sidecar`, PR to `develop`, tests green, NOT proven on a real phone): inbound image download + bounded in-memory store + `GET /sessions/{key}/media/{id}`, outbound `image:{url,caption}` with SSRF-guarded fetch; backend half is a parallel track (SOP `docs/sop/whatsapp_sidecar.md` "Images")
  - [x] Sidecar: inbound jpeg/png/webp/gif download (10 MB cap on received bytes), `media_too_large` / `media_unavailable` skips
  - [x] Sidecar: media store (10 min TTL, 8/session, `MEDIA_STORE_MAX_MB`, per-session isolation, cleared on logout)
  - [x] Sidecar: outbound image send (allowlist, pinned DNS, no redirects, 10 s, 10 MB, type + magic bytes), same pacing/caps as text
  - [x] Compose: `/tmp` tmpfs 32m, `MEDIA_*` env passthrough
  - [x] Live: real-phone image in and out (2026-10-28)
  - [ ] Live: >8 MB image on the 32m tmpfs, `rss_mb` with images
  - [x] Backend half (fetch from sidecar, vision pipeline, forward agent images) — #168, live-proven 2026-10-28
- [ ] T9 (rest) Disconnect/plan-limit email alerts, first-responder override per connection
- [ ] T10 Dashboard Channels -> WhatsApp (luran-dashboard repo; dashboard track)
- [ ] T11 FE integration guide re-capture against the real sidecar
- [ ] T12 Soak and deploy-restore drill
  - [x] Soak harness: collector (`/health` + container cgroup memory + step marker to CSV), analysis with recommended limits, image generator, fake-socket dry run; 27 unit tests on synthetic CSV fixtures; probe verified inside `node:22.23.4-slim` under cgroup v2 — 2026-10-28, SOP `docs/sop/whatsapp_sidecar_soak.md`
  - [x] Runbook with exact commands (baseline, 1/2/4/6, text load, image burst, 24 h hold, restart storm)
  - [ ] RUN on staging: baseline at 0 sessions
  - [ ] RUN: marginal RSS at 1 / 2 / 4 / 6 paired test numbers
  - [ ] RUN: image burst at the highest N (inbound photos; outbound 9.5 MiB if the product allows)
  - [ ] RUN: idle hold, 1 h and 24 h marks
  - [ ] RUN: reconnect storm (restart), time to all sessions restored + peak RSS
  - [ ] Attach the report to the memory snapshot SOP; then size the production sidecar (limit, `MAX_SESSIONS`)
  - [ ] Deploy-restore drill (separate from the RAM soak)
- [ ] T13 Opus security review of the sidecar + beta fixes (backend demo review is done, see SOP)
- [ ] T14 Option B Cloud API adapter (blocked on Meta Tech Provider approval)
- [x] T15 Per-connection contact exclusion list (AI never answers personal contacts): backend built 2026-10-29 on `feature/whatsapp-contact-exclusion`, PR to `develop` open, NOT merged. SOP `docs/sop/whatsapp_contact_exclusion.md`, FE guide `docs/fe-integration-guide-whatsapp-exclusion.md` (live captures). Gate for the public `WHATSAPP_ENABLED` flip.
  - [x] Model + migration `wa40contactexclusion` (additive, reversible, single head)
  - [x] Peppered-HMAC entries (phone + learned LID), encrypted mask/label, no full number stored
  - [x] CRUD: add / bulk (partial success) / list (paged) / remove; plan-gated add; tenant-scoped
  - [x] Drop before any side effect: webhook, worker (`dropped_excluded`), sender (queued replies, agent takeover)
  - [x] Tests: SQLite + real Postgres + migration round trip; live-verified webhook and worker drops
  - [ ] Dashboard UI (luran-dashboard, separate PR; needs listed in FE guide section 8)
  - [ ] Known gap: first message from an excluded contact whose LID has no resolvable number (see SOP follow-ups)

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
  - 2026-10-30: re-run end to end locally on the real code with a stand-in LLM, local Slack/CRM receivers and a mail stub: 69/69 live checks + 396 tests pass (SOP `docs/sop/sales_agent_smoke_2026-10-30.md`). Still open: real model, real Slack/CRM/inbox, staging/prod. Defects from it: quote PDF naira box, lead score not sticky and rep-email CTA `#` are fixed in PR #194 (pending merge, SOP `docs/sop/sales_quote_pdf_score_link_fixes.md`); still open: WhatsApp does not carry the quote card/picker (catalog plan).
- [ ] Dashboard: lock badges, upgrade prompts, 403 `feature_not_in_plan` handling (separate track)
- [ ] Docs site and public pricing page copy (after prod promotion)
- [ ] Prod promotion (migration + seed review) — lead / Adebayo

## Referral program (marketer recruits) — IN PROGRESS (backend built 2026-10-29, PR not yet opened; dashboard stream and owner actions open; prod untouched)
Why: Luran wants marketers and influencers to bring businesses, paid a one-time 5,000 naira only after the referred tenant's first successful payment (zero up-front cost; prod has 13 tenants, 0 payments, ~1.6 signups a month). Docs: SOP `docs/sop/referral_program_backend.md`; FE guide `docs/fe-integration-guide-referral.md`; captures `docs/fe-integration-captures/referral/` (78, from `scripts/capture_referral.py`); spec `docs/superpowers/specs/2026-10-28-referral-program-design.md`; plan `docs/superpowers/plans/2026-10-28-referral-program-backend.md`. Branch `feature/referral-backend` (migration `ref10referral`, down_revision `wa20ratingreply`). PR: [innocent98/luranai-api#174](https://github.com/innocent98/luranai-api/pull/174).

### Task 1 prerequisite: idempotent `charge.success` (money path the reward hangs on)
- [x] `_handle_charge_success` returns `already_processed` for a settled reference under a row lock (no second upgrade, history row or `paid_at` overwrite) — `fix/charge-success-idempotent`, PR #173 (CI green 2026-10-29); also cherry-picked into the feature branch as `0101ae0`
- [ ] #173 merged to `develop` (lead); then drop the duplicate commit on rebase

### Task 2: schema and settings
- [x] Models: `Marketer`, `ReferralAttribution`, `ReferralReward`, `PayoutBatch`, `PayoutItem`, `SignupOrigin`, `ReferralConfig`; native enums; all `*_kobo` columns BigInteger
- [x] Migration `ref10referral` (additive, `lock_timeout`, existence-checked, reversible; up/down/up proven on scratch Postgres; single head)
- [x] Settings `REFERRAL_REWARD_KOBO`, `REFERRAL_HOLD_DAYS`, `REFERRAL_COMMISSION_WINDOW_DAYS`, `REFERRAL_COOKIE_DAYS`, `REFERRAL_PAYOUT_PROVIDER` (default `manual`), `REFERRAL_LINK_BASE_URL`; admin overrides in `referral_config`
- [x] Account purge anonymises/detaches referral rows; a reward survives a tenant purge

### Task 3: marketer onboarding (super admin)
- [x] Create / list / get / bank update / suspend / reactivate under `/api/v1/platform/referrals/`; code rules (3-32, lowercase, reserved words, immutable); bank details encrypted and write-only (`bank_last4` only)
- [x] One-time invite token (7 days) and invite email; suspend holds unpaid rewards; reactivate does not release them
- [x] Writes require SUPER_ADMIN, reads any platform admin (security-review finding, fixed)
- [x] Create / bank update / suspend / reactivate commit atomically with their audit entry (an audit failure rolls the change back with a fixed 500; final review M2)

### Task 4: marketer identity and auth
- [x] Separate identity: `admin_type="marketer"`, own cookies, `get_current_marketer` (cookie only); login / refresh / logout / set-password / forgot-password; identical 401 for unknown email, wrong password and no password; 403 `account_suspended`
- [x] Isolation proven in both directions (every cross-identity combination is exactly 401); invite/reset token retirement; forgot-password cannot kill an invite (Opus + security-review findings, fixed)

### Task 5: attribution
- [x] `referral_code` / `referral_source` on `register-company`; unknown or malformed code silently ignored (identical 201); first link wins; self-referral recorded and hidden; salted-hash signup origin

### Task 6: fraud flags
- [x] Registrable-domain matching (`tldextract` 5.3.1 offline, snapshot 2025-04-07); `shared_domain`, `shared_card`, `shared_origin`, `self_referral`, `marketer_burst`; social domains ignored; evidence source recorded per match
- [ ] `authorization.signature` confirmed in a real Paystack payload (never seen live)

### Task 7: reward lifecycle
- [x] Reward on the first successful payment inside the commission window (savepoint in the webhook, daily reconcile); `pending` to `payable` after the hold; release / void with a mandatory note and atomic audit; dispute hold (`charge.dispute.create`)
- [ ] A real `charge.dispute.create` payload checked against what the handler reads

### Task 8: payouts
- [x] Payout batches (cron on the 1st and 15th, 04:00 UTC), items, approve / retry / mark-paid, manual and Paystack Transfers providers, `transfer.*` webhook events
- [x] Double-pay defences (claim-then-call, verify-first retry, amounts recomputed from batched rewards, `reward_in_flight`, `amount_mismatch`); the Opus-found double-pay defect fixed and re-reviewed
- [x] Final-review fix wave (2026-10-29): `awaiting_manual` is treated like `processing`/`failed` for a dispute hold (the reward keeps its batch link; `release`/`void` refuse) and `mark-paid` accepts the claimed or the current amount (closes a double-pay path in the manual flow)
- [x] Paystack-era `failed`/`processing` items are verified with Paystack after the provider is switched back to `manual`
- [x] `POST /api/v1/platform/referrals/batches/prepare` (super admin, audited, idempotent) and a 3-retry deadlock policy for the `referral_prepare_batch` cron
- [ ] Paystack Transfers exercised in test mode (not-found / failed / reversed shapes, OTP handling) — owner action

### Task 9: marketer API and admin queue
- [x] `/api/v1/marketer/me|referrals|earnings` (allow-list responses, masked email, rate limited, bounded); admin reward queue with shared-domain evidence; config GET/PATCH with bounds

### Task 10: notifications, captures, docs
- [x] Emails: invite, reset, conversion notice, payout notice (once per transition, after commit, never raised)
- [x] 78 live captures against a local app on scratch Postgres/Redis; FE guide built only from them and checked block by block
- [x] SOP and this checklist entry (2026-10-29)
- [ ] Local CI parity on the whole branch (ruff 0.8.6, black 24.10.0, full pytest, SQLite regression job)
- [x] Opus whole-branch review (`e4ee8db..0d4b38f`: no Critical, 1 Important, minors) and the fix wave (2026-10-29)
- [x] Marketer page follows R44: a `void` reward shows `not_converted`; capture 69's `signed_up` is explained by a failed upgrade in the scratch run (FE guide 6.2, SOP)
- [x] Unmatched dispute logs an ERROR with Paystack's dispute id; SMTP socket timeout raised to 30 s; exception text no longer logged in the new referral code
- [ ] PR opened to `develop`, reviewed and merged (lead)

### Not done: frontend and owner items
- [ ] Dashboard: marketer page `/marketer`, admin screens (marketers, review queue, payout batches, settings) against `docs/fe-integration-guide-referral.md`
- [ ] `luran_ref` cookie script on `luranai.com` (30 days, first link wins) and the optional "Referral code" field on the signup form
- [ ] Login page falls back from the tenant login to `POST /api/v1/auth/marketer/login`
- [ ] Privacy-policy text for the `luran_ref` cookie and the commission programme
- [ ] Owner: decide Paystack API transfer OTP handling (or disable it for API transfers); keep the balance funded
- [ ] Owner: run one small Paystack test-mode transfer and capture the real shapes
- [ ] Owner: confirm container `TZ=UTC` and `FORWARDED_ALLOW_IPS` covers the real proxy
- [ ] Owner: set `REFERRAL_PAYOUT_PROVIDER=paystack` (only after the items above)
- [ ] First live payout batch approved and reconciled
- [ ] Prod promotion (migration `ref10referral`) — lead / Adebayo

### Deferred follow-ups (Referral)
See the grouped list in `docs/sop/referral_program_backend.md` ("Known gaps / Follow-ups"). Headlines:
- [ ] Test fixture `db_session` (`tests/conftest.py`): replace the SAVEPOINT-restart listener with `join_transaction_mode="create_savepoint"` (review M4; today an app-level `rollback()` wipes the whole test)
- [ ] Dispute webhook: look the transaction up by its numeric id when Paystack sends no reference (today it only logs an ERROR with the dispute id)
- [ ] `CHECK (amount_kobo > 0)` constraints on reward and payout amounts
- [ ] Approve/retry run provider calls inside the request (background worker for larger batches)
- [ ] No single kill switch for the whole programme; batch days hardcoded to the 1st/15th
- [ ] Refresh-reuse detection and a `password_changed_at` check for marketer tokens
- [ ] SQL prefilter for the shared-domain sweep when the tenant count grows; periodic `tldextract` bump

## WhatsApp default-on (plan-driven access + capacity alert) — BUILT 2026-10-29, PR open to `develop`, not merged; dashboard and prod rollout open
Docs: SOP `docs/sop/whatsapp_default_on.md`; FE guide `docs/fe-integration-guide-whatsapp-default-on.md`; captures `docs/fe-integration-captures/whatsapp-default-on/`; plan `docs/superpowers/plans/2026-10-29-whatsapp-default-on.md`. Decision change by Adebayo 2026-10-29 (reverses the 2026-10-28 platform grant).
- [x] `subscription_plans.can_use_whatsapp` (true on all tiers) + catalog + feature string; `can_use_whatsapp` leaves the tenant-only list
- [x] Migration `wa30whatsappdefaulton` (backfill skips `override_flags`; up/down/up proven on scratch Postgres and by hand on a live local DB)
- [x] Resolver: `whatsapp.enabled` follows plan/override; additive `platform_available` (kill switch); inbound gate via resolver; downgrade-conflict on the flag
- [x] Admin revoke (`false` override) survives plan changes, downgrade, promotional grants and tier override (Opus review finding, fixed)
- [x] Capacity alert: `PLATFORM_ALERT_EMAILS`, cron every 5 minutes, Redis dedupe 6 h per condition, post-create hook, SMTP failure never raised
- [x] Stable tenant-facing 503 `capacity_exceeded` text
- [x] Tests (override respected, backfill idempotent, new-tenant default, revoke survives migration, entitlements payload, alert thresholds and dedupe, email failure does not break create) + live captures + FE guide + SOP
- [ ] Owner, before deploying anywhere real: count tenants that will stay dark (`override_flags` has `can_use_whatsapp` and stored false) - query in the SOP
- [x] Owner: `WHATSAPP_MAX_CONNECTIONS` set to 10 in the encrypted prod env (2026-10-30, with the matching sidecar caps; PR #187); `PLATFORM_ALERT_EMAILS` not re-checked here
- [ ] Dashboard: hide the WhatsApp section unless `platform_available && enabled`; remove the "ask Luran to enable" state; relabel the admin "WhatsApp grant" as access/revoke; new capacity wording (separate task)
- [ ] Admin cannot see or clear a `can_use_whatsapp` override (`override_flags` not in the platform response)
- [ ] Contact exclusion list (T15) and the Terms wording (separate follow-ups)

## Conversation counting (6-hour window, one shared limit check) — BUILT 2026-10-29, PR open to `develop`, not merged; no plan numbers changed
Docs: SOP `docs/sop/conversation_counting.md`; FE guide `docs/fe-integration-guide-conversation-counting.md`; captures `docs/fe-integration-captures/conversation-counting/`. Branch `feature/conversation-counting-window`. Step 1 before the conversation-limit numbers change.
- [x] One module `app/services/conversation_counting.py`; the five count copies replaced; dead `verify_conversation_limit` deleted
- [x] `CONVERSATION_REUSE_WINDOW_HOURS` (default 6): anonymous + authed + WhatsApp continue an abandoned/resolved session inside the window (reopened in place, not recounted); beyond it a new counted session
- [x] Getters made safe (latest by `last_message_at`, `LIMIT 1`)
- [x] Gaps closed: `POST /chat/sessions` gated, voice mint gated, reactivation past the window counts as new
- [x] Tests (boundary, anonymous/authed/WhatsApp, 402 shape, ungated paths, month rollover, multi-row getters) + live captures + FE guide + SOP
- [ ] `luran-chat`: adopt a changed `session_id` from any response (server tolerates the old id meanwhile)
- [ ] Re-baseline the proposed plan limits (Lite 1,000 / Pro 5,000 / Growth 10,000 / trial 100) on post-fix counts, then change them in a separate PR
- [ ] Dashboard usage-meter wording

## Conversation limit numbers (Trial 100 / Lite 1,000 / Professional 5,000 / Growth 10,000) — BUILT 2026-10-29, PR open to `develop`, not merged; prod untouched
Owner decision 2026-10-29 (step 2 after the counting meter). Docs: SOP `docs/sop/conversation_limit_numbers.md`; FE guide `docs/fe-integration-guide-entitlements.md` ("Monthly conversation limits"); captures `docs/fe-integration-captures/conversation-limits/`; prod usage query `docs/handoff/prod-conversation-usage-readonly.sql`. Branch `feature/conversation-limit-numbers`.
- [x] Plan catalog numbers + generated feature strings (`app/services/plan_catalog.py`); Enterprise stays unlimited
- [x] Alembic `cl10conversationlimits` (parent `wa30whatsappdefaulton`, single head): plan rows, one feature string each, tenants on the old plan number and without an override; idempotent; reversible; WARNING per over-limit tenant; proved on a scratch Postgres (upgrade / downgrade / upgrade, 43 tests)
- [x] Parity test repointed (ent10 + wa30 + cl10); pricing helper (Growth cap 10,000, `custom` answer for 10,001-24,999)
- [x] Email template, staging seed, dev script, plan docs, knowledge base, superseded banners
- [x] Live captures + FE guide section (public plans, tenant numbers, admin override and what clears it, pricing compare)
- [x] Read-only prod usage SQL for the owner
- [ ] **Owner decision:** the one active Growth tenant on prod moves 25,000 -> 10,000 unless an override is set
- [ ] **Owner decision:** what covers 10,001-24,999 conversations/month (Enterprise copy still says 25K+)
- [ ] Owner runs the read-only SQL on prod before the promotion; act on every row with `over_new_limit_by > 0`
- [ ] Dashboard follow-up (separate repo, not changed here): `app/components/luran/pricing/plans.ts` static fallback (200/2000/10000/25000), platform-admin calculator copy (25,000), `luran-docs` pricing page/API doc
- [ ] In-app notice at 80% / 100% of the monthly limit (none today; the widget only gets the 402)

## Backlog / upcoming
- [ ] Reconcile all earlier modules into this checklist
- [ ] Referral program: dashboard stream (marketer page + admin screens) is next, against `docs/fe-integration-guide-referral.md`

## Production release (activation + entitlements + WhatsApp dark) — PLANNED, not started
Plan: `docs/deployment/prod-release-plan-2026-10.md` (DRAFT 2026-10-28). Merging `develop` -> `main` deploys prod automatically; WhatsApp ships off by default.
- [ ] Read-only prod checks: tenants per tier, Lite/other tenants using gated features, Growth override flags, paying Lite subscribers, `alembic current` = `pf10planfeatures`
- [ ] Per-tenant decision (cut / grandfather via `override_flags` / hold) for anyone the gates would take features from
- [ ] Owner: test-mode Lite checkout on staging (15,000 / 150,000); fix GitHub Actions spending limit; re-private `luranai-api`
- [ ] Manual DB dump + `docker stats` baseline, then PR `develop` -> `main`; verify health, migrations at `wa20ratingreply`, pricing, entitlements, worker functions
- [ ] Dashboard `staging` -> `main` (15 commits) right after the backend; `luran-chat` `stage` -> `main` (3 commits)
- [ ] Production sidecar job: **(Redis now isolated, see "Sidecar Redis isolation")** wiring built 2026-10-29 (branch `feature/sidecar-production-deploy`, PR to `develop`, SOP `docs/sop/whatsapp_sidecar_production_deploy.md`)**; still open: secrets in `.env.production.enc`, first deploy + smoke, consent/ToS wording, flipping `WHATSAPP_ENABLED`, per-tenant grant (see "Sidecar on production")

## Sidecar on production (dark) — BUILT, PR open to `develop`, nothing run on the VPS (2026-10-29)
Docs: SOP `docs/sop/whatsapp_sidecar_production_deploy.md` (owner actions in order, memory budget, rollback, rulings). Branch `feature/sidecar-production-deploy`. Mirrors the staging job; `WHATSAPP_ENABLED` is never turned on by it.
- [x] `scripts/prepare_production_whatsapp_env.sh`: production guard (project `luran`, `ENVIRONMENT` production), gate on names + format, sidecar-only secrets stripped from the api env, **byte-identical no-op when no sidecar names exist**
- [x] `docker-compose.whatsapp.production.yml` (container `luran_whatsapp_sidecar`, 384m / RSS 216 / 3 sessions); staging effective config proven unchanged
- [x] `cd.yml` `production-deploy`: gate (fail-closed), GHCR image promotion, copy, deploy with server-side `luran_api` project/working-dir guards, health, smoke, status; diff is pure insertion, existing steps byte-identical
- [x] `scripts/generate_whatsapp_production_secrets.sh` (writes `WHATSAPP_ENABLED=false`); smoke `SMOKE_BACKEND=disabled` mode + CREATE-privilege check
- [x] `scripts/whatsapp_sidecar_snapshot.sh` + cron instructions + tests; 55 pytest tests in `tests/deploy/`
- [x] Local rehearsal: the real ssh script from `cd.yml` run against throwaway Postgres/Redis + the real image (smoke 7 PASS + 1 SKIP disabled, 8 PASS enabled; guards refuse; idempotent)
- [ ] PR merged to `develop` (lead), then promoted to `main` (no secrets yet: skip path)
- [x] Owner: secrets generated, `WHATSAPP_AUTH_MASTER_KEY` backed up, names added with `WHATSAPP_ENABLED=false`, promoted (#181 / #183; prod sidecar up and healthy, dark)
- [ ] First sidecar deploy in a quiet window (not near 02:00); smoke 7 PASS + 1 SKIP; install snapshot cron
- [ ] Flip `WHATSAPP_ENABLED=true`: encrypted env PR #187 open (also ceiling 10 + sidecar Redis password); consent/ToS wording approved; WhatsApp is plan-driven default-on, so there is no per-tenant grant: invite pilots on the normal trial and watch the snapshot log; strict smoke after the promotion that carries #186 + #187
- [ ] Not proven anywhere: the new CD steps on a real runner/VPS, live prod Redis/Postgres settings, more than 2 sessions

## WhatsApp connect requires an active subscription — BUILT 2026-10-30, PR open to `develop`, not merged
Docs: SOP `docs/sop/whatsapp_connect_requires_active_subscription.md`; FE guide `docs/fe-integration-guide-whatsapp-connect-subscription-gate.md`. Branch `fix/whatsapp-connect-requires-active-subscription`.
- [x] Reproduced with failing tests: an expired-trial / canceled / past_due tenant could link a number and take a launch slot (10 of 13 prod tenants are `trial_expired`)
- [x] `verify_subscription_active` on `POST /whatsapp/connections` and `POST /whatsapp/connections/{id}/pairing-code` (402; list/patch/delete stay open); active, trialing and promo-granted still connect
- [x] Inbound of an already-linked lapsed number: already stops answering (`_plan_blocks_conversation`), message stored, no reply — no change
- [ ] PR merged to `develop` (lead), then promoted
- [ ] Dashboard: map 402 `error` values (`subscription_inactive`, `trial_expired`, `subscription_expired`) on the connect page; hide the form when entitlements say `enabled: false`
- [ ] Lapsed tenants' already-linked numbers still count toward `WHATSAPP_MAX_CONNECTIONS` and hold a sidecar session; no cleanup job (decide)
- [ ] `_plan_blocks_conversation` ignores `subscription_ends_at` for `active` tenants (use `entitlements.is_lapsed`)

## Sidecar Redis isolation (production) — BUILT, PR open to `develop`, nothing run on the VPS (2026-10-30)
Docs: plan `docs/superpowers/plans/2026-10-30-sidecar-redis-isolation.md`; SOP `docs/sop/whatsapp_sidecar_redis_isolation.md` (owner steps in order, rollback, what is not verified). Branch `feature/sidecar-redis-isolation`. Resolves the "decide before `WHATSAPP_ENABLED` is flipped" ruling of the production sidecar SOP. No production container restarts; `docker-compose.prod.yml` untouched.
- [x] Findings from the code: the sidecar's Redis holds only `wa:lease:*` (30 s) and `wa:daily:*` (26 h); the backend never reads them, so only the sidecar needs the new Redis
- [x] Own Redis `luran_whatsapp_redis`: ACL user limited to `wa:*` keys, password only in `.env.sidecar`, `noeviction`, `maxmemory 8mb`, 32m, `save 60 1` on a named volume (daily send counter), on an internal-only network
- [x] Sidecar off `luran_default`; shares `luran_wa_backend` only with `luran_api`, `luran_worker`, `luran_db` (runtime `docker network connect`, nothing restarted)
- [x] New sidecar-only name `WHATSAPP_SIDECAR_REDIS_PASSWORD`: gate fail-closed (old names without it skip, names only), stripped from the api/worker env, generator `--redis-only`
- [x] `cd.yml` (inside `production-deploy` only): network steps after every API deploy, Redis started and awaited before the sidecar, isolation smoke (18 PASS + 1 SKIP)
- [x] 94 pytest tests in `tests/deploy/` (was 55); local rehearsal with the real ssh scripts: sidecar healthy, cannot reach prod Redis/Qdrant/pgbouncer (DNS and by IP), Redis requires the password, api/worker hold no sidecar secrets, rerun no-op, counter persists
- [ ] PR merged to `develop` (lead), then promoted
- [ ] Owner: `scripts/generate_whatsapp_production_secrets.sh --redis-only`, add `WHATSAPP_SIDECAR_REDIS_PASSWORD` to `.env.production.enc` (after the PR is on `develop`), PR + promote; read the deploy log (18 PASS + 1 SKIP)
- [ ] Not proven anywhere: the new CD steps on a real runner/VPS, Linux bridge isolation on the VPS, a live WhatsApp session through the new Redis, memory at 3 or 10 sessions
- [ ] Follow-ups: declare `luran_wa_backend` on api/worker in the prod compose (never db); auth for the shared prod Redis + Qdrant key; sidecar egress filtering; pin images by digest

## Memory and capacity — OPEN (snapshot `docs/sop/memory_and_capacity_snapshot_2026-10-28.md`)
- [ ] Split prod/staging API memory (anon vs cache vs slab) now and after 24 h
- [ ] Explain the prod API's 1.94 GB of block reads; test the 30 s Python healthcheck as a cause
- [ ] Decide worker count / `--preload` from measurements (prod stays at 2 workers, 1.172 GiB until then)
- [ ] Sidecar soak at 1/2/4/6 sessions with load, then size the prod sidecar limit and `MAX_SESSIONS` — tooling + runbook built (`docs/sop/whatsapp_sidecar_soak.md`), **run pending**
- [x] Interim prod sizing from the 21 h / 2-session staging measurement (peak RSS 144 MB, container peak 97.3 MiB): RSS refuse 216, limit 384m, `MAX_SESSIONS` 3, API cap 3; the third session is extrapolated (SOP `whatsapp_sidecar_production_deploy.md`). Raising it still needs the soak
- [x] Shared-host budget table with the prod sidecar added (about 2.25 GiB expected of 7.8 GiB; limits sum 9.1 GiB)

## Deferred follow-ups (WhatsApp)
- [ ] Sidecar: purge long-dormant `logged_out` session rows; persist the daily send counter beyond Redis; second Baileys-version re-vet on every bump
- [ ] WhatsApp images: both halves merged and PROVEN LIVE on staging 2026-10-28; audio, video, documents and stickers still unsupported; no outbound thumbnail (needs sharp/jimp)
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
- [x] Deploy the sidecar to staging: deployed and smoke-tested 2026-10-28 via a manual dispatch (PR #165 fixed the env-permission bug the first real run exposed); live test passed the same day (SOP `docs/sop/whatsapp_staging_live_test.md`).
- [ ] Investigate why the api containers sit at 83-90% of their limits (prod api 994 MiB / 1.17 GiB on 2026-10-28)
- [x] Production sidecar story: GHCR promotion (`:develop` image, no rebuild), prod DB role, limits review (built 2026-10-29, PR open); dedicated Redis / network isolation deliberately NOT done, see below
- [x] (done 2026-10-30, #186; deploys with the flip promotion) Before `WHATSAPP_ENABLED` is flipped in prod: dedicated Redis on a private network + ACLs and a Qdrant API key (the sidecar currently shares the default network and Redis DB 1 with unauthenticated services; staging review caveat)
- [ ] Re-private `luranai-api` after CI/CD runs (made public 2026-10-27 so Actions could run)
- [ ] WhatsApp: the rating request ("How was your chat with <agent>?") is sent as plain text on resolve, but a digit reply is not parsed, so the customer cannot answer it; implement 1-5 reply handling (plan T6) or suppress the request on WhatsApp until then
- [ ] WhatsApp media: customer photos reach agents only as a placeholder; the "I can only read text" fallback is also sent while a human agent owns the conversation (plan T8, about 1-1.5 agent-days)
- [ ] Agent app: the live takeover screen shows no earlier Luran-AI turns (by design: the WebSocket replay carries only customer and human-agent turns); give agents the AI context (Flutter release)
- [ ] Re-confirm the staging API memory floor (768m) after `cd.yml` reaches `main`: automatic deploys use main's workflow file, so the floor only applied to the manual dispatch run

