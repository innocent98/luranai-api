# WhatsApp channel (quick connect) — Backend Build Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. Evidence base: `docs/sop/whatsapp_pairing_spike.md` (pairing proven live). Option B facts: `docs/whatsapp-option-b-decision-memo.md`.

**Goal:** A tenant enters a WhatsApp number in the dashboard, gets a pairing code, links it, and the existing Luran AI/human pipeline answers customers on WhatsApp within minutes. Built behind ONE `Channel` abstraction with two adapters: **A** linked-device via a Baileys sidecar (flagged beta, reply-only, ToS-risk consent) and **B** the official Meta Cloud API (Embedded Signup; plan only until Meta approval).

**Architecture (one paragraph):** WhatsApp inbound (A: signed webhook from the Node sidecar; B: Meta webhook) is verified, deduped, and enqueued to arq. A worker normalizes it into a `MessageRequest` and calls the existing `send_message` wrapper (the single AI pipeline, `_send_message_impl`), then renders `ChatResponse.message` (plus `meta`) to plain WhatsApp text and enqueues an outbound job. Agent/system messages reach WhatsApp from one hook inside `connection_manager.fanout_event` (runs once at the originating worker) so the REST agent endpoint, the agent WebSocket and the system notices are all covered. A per-connection token bucket + jitter paces every send.

**Tech stack:** FastAPI + SQLAlchemy 2 + Alembic + arq (this repo); Node 20 + Baileys 7.0.0-rc14 pinned (new `whatsapp-sidecar/` service); Next.js (`luran-dashboard`, separate repo).

---
## 0. Code facts this plan builds on (re-verified in this worktree, base `origin/develop` = `1ac84d2`)
| Fact | Where | Consequence |
|---|---|---|
| Single AI pipeline | `app/api/v1/endpoints/chat.py:1040` `_send_message_impl(message_data, db, linked_attachments, authed_customer_id)`; wrapper `send_message` at `:931` (public route `post_chat_message:910` adds `chat_rate_limit`; programmatic precedent `voice.py:704`) | Call `send_message` from the worker; set a `channel` ContextVar like `voice_channel` (`conversation_service.py:42`) so messages are tagged `meta.channel="whatsapp"` |
| `ChatResponse` has only `session_id, message, user_message_attachments, should_escalate, escalation_reason, ticket_id` | `app/schemas/conversation.py:224` | All widget UI lives in `message.meta` (dict); the renderer reads `meta` |
| `source` never written today | `ConversationSession.source` `conversation.py:110`; neither `create_session` call site in `chat.py` (`:891`, `:1171`) passes it; `coerce_source` (`app/utils/attribution.py:8`) maps unknown values to `"unknown"` | Add `ConversationSource.WHATSAPP` (`app/schemas/explore.py:25`) **and** pass `source`; otherwise it silently becomes `unknown`. `SourceMeta` (`explore.py:34`) is `extra="ignore"`: add `channel_connection_id` / `provider` or they are dropped |
| Visitor stores phone encrypted + `phone_hash` | `visitor.py:73-83`; listener `:242` | `hash_for_lookup` is **unsalted SHA-256** (`encryption.py:82`): low-entropy phones are brute-forceable. Use a peppered HMAC for the WhatsApp contact key. No lookup-by-phone helper exists; `VisitorSource` has only `widget`/`support_page` (Postgres enum -> `ALTER TYPE ... ADD VALUE`) |
| Agent->customer paths | `send_agent_message` `chat.py:3384` (persist `:3443`, fanout `:3472-3486`); agent WS `websocket.py:438` (persist ~`:636`, fanout `:656-671`); `emit_agent_joined` `conversation_service.py:~700`; ticket resolved/closed `tickets.py:355-427`; missed notice `workers/agent_alerts.py:~225`; rating request `rating_request_service.py:~330` | `fanout_event` (`core/websocket.py:310`) is the single choke point with `to_customer=True`. Hook **inside `fanout_event`, not `_deliver_event_local`** (the latter re-fires on every worker via Redis). WS-only sends that bypass it: `send_to_customer:144`, typing `:415`, `agent_left` ~`websocket.py:697` |
| AI replies are NOT fanned out | returned in the HTTP response of `send_message` | The inbound worker sends `response.message.content` itself |
| arq | `worker.py` -> `WorkerSettings` in `app/workers/document_processor.py:1203` (27 functions, cron list); enqueue helpers `app/services/notification_enqueue.py` (`enqueue_agent_alerts_async` = fail-fast template) | New jobs must be added to `functions` (and `cron_jobs`) in that one file |
| Encryption | `app/core/encryption.py`: Fernet `MultiFernet` via `ENCRYPTION_KEY[_PREVIOUS]`; `EncryptedString`, `EncryptedJSON` | Use for tokens (option B) and phone. Sidecar auth state uses its own key (below) |
| Plan limit | monthly conversation count enforced only on **new-session creation** (`chat.py:1136-1168`, HTTP 402) and not on thread reuse; `verify_conversation_limit` (`deps.py:326`) has no callers | The WhatsApp worker must catch the 402; see section 6 |
| Capabilities | `TenantCapabilities` (`tenant.py:381`) vs `SubscriptionPlan` (`subscription_plan.py`); voice and screen-recording flags are **tenant_capabilities only** (grant-only); tier sync in `subscription_service.py:63`; platform-admin edit `PUT /platform/tenants/{id}/capabilities` uses `extra="forbid"` | Beta = grant-only flag; tier-driven count = columns on both tables + `get_capabilities`/`to_dict`/`update_plan`/seed |
| First responder | tenant-wide `TenantAIConfig.first_responder` (`tenant.py:332`), resolved at `chat.py:1269` | Add nullable per-connection override |
| Image upload | `_process_image_upload` (`chat.py:162`) takes `UploadFile`; 10 MB, JPEG/PNG/GIF/WEBP only, **OpenAI omni-moderation fail-closed** (`image_moderation.py`), Cloudinary, 30-day retention | Refactor to accept bytes; WhatsApp images are JPEG/WEBP |
| Webhook precedent | `POST /webhooks/paystack` (`webhooks.py:16`): raw body, HMAC, `compare_digest`, rate limit, 200 on handler error | Same shape. **Do not copy** `paystack_service.py:~499`, which logs a prefix of the expected signature |
| Migrations / CI | single head `pf10planfeatures` (from file parse; confirm with `alembic heads`); CI runs `ruff check app/`, `black --check app/`, `pytest tests/` (SQLite regression + Postgres); **CI never runs alembic and tests use `create_all`** | A model/migration mismatch will not fail CI: add a migration test + manual `upgrade head`/`downgrade -1` on a scratch DB |
| Compose limits | api 900m, worker 700m, db 1200m, qdrant 900m, redis 256m (allkeys-lru: queue keys evictable), pgbouncer 128m | Sidecar needs its own limit and its own Redis keys with persistence considered (see 8) |

---
## 1. Channel abstraction
`app/channels/` (new):
- `base.py`: `InboundMessage` (provider_message_id, chat_ref, sender_ref, kind text|image|audio|other, text, media_ref, received_at, raw_hash), `OutboundMessage` (chat_ref, kind, text|media, reply_to), `ChannelStatus` enum, `ChannelAdapter` protocol:
  - `verify_and_parse(request) -> list[InboundMessage | StatusEvent]` (inbound normalization, signature check)
  - `send(connection, OutboundMessage) -> SendResult`
  - `status(connection) -> ChannelStatus`
  - `fetch_media(connection, media_ref) -> bytes`
  - `capabilities` (`rich_buttons`, `templates_required_outside_window`, `max_text_len`, `media_kinds`)
- `registry.py`: `provider -> adapter` (`linked_device`, `cloud_api`).
- `linked_device.py` (A): HTTP client to the sidecar (HMAC-signed, internal network only); capabilities: **no rich buttons, text/image only, reply-only enforced here**.
- `cloud_api.py` (B): Graph API client; interactive buttons (<=3) / list messages available; template send required outside the 24 h window.
- `render.py`: structured `message.meta` -> plain text (section 5).
- `identity.py`: contact key = peppered HMAC of E.164 when known, else of the opaque id (the spike showed the sender id can be a non-phone id); store both when learned.

## 2. Data model (one Alembic revision, `SET LOCAL lock_timeout='5s'` on hot tables like `n3_first_responder.py`)
| Table | Columns (all tenant-scoped, `tenant_id` FK CASCADE, indexed) |
|---|---|
| `whatsapp_connections` | `id`, `tenant_id`, `provider` enum(`linked_device`,`cloud_api`), `status` enum(`pairing`,`connecting`,`connected`,`reconnecting`,`logged_out`,`disabled`,`failed`), `label`, `phone_e164` (EncryptedString), `phone_hmac` (peppered), `display_name`, `sidecar_session_key` (opaque, unique), `waba_id`/`phone_number_id` (B), `access_token` (B, EncryptedString), `consent_version`, `consent_accepted_by`, `consent_accepted_at`, `ai_enabled` (default true), `first_responder` (nullable override), `last_connected_at`, `last_disconnect_at`, `last_disconnect_reason`, `last_alert_at`; **UNIQUE(tenant_id, phone_hmac)** and a global unique on `phone_hmac` (one number, one tenant) |
| `whatsapp_chats` | `id`, `tenant_id`, `connection_id`, `contact_hmac`, `contact_ref` (EncryptedString, jid/wa_id), `visitor_id`, `session_id`, `last_inbound_at` (24 h window), `opted_out`, `pending_options` (JSON: numbered-menu / rating-token state + expiry); UNIQUE(`connection_id`, `contact_hmac`) |
| `whatsapp_inbound_messages` | `connection_id`, `provider_message_id`, `received_at`; **UNIQUE(connection_id, provider_message_id)** = dedupe; 7-day purge cron |
| `whatsapp_auth_state` | **sidecar-owned**, separate DB role: `connection_id`, `key_type`, `key_id`, `ciphertext` bytea (AES-256-GCM, per-connection key = HKDF(`WHATSAPP_AUTH_MASTER_KEY`, connection_id)); batched writes. The spike's file store wrote 7,661 files in 10 minutes |
| Existing tables | `ConversationSource.WHATSAPP`, `VisitorSource.WHATSAPP`, `SourceMeta` fields; `tenant_capabilities.can_use_whatsapp` (bool, default false) + `max_whatsapp_numbers` (int, default 0) |

## 3. Endpoints (tenant admin only: `get_current_tenant_admin`, always scoped to `current_user.tenant_id`; unknown id -> 404, never 403)
| Method + path | Behaviour |
|---|---|
| `POST /whatsapp/connections` | body `{provider, phone_number, label, consent:{version, accepted:true}}`; checks `can_use_whatsapp`, `max_whatsapp_numbers`, number format and global uniqueness; creates row (`pairing`), calls sidecar, returns `201 {id, status, pairing_code, expires_at}`. **Pairing code is returned once and never stored or logged.** Consent must match the current text version, else 422 |
| `POST /whatsapp/connections/{id}/pairing-code` | regenerate while `pairing`/`logged_out`; rate-limited (3/10 min) |
| `GET /whatsapp/connections`, `GET /whatsapp/connections/{id}` | status for polling (`status`, `last_connected_at`, `last_disconnect_reason`, `messages_today`) |
| `PATCH /whatsapp/connections/{id}` | `ai_enabled`, `first_responder`, `label` |
| `DELETE /whatsapp/connections/{id}` | logout via sidecar, wipe auth rows, soft-delete connection, keep conversations |
| `POST /webhooks/whatsapp/linked` | sidecar events; HMAC-SHA256 over `ts.rawbody`, 5-min replay window, `compare_digest`, two secrets accepted for rotation, tenant resolved **only** from `sidecar_session_key` server-side, 200 fast + enqueue |
| `GET/POST /webhooks/whatsapp/cloud` (B) | Meta verify challenge + `X-Hub-Signature-256` (HMAC-SHA256 with app secret) |
| `POST /whatsapp/embedded-signup/callback` (B) | code exchange (30 s validity) -> business token -> `subscribed_apps` -> `register` with PIN |
| Internal sidecar API | `GET /internal/whatsapp/connections?shard=` (restore list), authenticated by service token, network-restricted |

## 4. Pipeline bridge (inbound)
`process_whatsapp_inbound(connection_id, event)` (arq):
1. Dedupe on `whatsapp_inbound_messages` (idempotent insert). Skip groups/status/own messages.
2. Resolve/create `whatsapp_chats` + visitor (peppered key; new `get_visitor_by_contact`), mark `last_inbound_at`.
3. Opt-out keyword (STOP) -> set `opted_out`, send one acknowledgement, stop AI.
4. **Session rotation:** reuse the chat's session unless idle > 24 h or resolved, then create a new `ConversationSession(source='whatsapp', source_meta={channel_connection_id, provider})`. Aligns with Meta's 24 h window and makes the existing monthly meter meaningful.
5. Image -> `fetch_media` -> bytes -> refactored `_process_image_upload` (moderation fail-closed) -> `attachment_ids`. Audio/video/doc -> polite fallback text ("send text or an image").
6. Build `MessageRequest(session_id, tenant_id, user_identifier, message, attachment_ids, metadata={entry_point:'whatsapp', ...})`, set the channel ContextVar, call `send_message`.
7. Catch `HTTPException 402` (plan/trial limit): store the inbound, **send nothing** to the customer, alert the tenant (debounced). Escalated session: AI skipped by the pipeline; the existing `_fanout_customer_message` shows the message to agents.
8. `connection.ai_enabled == false` or resolved first responder `human`: store + notify agents, no AI (the pipeline's human-first branch already does this).
9. Render reply (section 5) -> enqueue `send_whatsapp_message`.

## 5. Widget-only structured messages need text equivalents (largest hidden cost)
| Widget feature | Source | WhatsApp text equivalent (A: text only; B may use interactive buttons) | State needed |
|---|---|---|---|
| Flow router / flow node buttons | `meta.buttons`, `meta.button_actions` (`chat.py:1493-1503`, `:1879-1891`); input token `__flow_start__:<uuid>` in `metadata.button_value` | Numbered list: "Reply 1 for X, 2 for Y". Digit reply -> `button_value`. Entry word `start`/`menu` -> literal `start` | `pending_options` with 10-min expiry |
| Rich flow node output | `meta.flow_output` | **Per-node review required** (node types not enumerated here); default: render node text only | |
| Contact capture | `meta.contact_capture_required` (`_build_contact_capture_prompt:531`) | Already plain text; works as is | |
| Lead capture | silent server side (`:2851-2924`; `source` hard-coded `"chat_widget"` at `:2883`) | Nothing to render; pass the real channel into the lead source | |
| Meeting picker | `meta.offer_meeting` (`:2970`); booking via `meetings.py:50,74` | Phase 1: text with the hosted booking link if one exists, else "A teammate will confirm a time" + ticket. Native slot picking by chat in phase 2 | open question: is there a public booking page URL? |
| Quote card | `meta.quote` (`:2976-3021`) | Line items + total as text; quote row/notification already created server side | |
| Rating prompt | system notice `meta.rating_token` (`rating_request_service.py:~330`) | "Rate us 1-5"; digit reply -> `POST /reviews` with the token | `pending_options.rating` |
| Pre-chat name/email gate | widget-only config (`collect_*_before_chat`), not server enforced | Skipped; the server-side contact-capture-before-escalation flow covers it | |
| Control tokens | `start`, `__talk_to_human__`, `__rag_chat__` (`chat.py:~139-146`) | Keywords: "menu", "human"/"agent", mapped before the call | |
| Typing / presence | WS frames | Composing presence by the sidecar before send | |
| Attachments out (agent images) | `agent_message` frame | Image send (A: sidecar; B: media upload) | |
Estimate for this section alone: **~2.5 agent-days** (renderer, state machine, tests).

## 6. Outbound, pacing, metering, first responder
- **Hook:** `channel_dispatch.maybe_forward(session_id, frame)` called inside `fanout_event` when `to_customer=True`; looks up (Redis-cached) whether the session is a WhatsApp chat; enqueues `send_whatsapp_message`. Frame types forwarded: `agent_message`, `agent_joined`, `ticket_resolved`, `ticket_closed`, `ticket_missed`, rating request. AI replies are enqueued by the inbound worker. The WebSocket-only bypass paths are not WhatsApp-relevant but get a test.
- **Sender job:** per-connection token bucket in Redis (spike default 6/min + daily cap, tenant-configurable down only), serialised per chat, jitter, composing delay scaled by length, retry with backoff on transient failure, **hard stop** (no retry) when status is not `connected`, 24 h window check for B (template fallback), reply-only check for A (chat must have `last_inbound_at`).
- **Metering:** a WhatsApp conversation = one `ConversationSession` (rotated per section 4) and counts against `max_conversations_per_month` like web. No separate meter in phase 1. Limit hit (402) = silent store + tenant alert, never an error text to the customer. Option B additionally shows the tenant their Meta message fees (not metered by Luran).
- **First responder:** `resolve_first_responder(tenant_ai_config, session)` reads `source_meta.channel_connection_id` -> connection override, else tenant value (single change at `chat.py:1269`). Default for new WhatsApp connections: `ai_with_handoff`.

## 7. Plan gating
Phase 1 (beta): `can_use_whatsapp` + `max_whatsapp_numbers` on **`tenant_capabilities` only** (grant-only, same pattern as voice), set via `PUT /platform/tenants/{id}/capabilities` (add both to `TenantCapabilitiesUpdate`, which is `extra="forbid"`). Phase 2: tier-driven columns on `subscription_plans` and the sync in `subscription_service.py:63`. **Proposed (Adebayo to confirm): Lite 1, Professional 2, Growth 5 numbers; Trial 0; Enterprise unlimited.** Beta access is a platform-admin allowlist behind a global feature flag and a kill switch.

## 8. Sidecar (new service `whatsapp-sidecar/`, promoted from `spikes/whatsapp/`)
- Multi-session manager (one Baileys socket per `sidecar_session_key`), `npm ci --ignore-scripts`, exact pin 7.0.0-rc14, Node 20 slim image, no published port (compose network only).
- Postgres auth store (table above, encrypted, batched), `makeCacheableSignalKeyStore`, `syncFullHistory=false`.
- **Single-writer lease** per connection in Redis (`wa:lease:{id}` with TTL + heartbeat). Two processes on the same creds produce a 401 "conflict" that *looks like an unlink* (observed): a duplicate deploy would log tenants out.
- **Restore on boot:** fetch the connection list, reconnect sessions staggered (e.g. 1 per 2 s + jitter; measured 3.0 s per session alone), handle 515 restart, backoff on other closes, no re-pair, mark `reconnecting`. SIGTERM: stop accepting sends, flush creds, close. `cd.yml` recreates containers on every deploy, so this runs every deploy.
- Events out (signed): `session.status`, `message.inbound`, `message.status`. Status mapping: 401/`loggedOut` -> `logged_out` (wipe creds), 440/conflict -> `logged_out` after lease check, 515 -> reconnect, other -> `reconnecting`.
- Compose: new service with `mem_limit` (512m for demo; beta value only after the soak), `restart: unless-stopped`, same logging block. Redis persistence caveat: `redis` is `allkeys-lru` at 256mb, so lease/limiter keys can be evicted: use a prefix and fail closed on a missing lease.
- CI: new job `whatsapp-sidecar` (`npm ci --ignore-scripts && npm test && npm audit signatures`) in `ci.yml`.
- Disconnect alerts: on `logged_out`/`failed`, flip the row, enqueue `send_whatsapp_disconnect_alert` (email to tenant admins + in-app; debounce 1/6 h; not for deliberate disconnects).

## 9. Dashboard (`luran-dashboard`, separate repo + PR)
Channels -> WhatsApp: (1) not connected card with option A/B explainer; (2) connect form: dedicated-number guidance, **risk consent checkbox with version-stamped text** (unofficial, possible suspension, reply-only, Luran not liable for the number); (3) code screen: large grouped code, 3-minute countdown, the three phone steps (WhatsApp > Linked devices > Link a device > *Link with phone number instead*), regenerate; polls `GET /whatsapp/connections/{id}` every 2 s while `pairing`, 15 s after; (4) connected view: status pill, last seen, AI on/off, first responder, rate-cap info, disconnect; (5) logged-out banner with reconnect; (6) plan-gate upsell. FE contract goes in `docs/fe-integration-guide-whatsapp-channel.md` from **captured live responses** (workspace rule), including the "pairing code returned once" and "status `logged_out` needs a new code" UX instructions.

## 10. Tests (CI parity)
Unit/integration at `tests/channels/`, `tests/api/v1/test_whatsapp_*`, `tests/migrations/test_whatsapp_channel.py`: signature (valid/tamper/stale/wrong secret/rotation), dedupe on replay, cross-tenant isolation (tenant A cannot read/patch/delete B; webhook for an unknown session key is dropped), consent version enforcement, capability and count gating, 402 path, reply-only guard, rate cap, rotation, renderer table, fanout hook fires once with two workers, `source='whatsapp'` actually persisted. Before every push run exactly what CI runs through Poetry: `poetry run ruff check app/`, `poetry run black --check app/`, `poetry run pytest tests/` (confirm the Black version equals `poetry.lock`), plus manual `alembic upgrade head` / `downgrade -1` and `alembic heads` = 1.

## 11. Task breakdown, effort, parallelism
Effort = agent-days (Sonnet implements; incl. tests). Model: Sonnet implements and does per-task review; **one Opus review** at the end of Wave 2 for the security-critical parts only (webhook signature, credential storage, tenant isolation, lease), plus one final Opus pass per phase.

| # | Task | Effort | Depends | Track |
|---|---|---|---|---|
| T1 | Channel abstraction + types + registry | 0.5 | - | B |
| T2 | Migration + models + enums + capability columns (database-ops review) | 1.0 | - | B |
| T3 | Sidecar: multi-session, PG encrypted store, lease, restore, signed events, Dockerfile, compose, CI job | 3.0 | contract | A |
| T4 | Connection endpoints + consent + gating + pairing code handling | 1.5 | T1,T2 | B |
| T5 | Inbound webhook + dedupe + arq bridge + session rotation + 402 | 2.0 | T1,T2 | B |
| T6 | Renderer + pending-options state machine | 2.5 | T5 | B |
| T7 | Outbound sender, pacing, `fanout_event` hook, AI reply path | 2.0 | T5 | B |
| T8 | Media in/out (bytes upload refactor, fallbacks) | 1.0 | T5 | B |
| T9 | Status machine, disconnect alerts, kill switch, first-responder override, peppered identity | 1.5 | T2,T5 | B |
| T10 | Dashboard Channels -> WhatsApp (other repo) | 2.5 | API contract | C |
| T11 | Live-captured FE guide, SOP, checklist update | 1.0 | T4-T9 | C |
| T12 | Soak: N-session RAM, 24 h stability, deploy-restore drill | 1.0 | T3 | A |
| T13 | Opus security review + fixes | 1.0 | T3-T7 | review |
| T14 | **Option B adapter** (Cloud API client, Embedded Signup callback, webhook verify, templates, window): starts only after Meta Tech Provider approval | 4.0 | approval | later |
Total beta (T1-T13) ~ 20.5 agent-days; with 3 parallel tracks this is the 5-7 working days below.

### 2-day staging demo (one tenant, granted by hand, text only)
Day 1: contract freeze (webhook event schema + connection API) in the morning; T1, T2 (minimal), T3-core (single process, multi-session map, PG store, signed events), T4 (create/status/delete, no plan gating), T5 (text only, no rotation). Day 2: T7 text-only (fixed cap, delay), T9 status-only, minimal dashboard page (or the curl script) showing number -> code -> connected -> live reply, demo script and rollback. **Out of the demo:** media, renderer beyond plain text, alerts, plan tiers, lease, restore drill, B.
Limits to state out loud: one process, no soak, no HA, reply-only text, consent text draft.

### 5-7 day flagged beta (3 tenants on an allowlist)
Everything T1-T13. Suggested waves:
- Wave 1 (days 1-3): Track A T3; Track B T1,T2 -> T4,T5; Track C T10 against the frozen contract.
- Wave 2 (days 3-6): Track B T6,T7,T8,T9; Track A T12; Track C T11.
- Wave 3 (days 6-7): T13 Opus review + fixes, CI-parity run, staging deploy, live captures.
**Max 3 agents at once.** T6/T7/T8 touch different files (renderer, sender/hook, media) but all touch `chat.py` or `core/websocket.py` lightly: sequence the `chat.py` edits (T5 first, T8 second) to avoid conflicts.

## 12. Risks (ranked)
1. **WhatsApp bans/restricts numbers** (option A, ToS). Mitigations in `docs/sop/whatsapp_pairing_spike.md`; consent + dedicated number + reply-only; can't be fully removed.
2. **Capacity unknown** beyond one session (RAM per extra session not measured); VPS size unknown. Soak is T12; do not promise numbers before it.
3. **Duplicate-process logout** (observed conflict signature): lease is mandatory.
4. **Supply chain** of an unofficial library (rc, new Rust/WASM dep): pinned lockfile, `--ignore-scripts`, provenance check per bump, egress-restricted container, no secrets beyond its own key.
5. **Structured-message parity** (section 5) is the biggest scope risk and the most likely schedule slip.
6. **Identity**: sender ids may be opaque; the contact model must not assume E.164.
7. **Meta AI-provider terms (4.7)** and USD-only billing for option B; see the memo.

## 13. Decisions needed from Adebayo
(a) Confirm plan numbers (Lite 1 / Pro 2 / Growth 5) and that beta is platform-admin grant only; (b) VPS RAM so the sidecar limit can be set; (c) approve the ToS-risk consent wording; (d) public booking page URL for the meeting equivalent; (e) go/no-go for the 2-day demo.

## Self-review
Spec coverage: abstraction, tables + migration, endpoints, signed webhook + dedupe, outbound arq + throttling, restore on deploy, disconnect alerts, dashboard, first responder, meter, gating, tests, SOP/checklist/FE guide: all present. Known soft spots: flow node types not enumerated (flagged), booking URL unknown, multi-session RAM unmeasured, Alembic head not confirmed with the tool.
