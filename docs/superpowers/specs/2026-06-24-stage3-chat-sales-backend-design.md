# Stage 3 — AI Sales Agent · Slice 2 "Chat-Widget Sales: Intent + Conversational Lead Capture" — Backend Design

**Date:** 2026-06-24
**Repo:** `luran` (FastAPI · SQLAlchemy async · Pydantic · pytest · OpenAI)
**Paired FE spec:** `luran-chat/docs/superpowers/specs/2026-06-24-stage3-chat-sales-frontend-design.md`
**Builds on:** Slice 1 "Leads Core" (branch `feat/stage3-leads-core-backend` / PR #31 → develop) — `Lead`, `LeadService`, `app/sales/scoring.py`, `TenantSalesAgentConfig`.
**PRD:** `docs/luran_prd.md` §3.1 (Intent Detection), §3.2 (Lead Capture Flow), §3.3 (Lead Scoring), §6.2 (intent tech), acceptance §2.3.
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Slice 2 of Stage 3 makes the **customer-facing chat agent detect buying intent and capture qualified leads conversationally**, feeding the Slice 1 leads pipeline. Per the locked slice order: 1 Leads core ✅ → **2 Chat-widget sales (this)** → 3 Meeting booking → 4 Quotation → 5 Notifications+CRM.

**In scope (BE):**
- **Piggyback intent + signal extraction**: the main chat-response LLM call emits structured JSON `{reply, intent, signals, captured}` when the tenant's Sales Agent is enabled — no separate classifier call (satisfies PRD acceptance "intent classified on every message" §283).
- **Sales directive** in the system prompt driving the PRD §3.2 flow: answer → one qualifying question → request name/email conversationally ("no forms").
- **Anonymous-on-intent lead creation + enrichment**: create an anonymous `Lead` when a trigger intent fires; enrich the *same* lead (name/email/company/team_size/industry + signals) and re-score each turn.
- **Response contract**: chat `meta` gains `lead_captured` when email is first captured (drives the FE confirmation chip).

**Out of scope (later slices):** meeting picker/booking (3), quotation (4), in-app/Slack/CRM notifications (5), CHURN_RISK retention + OBJECTION handling flows. Lead `meeting_id`/`crm_id` stay null.

**Constraint (PRD acceptance §291):** *Sales Agent does not interrupt pure support flows* — when Sales Agent is off, or intent is `SUPPORT`, behavior is the existing plain-text path, unchanged.

---

## 2. Intent labels & triggers (PRD §3.1)

Intent ∈ `SUPPORT | PRICING_INQUIRY | ENTERPRISE_PROSPECT | DEMO_REQUEST | PURCHASE_INTENT | OBJECTION | CHURN_RISK`.

**Trigger intents for lead capture** = the tenant's `TenantSalesAgentConfig.trigger_intents`. `SUPPORT`/`OBJECTION`/`CHURN_RISK` never create a sales lead in this slice.

**Label reconciliation (required — cross-slice contract):** the classifier MUST emit the PRD §3.1 labels, and `trigger_intents` MUST contain values from that same set, or triggering silently never matches. Slice 1 shipped `TenantSalesAgentConfig.trigger_intents` defaults as `["PRICING_INQUIRY","PRODUCT_DEMO_REQUEST","LEAD_QUALIFICATION"]` — which do **not** match the PRD enum. Slice 2 standardizes on the PRD labels and **updates the Slice 1 config defaults** to `["PRICING_INQUIRY","DEMO_REQUEST","PURCHASE_INTENT","ENTERPRISE_PROSPECT"]` (Slice 1 is not yet in prod, so changing the default is low-cost; any existing rows are handled by mapping legacy values: `PRODUCT_DEMO_REQUEST→DEMO_REQUEST`, `LEAD_QUALIFICATION→PURCHASE_INTENT`). Define the canonical labels as a server-side enum/constant `SALES_INTENTS` reused by the prompt schema, the trigger check, and the config default.

---

## 3. Piggyback structured generation

`app/services/rag_service.py` — add a **sales-mode generation path** alongside the existing `generate_ai_response`.

- **Activation:** only when `TenantSalesAgentConfig.agent_enabled and lead_capture_enabled` for the tenant. Otherwise the existing plain-text generation runs untouched.
- **Structured output:** use OpenAI JSON/structured output (response_format) on `gpt-4o-mini` (PRD §1590). Schema:
  ```
  { "reply": str,
    "intent": one of SALES_INTENTS,
    "signals": { "asked_features": bool, "requested_demo": bool,
                 "mentioned_competitor": bool, "ready_to_buy": bool },
    "captured": { "name": str|null, "email": str|null, "company": str|null,
                  "team_size": str|null, "industry": str|null } }
  ```
- **Sales directive** (system-prompt addendum, sales mode only): instruct the model to — on a trigger intent — (1) answer the question using the normal grounded knowledge, (2) ask exactly ONE qualifying question drawn from `config.qualification_questions`, (3) once the visitor has answered, request name + email conversationally ("no forms — just reply here"), (4) never push sales on a `SUPPORT` intent, (5) extract into `captured`/`signals` only what the visitor actually stated (no hallucinated fields). Mirror the PRD §3.2 example conversation tone.
- **`reply`** is the only visitor-visible text — identical UX to today. `intent`/`signals`/`captured` are backend-internal.
- **Robustness:** if structured parsing fails or the model errors, fall back to the plain reply (use `reply` if present, else the raw text) and skip lead mutation for that turn — never break the chat. Reuse the existing LLM error/rate-limit handling.

---

## 4. Lead create / enrich / score

New `LeadService` method(s) (`app/services/lead_service.py`):
- `get_or_create_by_conversation(tenant_id, conversation_id, *, intent, source="chat_widget", utm) -> Lead` — returns the existing lead for the conversation, or creates an anonymous one (status `NEW`) when none exists.
- `enrich_from_chat(lead, *, intent, captured, signals) -> Lead` — sets any newly-provided `captured` fields (validate email; ignore null/empty; never overwrite a non-empty field with null), updates `intent`, then calls `apply_score(lead, signals=signals)` and persists. Returns the lead.

**Orchestration** in the chat message handler (`app/api/v1/endpoints/chat.py`), after sales-mode generation:
1. If not sales mode → skip entirely (plain path).
2. Resolve lead: `get_or_create_by_conversation(...)` **only if** a lead already exists for the conversation OR `intent ∈ trigger_intents`. (A `SUPPORT` first message with no prior lead creates nothing.)
3. `enrich_from_chat(lead, intent, captured, signals)` — fills fields, re-scores.
4. If this turn captured an email that wasn't set before → set `response.meta["lead_captured"] = {"name": lead.name, "email": lead.email}`.

**Scoring** reuses Slice 1 unchanged: field-based signals (email/company/team>50/enterprise/paid_ad) derive from the lead; the four conversation-derived signals come straight from `signals`. `source="chat_widget"`; `utm` from the visitor/session (reuse the metadata resolution already in `chat.py`). `conversation_id` = the chat `session_id`.

---

## 5. Response contract

Chat response `meta` (in `ChatResponse`/`MessageResponse.meta`) gains an optional:
```
"lead_captured": { "name": str|null, "email": str } | absent
```
Present only on the turn email is first captured. Everything else in the response is unchanged. Intent/signals are NOT surfaced to the widget in this slice (kept internal; analytics may read them from the lead).

---

## 6. Safety & invariants

- Sales mutation gated by `agent_enabled and lead_capture_enabled`; intent gating by `trigger_intents`.
- One lead per conversation (idempotent via `conversation_id` lookup).
- Email format validated before persist (reuse existing validator); invalid email → not stored, no chip.
- Tenant-scoped throughout (lead carries `tenant_id` from the session).
- `SUPPORT` never creates/enriches a sales lead; `CHURN_RISK`/`OBJECTION` out of scope (no sales action).
- Idempotent + side-effect-free on LLM failure (plain reply, no lead mutation).
- No PII beyond what Slice 1 already stores (encrypted name/email).

---

## 7. Testing (pytest)

- **Structured generation** (`tests/sales/test_chat_sales_generation.py`): valid JSON parsed into `{reply,intent,signals,captured}`; malformed/empty → graceful fallback to plain reply, no lead mutation; sales-mode-off uses the plain path (no structured call).
- **Lead capture** (`tests/api/v1/test_chat_lead_capture.py`): trigger intent with no prior lead → anonymous Lead created (conversation_id, source=chat_widget, status NEW, low score); subsequent turn with `captured` email/company/team_size → SAME lead enriched + re-scored higher; `meta.lead_captured` set the turn email arrives; `SUPPORT` intent → no lead; sales-mode-off → no lead; cross-tenant isolation; invalid email not stored.
- **Scoring integration**: signals from `signals` fire the right conversation-derived weights via `apply_score`; field signals derive correctly after enrichment.
- Green `poetry run pytest`; no regression to existing chat/support tests (the plain path must be untouched when sales is off).

---

## 8. Files

**New:** `app/sales/intent.py` (SALES_INTENTS enum/constants + the structured schema + sales-directive prompt builder), `tests/sales/test_chat_sales_generation.py`, `tests/api/v1/test_chat_lead_capture.py`.
**Modify:** `app/services/rag_service.py` (sales-mode structured generation path), `app/services/lead_service.py` (`get_or_create_by_conversation`, `enrich_from_chat`), `app/api/v1/endpoints/chat.py` (sales orchestration after generation; set `meta.lead_captured`), `app/schemas/conversation.py` (document `meta.lead_captured`).

No new DB columns or migrations — Slice 1's `Lead` already has `conversation_id`, `intent`, `source`, `utm`, `score_breakdown`.

---

## 9. Acceptance (maps to PRD §2.3)

1. Intent is classified on every incoming message in sales mode (piggyback field).
2. Lead capture triggers for each configured trigger intent; not for `SUPPORT`.
3. An anonymous lead appears in the Slice 1 inbox as soon as buying intent is detected; it enriches (name/email/company/team_size) and re-scores as the conversation proceeds — one lead per conversation, linked by `conversation_id`.
4. Lead score is calculated from both field and conversation-derived signals and updates live.
5. `meta.lead_captured` is returned the turn email is captured (FE chip).
6. Sales Agent does not interrupt pure support flows; sales-mode-off path is byte-for-byte the existing behavior.
7. LLM/parse failures degrade to a plain reply with no lead side effects. All new tests green; existing suite green.
