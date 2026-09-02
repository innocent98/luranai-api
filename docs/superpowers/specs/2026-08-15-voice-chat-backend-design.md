# Voice Chat (in-widget) — Backend Design

> **Status:** Design approved 2026-08-15. Feeds `writing-plans`. **Source of truth** for the
> ElevenLabs Agent configuration + the endpoint/tool contract consumed by the widget
> (`luran-widget/docs/superpowers/specs/2026-08-15-voice-chat-widget-design.md`).

**Goal:** Let a customer *speak* to the Luran assistant inside the existing chat widget and hear it
reply in audio — with **full parity** to text chat (same KB grounding, intent, flows, lead scoring,
and AI actions), and a clean escalation to a human.

**Architecture:** ElevenLabs Agents runs the real-time voice loop (STT → turn-taking/barge-in → TTS)
over **WebRTC in the widget**; its "custom LLM" is pointed at **Luran's own chat pipeline** exposed as
an OpenAI-compatible streaming endpoint. Voice therefore reuses the existing brain, action engine, and
escalation path unchanged. One shared ElevenLabs Agent; the tenant + Luran conversation session are
passed as context so voice continues the *same* conversation as text.

**Tech stack:** FastAPI + SSE streaming, existing chat pipeline + action-call engine, ElevenLabs Agents
(custom-LLM + server/webhook tools + WebRTC token API), Redis for concurrency/usage counters.

---

## Global Constraints
- **API version:** additive, stays on `/api/v1` (new `/api/v1/voice/*`). Per `project_api_versioning_decision`.
- **Full parity by construction:** the reasoning is Luran's pipeline — never a second brain. Voice-specific
  code is limited to the streaming adapter, the write-action tool shim, buffer words, and handoff-to-text.
- **Tenant isolation:** the custom-LLM endpoint and every tool webhook resolve tenant from signed session
  context and scope all data by `tenant_id`.
- **No telephony.** In-widget WebRTC only. No phone numbers / SIP / Twilio in v1.
- **Respect confirmation policy:** confirmation-before-write stays in the pipeline's flow logic (same rule
  as text) — ElevenLabs tools are invoked only *after* the pipeline confirms.
- **No Claude co-author trailer** on commits. Grounded in `docs/luran_prd.md` where applicable
  (`feedback_always_refer_prd`) — note this feature is net-new, not a PRD stage.

---

## 1. Scope

### In scope (Phase 1 / v1)
- In-widget voice conversation (audio in / audio out), **text-out configurable** per tenant/session.
- Full-parity brain via custom-LLM endpoint; read actions + reasoning in-pipeline.
- Slow **write** actions (booking, quotes, CRM writes) as ElevenLabs async tools → Luran action engine.
- Voice→human escalation that **converts to text** and rings the caller app (reuses the escalation path;
  see `project_agent_caller_app`).
- Per-tenant **feature flag** + monthly **minute cap** + concurrency guard, enforced at token mint.

### Out of scope (v1)
- **Live-audio human bridge** (human speaks on the call) — that's Phase 3 and needs custom WebRTC/SFU work
  ElevenLabs does not provide in-widget.
- Telephony / phone numbers; outbound voice; multi-language tuning beyond EL defaults; per-tenant custom
  voices beyond a simple voice-id selection.
- Formal subscription-tier billing integration for voice minutes (fast-follow).

---

## 2. Key decisions

| Decision | Choice |
|---|---|
| Brain | **Option C** — EL voice I/O, Luran pipeline as the custom LLM |
| Agent topology | **One** EL Agent; tenant + Luran `session_id` passed as conversation context |
| Conversation continuity | Voice attaches to the **existing Luran conversation session** — unified history, seamless handoff-to-text |
| Actions | **Hybrid** — reasoning + reads in the pipeline; slow writes declared as EL async tools → action-engine webhook |
| Handoff | **Convert to text** via a client tool that fires the escalation pipeline + caller-app ring; EL session ends |
| Gating | **Per-tenant flag + monthly minute cap + concurrency guard**, enforced **at token mint** |
| Dead-air | Buffer words for in-pipeline latency; EL tool-call sounds for the write tools |

---

## 3. The custom-LLM endpoint (the core new component)

`POST /api/v1/voice/llm/chat/completions` — an **OpenAI-compatible, SSE-streaming** endpoint that wraps
Luran's existing chat pipeline.

- **Request** (sent by ElevenLabs): standard Chat Completions body — `messages`, `model`, `tools`
  (the write-action tools EL has declared), `stream: true`, plus an `elevenlabs_extra_body` /
  dynamic-variable passthrough carrying **`luran_session_id`** and **`tenant_id`** (set at token mint).
- **Behavior:** resolve tenant + Luran session → run the **existing pipeline** (intent → RAG/KB → read
  actions → flow logic) → **stream** the spoken reply as OpenAI `data: {chunk}\n\n … data: [DONE]`.
- **Latency:** emit **buffer words first** — the streamed reply starts with a short phrase ending in
  `"... "` (ellipsis + trailing space; the trailing space is required) so TTS has prosody to speak while
  the completion generates.
- **Write actions:** when the pipeline (post-confirmation) needs a slow write, the stream emits a standard
  `type:"function"` **tool call** for the corresponding EL-declared tool (§4) instead of speaking the
  result inline; EL executes it and re-invokes this endpoint with the tool result to produce the spoken
  confirmation.
- **Auth:** ElevenLabs → Luran is authenticated by a **shared secret / signed header** (validate the exact
  EL custom-endpoint auth contract in **Spike 1**). Reject unsigned calls. No tenant data without a valid
  signed session context.
- **Streaming requirement:** the pipeline's final LLM call must stream tokens through this endpoint;
  where a stage can't stream, buffer words bridge the gap.

---

## 4. Write-action tool webhooks

The slow write actions are declared to the ElevenLabs Agent as **server/webhook tools**; each points at:

`POST /api/v1/voice/tools/{tool_name}` → validates the EL signature → invokes **Luran's existing
action-call engine** (idempotent, SSRF-guarded — `project_action_call_review`) → returns the result to EL.

- **Tool inventory (declared up front to EL):** `book_meeting`, `send_quote`, `crm_write` (final set
  derived from the action engine's write actions). Read actions are **not** tools — they run inside the
  pipeline (§3).
- **Idempotency:** each call carries an idempotency key derived from `luran_session_id` + tool + args, so a
  voice network hiccup cannot double-book / double-send. (Reuses the engine's existing idempotency.)
- **Async / dead-air:** declared with EL "tool-call sounds" so audio stays alive during multi-second
  execution; use `expects_response` so the spoken confirmation waits for the real result.
- **No new action code** — these webhooks are thin adapters over the same engine text chat uses.

---

## 5. Token-mint endpoint + gating

`POST /api/v1/voice/session/token` (auth: the widget's existing session/customer context) →
returns a **short-lived EL WebRTC conversation token**.

At mint time it enforces, in order (fail → `409` + `reason`, widget falls back to text):
1. **Tenant flag** — voice enabled for this tenant?
2. **Minute cap** — tenant's monthly voice minutes remaining? (Redis counter.)
3. **Concurrency** — under the tenant's / platform's simultaneous-session ceiling? (Redis, respects the EL
   plan concurrency cap of ~20 Pro / ~40 Business.)

On success it creates the EL conversation with **metadata/dynamic variables** = `{ tenant_id,
luran_session_id, text_out: bool }`, so both the custom-LLM endpoint and the tools receive them, and voice
threads onto the **same Luran conversation session** as any prior text. Secrets never touch the client —
the token is minted server-side and short-lived.

## 6. Voice usage metering + caps
- Meter minutes per tenant per conversation from EL session lifecycle events (session start/end webhook or
  duration on close).
- Increment a monthly Redis counter (with a durable periodic flush to Postgres for billing/audit).
- Cap breach → future mints refused with a graceful reason; an in-flight session is allowed to finish.
- Reuses Luran's existing plan-limit enforcement pattern (trial/audit caps).

## 7. Escalate client-tool handler
The "talk to a human" path is a **client tool** (EL has no native in-widget human transfer — verified).
When the LLM calls it:
- Fire the **existing escalation pipeline** — `escalate_session` + `Ticket` + email + (new) **caller-app
  ring** (`project_agent_caller_app`), on the *same* session.
- Return a signal the widget uses to **switch voice → text** and end the EL session gracefully.
- The human joins in **text**. (Live audio = Phase 3.)

---

## 8. ElevenLabs Agent configuration (contract)
- **One Agent**, custom LLM URL = `/api/v1/voice/llm/chat/completions`.
- **Tools declared:** the write-action set (§4) as server/webhook tools; the `escalate_to_human` client
  tool; system tool `end_call`.
- **Voice:** a default voice id, optionally overridable per tenant at mint (simple selection only).
- **Turn-taking:** start with "normal" eagerness + barge-in enabled; tune in the spike.
- **Tenant/session context:** passed via conversation metadata/dynamic variables at token mint (§5).

## 9. Security
- EL→Luran (custom-LLM endpoint **and** tool webhooks) authenticated by signed shared secret; reject
  unsigned. Tenant resolved only from the signed session context, never client-supplied alone.
- Client holds only a short-lived EL token; no ElevenLabs API key or Luran secret reaches the browser.
- Tool webhooks inherit the action engine's SSRF guard + idempotency.
- All new secrets (EL API key, signing secret) live in the encrypted `.env` bundles (`scripts/env.sh`).

## 10. Testing
- Custom-LLM endpoint: OpenAI-compatible SSE shape; buffer-word prefix present; tenant/session resolution;
  parity smoke (same input → same decision as the text endpoint).
- Tool webhooks: idempotency (double call → single effect); signature rejection; action-engine reuse.
- Token mint: flag off → 409; cap exhausted → 409; concurrency ceiling → 409; success threads `session_id`.
- Metering: minutes increment + monthly reset; cap breach refuses new mint, lets in-flight finish.
- Escalate tool: fires ticket + caller-app ring on the same session; returns the switch-to-text signal.
- Run in `luran-api:latest` Docker per `project_ci_health`; lint **both** `ruff` and `black`.

## 11. Rollout + spikes
- **Spike 1 (do first):** validate the exact **EL custom-endpoint auth** contract (how EL signs/auths to our
  server) on a throwaway agent.
- **Spike 2:** validate **multi-second write-action dead-air** UX (tool-call sounds) with a real
  booking/quote latency.
- Ship behind the per-tenant flag; pilot with one tenant; Android + desktop browsers.
- Deploy via existing CD (develop → main); add EL secrets to encrypted env first.

## 12. Follow-ups
- **Phase 3 live-audio human bridge** (human speaks on the call) — custom WebRTC/SFU, couples to the caller
  app's audio-join (`project_agent_caller_app`).
- Formal subscription-tier voice-minute billing/overage.
- Per-tenant custom voices / persona; multi-language tuning; barge-in/turn-taking per-tenant profiles.
