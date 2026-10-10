# N3 — "Who answers first" tenant setting (design)

**Status:** APPROVED in brainstorm 2026-10-10 (Adebayo) · **Package:** D (field-test triage 2026-10-10, item N3)
**Repos:** `luran` (BE, feature → `develop`), `luran-dashboard` (feature → `staging`), `luran-chat` (feature → `stage`)
**Related:** triage `docs/handoff/2026-10-10-observations-triage.md` (workspace); PRD `docs/luran_prd.md` — Knowledge Gaps ("questions Luran could not answer") and Resolution/Escalation Rate metrics. The PRD has no explicit first-responder mode; this is new scope.

## 1. Problem

A tenant with an empty knowledge base still gets AI replies. Every turn retrieves 0 chunks, the grounding policy resolves to `no_retrieval_action` (default `clarify`), and the customer receives an endless series of AI clarifying questions; nothing reaches a human unless the customer explicitly asks (staging log "Retrieved 0 chunks"). Separately, when an escalation is **missed** (no agent picks up within `AGENT_ALERT_WAIT_SECONDS`, default 90 s) the customer is told nothing.

## 2. Goals / success criteria

- Tenants explicitly choose who answers first: **AI first**, **AI with human handoff**, or **Human only**.
- A tenant with no active knowledge sources defaults to Human only — visibly, never as a silent runtime switch.
- In Human only, a customer always reaches a person or gets a clear "we'll reply" message; the AI never replies.
- The existing Settings → AI grounding dropdowns remain the single source of truth for AI turns.

**Non-goals:** office hours/scheduling, per-channel modes, routing to a specific agent, AI-drafted replies for agents.

## 3. Decisions (brainstorm 2026-10-10)

| ID | Decision |
|---|---|
| N3-D1 | Human only = instant fixed (non-AI) system reply + ring agents immediately via the existing escalation machinery; on missed, the customer is told the team will reply. (Rejected: AI fallback after timeout; inbox-only queue with no ringing.) |
| N3-D2 | Setting is a **preset** over existing knobs: `ai_with_handoff` writes `escalate` into `no_retrieval_action` + `low_confidence_action`; knobs stay the runtime truth. (Rejected: separate overriding rules — two sources of truth; two-value setting.) |
| N3-D3 | 0-KB default: new tenants start `human`; one-off migration sets `human` for existing tenants with 0 ACTIVE knowledge sources; nudge to switch when KB fills; warning banner when an AI mode has 0 sources. No auto-switch, no email to admins. |
| N3-D4 | Human only also **skips tenant automation flows**. |
| N3-D5 | The missed-ticket customer message applies in **every** mode (fixes today's silence for AI-mode escalations too). |

## 4. Backend (`luran`)

### 4.1 Data
- `TenantAIConfig.first_responder`: `String(20)`, NOT NULL, `server_default='ai'`; allowed `ai | ai_with_handoff | human` (validated by `Literal` in schemas).
- Add to `TenantAIConfigBase`/`TenantAIConfigCreate`/`TenantAIConfigUpdate` (has `extra="forbid"`) /`TenantAIConfigResponse` (`app/schemas/tenant.py`).
- `TenantAIConfigResponse` gains read-only computed `active_knowledge_sources: int` = count of `KnowledgeSource` rows with `tenant_id = T AND status = ACTIVE` (indexed by `ix_knowledge_sources_tenant_status`). PROCESSING / INACTIVE / FAILED do not count.

### 4.2 Migration (one Alembic revision, reversible)
1. Add the column (all existing rows → `ai`, i.e. today's behaviour).
2. Data step: `UPDATE tenant_ai_configs SET first_responder='human' WHERE first_responder='ai' AND NOT EXISTS (SELECT 1 FROM knowledge_sources ks WHERE ks.tenant_id = tenant_ai_configs.tenant_id AND ks.status::text = 'ACTIVE')`. Log the affected row **count only** (no tenant names). Idempotent. Verify the stored enum literal for `SourceStatus` (name vs value) before writing the SQL, as `mt60missedrepair` did for `ticket_status`.
3. Downgrade drops the column.

### 4.3 Defaults on creation
- `tenant_service.create_tenant` (~`tenant_service.py:122`) and `company_registration_service` (~`:118`): set `first_responder='human'` explicitly.
- Lazy creation in the voice settings PUT (`app/api/v1/endpoints/tenant.py` ~`:275`): apply the migration rule (count ACTIVE sources → `human` if 0 else `ai`).

### 4.4 Preset write rules (`PUT /ai-config` → `tenant_service.update_ai_config`)
| `first_responder` saved | `no_retrieval_action` | `low_confidence_action` |
|---|---|---|
| `ai` | as sent / unchanged | as sent / unchanged |
| `ai_with_handoff` | forced `escalate` (overrides any sent value) | forced `escalate` |
| `human` | as sent / unchanged (not read in this mode) | as sent / unchanged |
Switching `ai_with_handoff → ai` leaves both at `escalate`. Admin-only — unchanged existing permission on `PUT /ai-config`.

### 4.5 Chat path (`app/api/v1/endpoints/chat.py`, `_send_message_impl`)
- **Refactor first:** extract the duplicated "escalate session → get-or-create ticket → ring agents → queue escalation emails" sequence (call sites ~`:1317`, `:1468`, `:1682`, `:1887`, `:2030`, `:2713`) into one helper `_escalate_to_human(db, session, reason, …)` returning the ticket. Behaviour-preserving; existing tests must stay green.
- **Human only:** immediately after the already-ESCALATED early return (~`:1191`) and **before** the flow router and RAG:
  1. If `require_name_before_escalation` / `require_email_before_escalation` fields are missing → existing contact-capture prompt flow (`_build_contact_capture_prompt`, `_store_pending_contact_capture`), unchanged; the capture completion path (~`:1887`) then escalates.
  2. Else → `_escalate_to_human(reason="human_first")`; persist the fixed assistant reply **"Thanks for reaching out — connecting you with someone from our team now."** (no LLM call, `token_count=0`); return `ChatResponse(should_escalate=True, ticket_id=…)`.
  3. Subsequent messages hit the existing ESCALATED early return.
  - `__talk_to_human__` token still works (same path).
  - After a ticket is resolved/closed and the session is no longer ESCALATED, the customer's next message re-enters this branch → `_escalate_to_human` reuses/reopens or creates a ticket per the existing `_get_or_create_escalation_ticket` rules (no special case).
- **AI with handoff:** no runtime change — existing `policy_action == "escalate"` → `OFFER_ESCALATION` (asks consent).

### 4.6 Missed-ticket customer message (all modes)
- In `alert_escalation_fallback` (`app/workers/agent_alerts.py`), only when the conditional UPDATE actually flips the ticket to MISSED (rowcount 1): persist an assistant message on the ticket's session — **"Our team couldn't get to you right now. We'll reply right here as soon as we can."** — and fan it out over the existing customer WS message frame.
- **Email clause gate:** append ", and by email at <email>" **only if** the platform already emails the customer when an agent replies while they are offline. The implementation plan's first BE task verifies this; if no such path exists, the clause is omitted (no new email feature in N3).
- Idempotent: a re-run of the fallback after the flip must not post a second message (guarded by the rowcount).

### 4.7 Public widget config
- `get_public_widget_config` (`app/api/v1/endpoints/widget.py:361`) returns `first_responder` (non-sensitive).

### 4.8 Ticket reason
- Human-only tickets carry escalation reason `human_first`; agent surfaces display it as **"Human-only mode"** wherever the reason is shown today.

## 5. Dashboard (`luran-dashboard`, Settings → AI)
- **"Who answers first"** card at the top: three radio cards — AI first ("Luran's AI answers using your knowledge base. Fine-tune how it behaves below."), AI with human handoff ("AI answers what it knows and offers your team whenever it can't."), Human only ("Every conversation goes straight to your team — the AI never replies.").
- **AI first:** existing three dropdowns move under an "Advanced" fold, expanded by default when any differs from defaults (`balanced`/`clarify`/`clarify`).
- **AI with handoff:** dropdowns shown read-only with "Set by AI with human handoff".
- **Human only:** dropdowns hidden; note "Customers are connected to your team immediately. Make sure your agents have the Luran agent app so they get rung." Require-name/email toggles remain.
- **Nudge** (mode `human`, `active_knowledge_sources ≥ 1`): dismissible "Your knowledge base has N active sources — let AI answer first?" with **Switch to AI with handoff** (saves immediately). Same component on the Knowledge Base page.
- **Warning banner** (mode `ai`/`ai_with_handoff`, `active_knowledge_sources = 0`): "Your knowledge base is empty — the AI has nothing to answer from. Add sources or switch to Human only."
- Switching **to** Human only asks one confirm: "The AI will stop replying to all new conversations."
- Non-admins: read-only (as today). Existing design system/components only.
- Agent-side reason label "Human-only mode" where ticket/escalation reason is displayed.

## 6. Customer surfaces (`luran-chat`: widget + authed customer chat)
- Read `first_responder` from public widget config; when `human`, hide the voice button. Greeting unchanged.
- First reply arrives with `should_escalate=true` → existing waiting-for-human state; no new UI.
- Missed message arrives as a normal message frame → no new code expected; verify render.
- **Coordination:** the parallel voice session reworked `MessageBubble.tsx` / `ChatWindow.tsx` (EscalationNotice, luran-chat #19 on `stage`). Build on `stage`; message that session before editing those files.

## 7. Agent app (`luran_agent`)
- Display reason `human_first` as "Human-only mode" where escalation reason is shown. No other change.

## 8. Rollout
1. BE → `develop` (staging migrates; record affected-tenant count from the staging deploy log), dashboard → `staging`, widget → `stage`; live-verify on staging.
2. Before prod: report the prod affected-tenant count from a read-only query Adebayo runs; then promote **BE and dashboard together** (the migration flips 0-KB tenants on deploy; admins need the card to understand/switch), widget right after.
3. Rollback: revert BE (downgrade drops column → all tenants back to AI behaviour); Vercel instant rollback for FEs.

## 9. Testing
- **BE (pytest):** `human` skips flows + RAG and replies with the fixed text; contact capture precedes escalation when required; ticket reason `human_first`; ESCALATED early return on later turns; `ai_with_handoff` preset forced on save even when other values sent; `ai_with_handoff → ai` keeps values; schema rejects unknown modes; `active_knowledge_sources` counts ACTIVE only; creation defaults; migration data step (+ idempotency); missed message posted once (with/without email clause per §4.6 gate); `_escalate_to_human` refactor keeps all existing escalation tests green.
- **Dashboard (Vitest):** render per mode, lock, Advanced fold default, nudge/banner conditions, confirm dialog, PUT body.
- **Widget (Vitest):** voice hidden in `human`; missed message renders.
- **Live (staging) for the FE integration guide:** captures for GET/PUT `/ai-config` (each mode, forced preset), public widget config, human-mode first message, and a real missed ticket (wait out the 90 s timer).
- CI reproduced locally per repo before every push. One Opus review for N3 as a whole; Sonnet implements.

## 10. Open items for the implementation plan
- Verify agent-reply-while-offline customer email exists (gates §4.6 clause).
- Verify `SourceStatus` stored literal for the migration SQL.
- Confirm where the escalation reason is rendered in dashboard + app (exact components).
