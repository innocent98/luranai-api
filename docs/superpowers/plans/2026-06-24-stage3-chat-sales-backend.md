# Stage 3 Slice 2 — Chat-Widget Sales (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chat agent detect buying intent and capture+score leads conversationally, feeding the Slice 1 leads pipeline — without interrupting support flows.

**Architecture:** Piggyback structured output on the main sales reply (one `gpt-4o-mini` call returns `{reply, intent, signals, captured}`); a sales directive drives PRD §3.2 (answer → one qualifying question → request contact). The chat handler creates an anonymous `Lead` on a trigger intent and enriches + re-scores the SAME lead each turn via `LeadService`. No new DB columns.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, Pydantic v2, OpenAI (gpt-4o-mini, JSON structured output), pytest, Poetry.

## Global Constraints

- Builds on Slice 1 (this branch is stacked on `feat/stage3-leads-core-backend`): reuse `Lead`, `LeadService`, `app/sales/scoring.py`, `TenantSalesAgentConfig` unchanged except the config-default label fix in Task 1.
- **Canonical intent labels (PRD §3.1):** `SUPPORT, PRICING_INQUIRY, ENTERPRISE_PROSPECT, DEMO_REQUEST, PURCHASE_INTENT, OBJECTION, CHURN_RISK`. The classifier emits these; `trigger_intents` must use these. Default `trigger_intents = ["PRICING_INQUIRY","DEMO_REQUEST","PURCHASE_INTENT","ENTERPRISE_PROSPECT"]`. Legacy mapping: `PRODUCT_DEMO_REQUEST→DEMO_REQUEST`, `LEAD_QUALIFICATION→PURCHASE_INTENT`.
- Sales mutation gated by `agent_enabled AND lead_capture_enabled`; capture gated by `intent ∈ trigger_intents`. `SUPPORT`/`OBJECTION`/`CHURN_RISK` create no sales lead.
- Sales-mode-OFF path must be byte-for-byte the existing plain-text behavior (PRD acceptance §291: do not interrupt support).
- One lead per conversation (idempotent by `conversation_id`). Email validated before persist. Tenant-scoped throughout.
- LLM/parse failure → graceful fallback to plain reply, no lead side effects.
- Model `gpt-4o-mini` (PRD §1590). Commit messages MUST NOT include a Claude co-author trailer. Tests: `poetry run pytest`.

---

### Task 1: Canonical intent labels + config default reconciliation

**Files:**
- Create: `app/sales/intent.py`
- Modify: `app/db/models/sales_agent_config.py` (default `trigger_intents`)
- Test: `tests/sales/test_intent_labels.py`

**Interfaces:**
- Produces: `SALES_INTENTS` (the 7 canonical labels), `TRIGGER_DEFAULTS: list[str]`, `normalize_intent(value: str) -> str` (maps legacy labels + unknown→`SUPPORT`), `is_trigger_intent(intent: str, trigger_intents: list[str]) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_intent_labels.py
from app.sales.intent import SALES_INTENTS, TRIGGER_DEFAULTS, normalize_intent, is_trigger_intent

def test_canonical_labels():
    assert SALES_INTENTS == {
        "SUPPORT","PRICING_INQUIRY","ENTERPRISE_PROSPECT","DEMO_REQUEST",
        "PURCHASE_INTENT","OBJECTION","CHURN_RISK",
    }

def test_defaults_are_canonical_triggers():
    assert TRIGGER_DEFAULTS == ["PRICING_INQUIRY","DEMO_REQUEST","PURCHASE_INTENT","ENTERPRISE_PROSPECT"]
    assert all(i in SALES_INTENTS for i in TRIGGER_DEFAULTS)

def test_normalize_maps_legacy_and_unknown():
    assert normalize_intent("PRODUCT_DEMO_REQUEST") == "DEMO_REQUEST"
    assert normalize_intent("LEAD_QUALIFICATION") == "PURCHASE_INTENT"
    assert normalize_intent("pricing_inquiry") == "PRICING_INQUIRY"
    assert normalize_intent("garbage") == "SUPPORT"

def test_is_trigger_intent():
    assert is_trigger_intent("DEMO_REQUEST", TRIGGER_DEFAULTS) is True
    assert is_trigger_intent("SUPPORT", TRIGGER_DEFAULTS) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_intent_labels.py -v`
Expected: FAIL (`ModuleNotFoundError: app.sales.intent`).

- [ ] **Step 3: Implement**

```python
# app/sales/intent.py
SALES_INTENTS: set[str] = {
    "SUPPORT", "PRICING_INQUIRY", "ENTERPRISE_PROSPECT", "DEMO_REQUEST",
    "PURCHASE_INTENT", "OBJECTION", "CHURN_RISK",
}
TRIGGER_DEFAULTS: list[str] = ["PRICING_INQUIRY", "DEMO_REQUEST", "PURCHASE_INTENT", "ENTERPRISE_PROSPECT"]
_LEGACY = {"PRODUCT_DEMO_REQUEST": "DEMO_REQUEST", "LEAD_QUALIFICATION": "PURCHASE_INTENT"}

def normalize_intent(value: str | None) -> str:
    if not value:
        return "SUPPORT"
    v = value.strip().upper()
    v = _LEGACY.get(v, v)
    return v if v in SALES_INTENTS else "SUPPORT"

def is_trigger_intent(intent: str, trigger_intents: list[str]) -> bool:
    norm = {normalize_intent(t) for t in (trigger_intents or [])}
    return normalize_intent(intent) in norm
```

In `app/db/models/sales_agent_config.py`, change the `_DEFAULT_TRIGGER_INTENTS` constant to `["PRICING_INQUIRY", "DEMO_REQUEST", "PURCHASE_INTENT", "ENTERPRISE_PROSPECT"]` (import or mirror `TRIGGER_DEFAULTS`).

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_intent_labels.py tests/sales/test_sales_config_model.py -v`
Expected: PASS (label tests + the Slice 1 config model test still green with the new default).

- [ ] **Step 5: Commit**

```bash
git add app/sales/intent.py app/db/models/sales_agent_config.py tests/sales/test_intent_labels.py
git commit -m "feat(sales): canonical intent labels + reconcile trigger_intents defaults"
```

---

### Task 2: Sales structured-output schema + prompt directive

**Files:**
- Modify: `app/sales/intent.py`
- Test: `tests/sales/test_sales_prompt.py`

**Interfaces:**
- Consumes: `SALES_INTENTS` (Task 1).
- Produces:
  - `SALES_RESPONSE_SCHEMA: dict` — the JSON schema for OpenAI structured output (`reply:str, intent:enum, signals:{asked_features,requested_demo,mentioned_competitor,ready_to_buy:bool}, captured:{name,email,company,team_size,industry: str|null}`).
  - `build_sales_directive(qualification_questions: list) -> str` — the system-prompt addendum.
  - `parse_sales_output(raw: str) -> dict | None` — parse+validate model JSON into `{reply, intent, signals, captured}`; return None on malformed.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_sales_prompt.py
from app.sales.intent import SALES_RESPONSE_SCHEMA, build_sales_directive, parse_sales_output

def test_schema_has_required_keys():
    props = SALES_RESPONSE_SCHEMA["properties"]
    assert set(props) == {"reply", "intent", "signals", "captured"}
    assert set(props["signals"]["properties"]) == {
        "asked_features", "requested_demo", "mentioned_competitor", "ready_to_buy"}
    assert set(props["captured"]["properties"]) == {
        "name", "email", "company", "team_size", "industry"}

def test_directive_mentions_one_question_and_no_forms():
    d = build_sales_directive([{"question": "How large is your team?"}])
    assert "one" in d.lower() and "no forms" in d.lower()
    assert "How large is your team?" in d

def test_parse_valid_and_invalid():
    good = '{"reply":"Hi","intent":"PRICING_INQUIRY","signals":{"asked_features":false,"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false},"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null}}'
    out = parse_sales_output(good)
    assert out["intent"] == "PRICING_INQUIRY" and out["reply"] == "Hi"
    assert parse_sales_output("not json") is None
    assert parse_sales_output('{"reply":"x"}') is None  # missing keys
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_sales_prompt.py -v`
Expected: FAIL (names not defined).

- [ ] **Step 3: Implement**

Add to `app/sales/intent.py`: `SALES_RESPONSE_SCHEMA` (a JSON-schema dict with the properties above, `intent` constrained to `sorted(SALES_INTENTS)`, all fields required, `captured`/`signals` sub-objects); `build_sales_directive(qualification_questions)` returning a directive string that instructs: answer normally → ask exactly ONE qualifying question (use the first configured question text, fall back to "What are you hoping to achieve?") → then request name + email conversationally with the literal phrase "no forms — just reply here" → never sell on SUPPORT → extract only stated facts into `captured`/`signals`; `parse_sales_output(raw)` doing `json.loads` in a try/except, validating the four top-level keys + `intent ∈ SALES_INTENTS` (else `normalize_intent`), returning the dict or `None`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_sales_prompt.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/sales/intent.py tests/sales/test_sales_prompt.py
git commit -m "feat(sales): structured-output schema + sales prompt directive + parser"
```

---

### Task 3: Sales-mode generation path in rag_service

**Files:**
- Modify: `app/services/rag_service.py`
- Test: `tests/sales/test_chat_sales_generation.py`

**Interfaces:**
- Consumes: `SALES_RESPONSE_SCHEMA`, `build_sales_directive`, `parse_sales_output` (Task 2).
- Produces: `async def generate_sales_response(self, prompt, config, *, model_name=None) -> tuple[str, dict | None, int | None, bool]` — returns `(reply_text, sales_meta, token_count, is_api_error)` where `sales_meta = {intent, signals, captured}` or `None` on parse/api failure (caller then has the plain `reply_text`).

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_chat_sales_generation.py
import pytest
from unittest.mock import AsyncMock, patch
from app.services.rag_service import RAGService

@pytest.mark.asyncio
async def test_sales_generation_parses_structured_output(monkeypatch):
    svc = RAGService.__new__(RAGService)  # or the project's construction pattern
    raw = '{"reply":"Our Business plan scales with team size. How large is your team?","intent":"PRICING_INQUIRY","signals":{"asked_features":false,"requested_demo":false,"mentioned_competitor":false,"ready_to_buy":false},"captured":{"name":null,"email":null,"company":null,"team_size":null,"industry":null}}'
    with patch.object(svc, "_call_openai_raw", AsyncMock(return_value=(raw, 42, False))):
        reply, meta, tokens, err = await svc.generate_sales_response(_fake_prompt(), _fake_config())
    assert "team" in reply.lower()
    assert meta["intent"] == "PRICING_INQUIRY"
    assert err is False

@pytest.mark.asyncio
async def test_sales_generation_falls_back_on_malformed(monkeypatch):
    svc = RAGService.__new__(RAGService)
    with patch.object(svc, "_call_openai_raw", AsyncMock(return_value=("not json", 5, False))):
        reply, meta, tokens, err = await svc.generate_sales_response(_fake_prompt(), _fake_config())
    assert reply == "not json"   # plain fallback
    assert meta is None
```

(Adapt `_fake_prompt`/`_fake_config`/construction to the real `RAGService` shape; if `rag_service` calls OpenAI inline rather than via a helper, refactor the raw call into a small `_call_openai_raw` so it's mockable — keep the existing `generate_ai_response` behavior unchanged.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_chat_sales_generation.py -v`
Expected: FAIL (`generate_sales_response` missing).

- [ ] **Step 3: Implement**

Add `generate_sales_response`: build messages like `generate_ai_response` but (a) append `build_sales_directive(config.qualification_questions)` to the system message, (b) pass `response_format` for JSON structured output using `SALES_RESPONSE_SCHEMA`, (c) call the model (gpt-4o-mini), (d) `parse_sales_output(raw)` → if parsed, return `(parsed["reply"], {intent,signals,captured}, tokens, False)`; if None or api error, return `(raw_or_fallback_text, None, tokens, is_err)`. Reuse the existing rate limiter + error handling. Do NOT change `generate_ai_response`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_chat_sales_generation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/rag_service.py tests/sales/test_chat_sales_generation.py
git commit -m "feat(sales): sales-mode structured generation in rag_service"
```

---

### Task 4: LeadService get-or-create-by-conversation + enrich

**Files:**
- Modify: `app/services/lead_service.py`
- Test: `tests/sales/test_lead_enrich.py`

**Interfaces:**
- Consumes: `Lead`, `apply_score` (Slice 1), `normalize_intent` (Task 1).
- Produces:
  - `get_or_create_by_conversation(tenant_id, conversation_id, *, intent, source="chat_widget", utm=None) -> Lead`
  - `enrich_from_chat(lead, *, intent, captured: dict, signals: dict) -> Lead` — set newly-provided `captured` fields (validate email; never overwrite non-empty with null/empty), update `intent` (normalized), `apply_score(lead, signals=signals)`, persist, return lead.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_lead_enrich.py
import pytest, uuid
from app.services.lead_service import LeadService
from app.db.models.lead import Lead

@pytest.mark.asyncio
async def test_get_or_create_is_idempotent_per_conversation(db_session, make_tenant):
    t = await make_tenant(); conv = uuid.uuid4(); svc = LeadService(db_session)
    a = svc.get_or_create_by_conversation(t.id, conv, intent="PRICING_INQUIRY")
    db_session.commit()
    b = svc.get_or_create_by_conversation(t.id, conv, intent="PRICING_INQUIRY")
    assert a.id == b.id  # same lead, not a duplicate

@pytest.mark.asyncio
async def test_enrich_fills_and_rescores(db_session, make_tenant):
    t = await make_tenant(); conv = uuid.uuid4(); svc = LeadService(db_session)
    lead = svc.get_or_create_by_conversation(t.id, conv, intent="PRICING_INQUIRY"); db_session.commit()
    before = lead.lead_score or 0
    svc.enrich_from_chat(lead, intent="ENTERPRISE_PROSPECT",
        captured={"name":"John","email":"john@co.com","company":"Co","team_size":"200","industry":"Bank"},
        signals={"requested_demo": True})
    assert lead.email == "john@co.com" and lead.intent == "ENTERPRISE_PROSPECT"
    assert (lead.lead_score or 0) > before  # email+company+team>50+enterprise+demo

@pytest.mark.asyncio
async def test_enrich_ignores_blank_and_invalid_email(db_session, make_tenant):
    t = await make_tenant(); conv = uuid.uuid4(); svc = LeadService(db_session)
    lead = svc.get_or_create_by_conversation(t.id, conv, intent="PRICING_INQUIRY")
    lead.name = "Set"; db_session.commit()
    svc.enrich_from_chat(lead, intent="PRICING_INQUIRY",
        captured={"name": None, "email": "not-an-email", "company": ""}, signals={})
    assert lead.name == "Set"      # not overwritten with null
    assert lead.email in (None, "")  # invalid email not stored
```

(Adapt fixtures to the repo's real `db_session`/tenant factory per Slice 1 tests.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_lead_enrich.py -v`
Expected: FAIL (methods missing).

- [ ] **Step 3: Implement**

`get_or_create_by_conversation`: `select(Lead).where(tenant_id, conversation_id)` → return existing, else construct `Lead(tenant_id, conversation_id, intent=normalize_intent(intent), source=source, utm=utm or {}, status=NEW)`, `apply_score(lead)`, `db.add`, `flush`. `enrich_from_chat`: for each `captured` key, set only if the value is non-empty AND (for email) passes an email-format check (reuse the existing validator used in the escalation contact-capture path); set `lead.intent = normalize_intent(intent)`; `apply_score(lead, signals=signals)`; `commit`; `refresh`; return.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_lead_enrich.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/lead_service.py tests/sales/test_lead_enrich.py
git commit -m "feat(sales): LeadService get_or_create_by_conversation + enrich_from_chat"
```

---

### Task 5: Chat handler sales orchestration + response meta

**Files:**
- Modify: `app/api/v1/endpoints/chat.py`, `app/schemas/conversation.py`
- Test: `tests/api/v1/test_chat_lead_capture.py`

**Interfaces:**
- Consumes: `generate_sales_response` (Task 3), `get_or_create_by_conversation`/`enrich_from_chat` (Task 4), `is_trigger_intent` (Task 1), `TenantSalesAgentConfig`.
- Produces: chat response with `meta.lead_captured = {name, email}` set on the turn email is first captured; no behavioral change when sales mode is off.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/v1/test_chat_lead_capture.py
import pytest

@pytest.mark.asyncio
async def test_trigger_intent_creates_anonymous_lead(client, sales_enabled_tenant, mock_sales_llm):
    mock_sales_llm(intent="PRICING_INQUIRY", captured={}, signals={})
    r = await client.post("/api/v1/chat/message", json={"message":"how much for bulk?","tenant_id":str(sales_enabled_tenant.id),"user_identifier":"v1"})
    assert r.status_code == 200
    # a lead now exists for this conversation, anonymous, source chat_widget
    lead = await _only_lead(sales_enabled_tenant.id)
    assert lead.email is None and lead.source == "chat_widget" and lead.intent == "PRICING_INQUIRY"

@pytest.mark.asyncio
async def test_enrich_same_lead_and_lead_captured_meta(client, sales_enabled_tenant, mock_sales_llm):
    mock_sales_llm(intent="PRICING_INQUIRY", captured={}, signals={})
    r1 = await client.post("/api/v1/chat/message", json={"message":"pricing?","tenant_id":str(sales_enabled_tenant.id),"user_identifier":"v1"})
    sid = r1.json()["session_id"]
    mock_sales_llm(intent="ENTERPRISE_PROSPECT", captured={"name":"John","email":"john@co.com","team_size":"200"}, signals={"requested_demo":True})
    r2 = await client.post("/api/v1/chat/message", json={"message":"John, john@co.com, 200 staff","session_id":sid,"tenant_id":str(sales_enabled_tenant.id),"user_identifier":"v1"})
    assert r2.json()["message"]["meta"]["lead_captured"] == {"name":"John","email":"john@co.com"}
    assert (await _count_leads(sales_enabled_tenant.id)) == 1  # SAME lead

@pytest.mark.asyncio
async def test_support_intent_creates_no_lead(client, sales_enabled_tenant, mock_sales_llm):
    mock_sales_llm(intent="SUPPORT", captured={}, signals={})
    await client.post("/api/v1/chat/message", json={"message":"reset password","tenant_id":str(sales_enabled_tenant.id),"user_identifier":"v1"})
    assert (await _count_leads(sales_enabled_tenant.id)) == 0

@pytest.mark.asyncio
async def test_sales_mode_off_uses_plain_path(client, plain_tenant, mock_plain_llm):
    r = await client.post("/api/v1/chat/message", json={"message":"pricing?","tenant_id":str(plain_tenant.id),"user_identifier":"v1"})
    assert r.status_code == 200
    assert (await _count_leads(plain_tenant.id)) == 0
    assert "lead_captured" not in (r.json()["message"]["meta"] or {})
```

(Provide fixtures: `sales_enabled_tenant` (a tenant with `TenantSalesAgentConfig.agent_enabled=True, lead_capture_enabled=True`), `plain_tenant`, and a `mock_sales_llm`/`mock_plain_llm` that patches `generate_sales_response`/`generate_ai_response`. Adapt helper names to the repo's test utils.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/v1/test_chat_lead_capture.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `chat.py` message handler: load the tenant's `TenantSalesAgentConfig`. If `agent_enabled and lead_capture_enabled`: call `generate_sales_response(...)` instead of `generate_ai_response(...)`; the visitor-visible `reply` is used exactly as the plain text would be. Then, if `sales_meta` is not None: resolve `existing = LeadService.get_lead_by_conversation(...)` (add a tiny helper or reuse get_or_create gated on trigger); create only when `existing` or `is_trigger_intent(sales_meta["intent"], config.trigger_intents)`; `enrich_from_chat(lead, intent, captured, signals)`; if email newly set this turn → `response_meta["lead_captured"] = {"name": lead.name, "email": lead.email}`. If `agent_enabled`/`lead_capture_enabled` false → unchanged `generate_ai_response` path, no lead logic. Document `meta.lead_captured` in `app/schemas/conversation.py`. Keep all existing escalation/flow/ticket behavior intact.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/api/v1/test_chat_lead_capture.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/chat.py app/schemas/conversation.py tests/api/v1/test_chat_lead_capture.py
git commit -m "feat(sales): chat handler sales orchestration + lead_captured meta"
```

---

### Task 6: Full-suite green + no support regression

**Files:** none new (verification).

- [ ] **Step 1: Run the full backend suite**

Run: `poetry run pytest`
Expected: all green — new sales tests + existing chat/support/auditor/Slice-1 tests. Pay attention to existing chat tests (the plain path must be unchanged when sales mode is off).

- [ ] **Step 2: Sanity-check the off path**

Confirm by reading the handler that a tenant without `agent_enabled` never calls `generate_sales_response` and never touches `LeadService`.

- [ ] **Step 3: Commit (if fixups)**

```bash
git commit -am "test(sales): full-suite green for chat-sales backend"
```

---

## Self-Review notes
- Spec §2 labels → Task 1. §3 structured generation → Tasks 2–3. §4 lead create/enrich/score → Task 4. §4 orchestration + §5 response meta → Task 5. §6 safety covered across Tasks 1/4/5. §7 testing distributed; §6 verification → Task 6. All spec sections covered.
- Names consistent: `SALES_INTENTS`, `normalize_intent`, `is_trigger_intent`, `SALES_RESPONSE_SCHEMA`, `build_sales_directive`, `parse_sales_output`, `generate_sales_response`, `get_or_create_by_conversation`, `enrich_from_chat`, `meta.lead_captured`.
- Adapt `db_session`/tenant fixtures + the `RAGService` construction/`_call_openai_raw` seam to the real repo during Tasks 3–5; if `rag_service` doesn't already isolate the OpenAI call, extract a minimal mockable helper without changing `generate_ai_response`'s behavior.
