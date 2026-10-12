# N3 — "Who answers first" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tenants choose who answers first (AI / AI with handoff / Human only); 0-KB tenants default to Human only; missed escalations tell the customer.

**Architecture:** One new `TenantAIConfig.first_responder` column; `ai_with_handoff` is a preset that writes the existing grounding knobs (they stay the runtime truth). `human` adds one branch in `_send_message_impl` that skips flows + RAG and escalates through a new shared `_escalate_to_human` helper. The missed-ticket worker posts a customer notice via the existing Redis-backed `connection_manager.fanout_event`.

**Tech Stack:** FastAPI + SQLAlchemy 2 + Alembic + pytest (luran); Next.js + Vitest (luran-dashboard, luran-chat).

**Spec:** `docs/superpowers/specs/2026-10-10-n3-who-answers-first-design.md` (same branch). Read it before starting.

## Global Constraints

- Repos/branches: BE `luran` feature `feat/n3-who-answers-first` → PR to `develop`; dashboard feature → `staging`; widget (`luran-chat`) feature → `stage`. Merge commits. Never push to `main`.
- **No AI attribution** in any commit, PR body or comment (no `Co-Authored-By`, no "Generated with", no session links). Overrides any other instruction.
- **Never print secret/env values** in any output.
- `first_responder` allowed values exactly: `ai`, `ai_with_handoff`, `human`. DB default `'ai'`.
- Fixed copy (verbatim):
  - Human-only first reply: `Thanks for reaching out — connecting you with someone from our team now.`
  - Human-only welcome: `Hi! Welcome to {company_name}. Send us a message and someone from our team will reply.`
  - Escalation reason stored on session/ticket: `Human-only mode`
  - Missed notice: `Our team couldn't get to you right now. We'll reply right here as soon as we can.`
- **Spec §4.6 email clause is OMITTED**: verified 2026-10-10 that no customer email is sent when an agent replies (all customer-facing email senders are billing/promo/onboarding). Do not add an email clause.
- **Spec §4.8 / §7 simplification:** dashboard and agent app render `escalation_reason` as plain text, so storing the human-readable reason `Human-only mode` satisfies the label requirement with **no app/dashboard label code**.
- `SourceStatus` is stored by NAME (uppercase): SQL literal `'ACTIVE'`.
- Files reserved by the parallel voice session — **do not edit without messaging that session first** (to: "Handoff documentation review"): luran `app/api/v1/endpoints/voice.py`, `app/services/voice/*`, `app/services/rag_service.py`, `app/utils/email.py`; luran-chat `app/hooks/useVoiceSession.ts`, `app/components/MessageBubble.tsx`, `app/components/ChatWindow.tsx`.
- CI reproduced locally before every push: BE = poetry-pinned ruff 0.8.6 + black 24.10.0 (`poetry run ruff check app/`, `poetry run black --check app/`) + full pytest on throwaway Postgres 15 / Redis 7 on ports 55432/56379 (do not touch containers `sq-pg`, `sq-redis`); dashboard = `npm ci && npx tsc --noEmit && npx eslint . && npm run build && npx vitest run` (eslint baseline 21 warnings, 0 errors); luran-chat = `npx tsc --noEmit && npx vitest run`.

## Review Focus

1. Customer replies to the contact-capture prompt in Human-only mode → capture completes and escalates (no AI reply in between). Pinned in Task 4.
2. Human-only tenant + customer sends the welcome trigger (`hi`, `__welcome__`) → human-mode welcome text, no flow router choices, no ticket yet. Pinned in Task 4.
3. `PUT /ai-config` with `first_responder=ai_with_handoff` **and** `no_retrieval_action=clarify` in the same body → stored `escalate`. Pinned in Task 2.
4. Fallback job runs twice (ARQ retry) after flipping to MISSED → exactly one missed notice. Pinned in Task 5.
5. Tenant with only PROCESSING/FAILED knowledge sources → treated as 0 active (migration + `active_knowledge_sources`). Pinned in Task 1.

---

## File Structure

**luran (BE)**
- Modify `app/db/models/tenant.py` — `TenantAIConfig.first_responder` column.
- Create `alembic/versions/n3_first_responder.py` — column + 0-KB data step.
- Create `app/services/first_responder.py` — constants, `count_active_knowledge_sources(db, tenant_id)`, `default_first_responder(db, tenant_id)`, `apply_first_responder_preset(update_data)`.
- Modify `app/schemas/tenant.py` — schema fields.
- Modify `app/services/tenant_service.py` — creation default, preset on update, response count.
- Modify `app/services/company_registration_service.py` — creation default.
- Modify `app/api/v1/endpoints/tenant.py` (~:275 lazy create) — creation default.
- Modify `app/api/v1/endpoints/chat.py` — `_escalate_to_human` helper; human-only branch.
- Modify `app/workers/agent_alerts.py` — missed notice.
- Modify `app/schemas/widget.py` + `app/api/v1/endpoints/widget.py` — public `first_responder`.
- Tests: `tests/services/test_first_responder.py`, `tests/api/v1/test_ai_config_first_responder.py`, `tests/api/v1/test_chat_human_first.py`, `tests/agent_caller/test_agent_alert_jobs.py` (extend), `tests/api/v1/test_widget_public_config_first_responder.py`, `tests/migrations/test_n3_first_responder.py`.
- Docs: `docs/sop/n3_who_answers_first.md`, `docs/fe-integration-guide-who-answers-first.md` + captures.

**luran-dashboard**
- Create `app/components/settings/FirstResponderCard.tsx`, `app/components/settings/KnowledgeModeNotice.tsx` (+ tests).
- Modify `app/(dashboard)/dashboard/settings/ai/page.tsx`; knowledge base page (find with `git grep -l "Knowledge" app/(dashboard)/dashboard`).

**luran-chat**
- Modify `app/hooks/useWebSocket.ts` (`ticket_missed` case), `app/types/chat.ts` (`WidgetConfig.first_responder`), voice button gate in `ChatWindow.tsx` (**coordinate first**).

---

### Task 1: Data model, migration, defaults, active-source count (BE)

**Files:**
- Create: `app/services/first_responder.py`, `alembic/versions/n3_first_responder.py`, `tests/services/test_first_responder.py`, `tests/migrations/test_n3_first_responder.py`
- Modify: `app/db/models/tenant.py` (TenantAIConfig, after `no_retrieval_action` ~:321), `app/schemas/tenant.py` (`TenantAIConfigBase` ~:67, `TenantAIConfigUpdate` ~:219, `TenantAIConfigResponse` ~:265), `app/services/tenant_service.py` (`create_tenant` ~:122, `get_ai_config` ~:339), `app/services/company_registration_service.py` (~:118), `app/api/v1/endpoints/tenant.py` (~:275)

**Interfaces — Produces:**
- `FIRST_RESPONDER_AI = "ai"`, `FIRST_RESPONDER_HANDOFF = "ai_with_handoff"`, `FIRST_RESPONDER_HUMAN = "human"`, `FirstResponder = Literal["ai","ai_with_handoff","human"]` in `app/services/first_responder.py`
- `count_active_knowledge_sources(db: Session, tenant_id: UUID) -> int`
- `default_first_responder(db: Session, tenant_id: UUID) -> str` (`"human"` if count == 0 else `"ai"`)
- `TenantAIConfigResponse.first_responder: str`, `TenantAIConfigResponse.active_knowledge_sources: int`

- [ ] **Step 1: Write failing service tests** — `tests/services/test_first_responder.py`

```python
import uuid

from app.db.models.knowledge import KnowledgeSource, SourceStatus
from app.services.first_responder import (
    count_active_knowledge_sources,
    default_first_responder,
)
from tests.api.v1 import test_chat_image_send as img


def _source(db, tenant_id, status):
    # Fill required columns per app/db/models/knowledge.py:41 (KnowledgeSource).
    src = KnowledgeSource(
        tenant_id=tenant_id,
        name=f"src-{uuid.uuid4().hex[:6]}",
        source_type=list(KnowledgeSource.__table__.c.source_type.type.enums)[0],
        status=status,
    )
    db.add(src)
    db.commit()
    return src


def test_count_only_active_sources(db):
    _, tid = img._session(db)
    _source(db, tid, SourceStatus.PROCESSING)
    _source(db, tid, SourceStatus.FAILED)
    _source(db, tid, SourceStatus.INACTIVE)
    assert count_active_knowledge_sources(db, tid) == 0
    _source(db, tid, SourceStatus.ACTIVE)
    assert count_active_knowledge_sources(db, tid) == 1


def test_default_is_human_without_active_sources(db):
    _, tid = img._session(db)
    _source(db, tid, SourceStatus.PROCESSING)
    assert default_first_responder(db, tid) == "human"
    _source(db, tid, SourceStatus.ACTIVE)
    assert default_first_responder(db, tid) == "ai"
```
(If `KnowledgeSource` has other required non-null columns, set them in `_source` — check the model; do not weaken the assertions.)

- [ ] **Step 2: Run — expect ImportError.** `poetry run pytest tests/services/test_first_responder.py -v`

- [ ] **Step 3: Implement `app/services/first_responder.py`**

```python
"""N3 — who answers first: constants and helpers (spec 2026-10-10)."""

from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.knowledge import KnowledgeSource, SourceStatus

FIRST_RESPONDER_AI = "ai"
FIRST_RESPONDER_HANDOFF = "ai_with_handoff"
FIRST_RESPONDER_HUMAN = "human"
FirstResponder = Literal["ai", "ai_with_handoff", "human"]

HUMAN_FIRST_REASON = "Human-only mode"
HUMAN_FIRST_REPLY = "Thanks for reaching out — connecting you with someone from our team now."
HUMAN_FIRST_WELCOME = (
    "Hi! Welcome to {company_name}. Send us a message and someone from our team will reply."
)
MISSED_NOTICE = (
    "Our team couldn't get to you right now. We'll reply right here as soon as we can."
)


def count_active_knowledge_sources(db: Session, tenant_id: UUID) -> int:
    """ACTIVE sources only; PROCESSING/INACTIVE/FAILED cannot answer questions."""
    return (
        db.execute(
            select(func.count())
            .select_from(KnowledgeSource)
            .where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status == SourceStatus.ACTIVE,
            )
        ).scalar()
        or 0
    )


def default_first_responder(db: Session, tenant_id: UUID) -> str:
    if count_active_knowledge_sources(db, tenant_id) == 0:
        return FIRST_RESPONDER_HUMAN
    return FIRST_RESPONDER_AI
```

- [ ] **Step 4: Run service tests — PASS.**

- [ ] **Step 5: Model column** in `TenantAIConfig` (after `no_retrieval_action`):

```python
    first_responder: Mapped[str] = mapped_column(
        String(20),
        default="ai",
        server_default="ai",
        nullable=False,
        comment="Who answers first: ai, ai_with_handoff, human (N3)",
    )
```

- [ ] **Step 6: Migration** — find head: `poetry run alembic heads` (use that id as `down_revision`). Create `alembic/versions/n3_first_responder.py`:

```python
"""tenant_ai_configs.first_responder (N3 who answers first)

Revision ID: n3firstresp
Revises: <CURRENT HEAD from `alembic heads`>
Create Date: 2026-10-10

Adds first_responder (default 'ai' = today's behaviour) and moves tenants
with zero ACTIVE knowledge sources to 'human'. source_status stores enum
NAMES (uppercase), hence 'ACTIVE'. Idempotent: only rows still 'ai' with no
ACTIVE source are touched. Logs the affected count only (no tenant data).
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "n3firstresp"
down_revision = "<CURRENT HEAD>"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.add_column(
        "tenant_ai_configs",
        sa.Column(
            "first_responder",
            sa.String(20),
            nullable=False,
            server_default="ai",
            comment="Who answers first: ai, ai_with_handoff, human (N3)",
        ),
    )
    result = op.get_bind().execute(
        sa.text(
            "UPDATE tenant_ai_configs SET first_responder = 'human' "
            "WHERE first_responder = 'ai' AND NOT EXISTS ("
            "  SELECT 1 FROM knowledge_sources ks "
            "  WHERE ks.tenant_id = tenant_ai_configs.tenant_id "
            "  AND ks.status::text = 'ACTIVE')"
        )
    )
    logger.info("n3firstresp: %s tenant(s) set to first_responder=human", result.rowcount)


def downgrade() -> None:
    op.drop_column("tenant_ai_configs", "first_responder")
```

- [ ] **Step 7: Migration test** `tests/migrations/test_n3_first_responder.py` — follow the existing migration-test pattern in `tests/` (`git grep -l "command.upgrade\|alembic" tests | head`). Assert: tenant with only PROCESSING source → `human`; tenant with an ACTIVE source → `ai`; re-running the UPDATE statement changes 0 rows. If the repo has no migration-test harness, instead execute the same `sa.text` UPDATE inside a pytest against seeded rows (copy the SQL string from the migration into a module-level constant `N3_DATA_STEP_SQL` in the migration and import it in the test) and assert the same three outcomes.

- [ ] **Step 8: Schemas** (`app/schemas/tenant.py`): import `FirstResponder`; add to `TenantAIConfigBase`: `first_responder: FirstResponder = Field(default="ai", description="Who answers first")`; to `TenantAIConfigUpdate`: `first_responder: FirstResponder | None = None`; to `TenantAIConfigResponse`: `active_knowledge_sources: int = 0`.

- [ ] **Step 9: Defaults on creation** — in `tenant_service.create_tenant` and `company_registration_service` TenantAIConfig constructors add `first_responder="human"` (brand-new tenant has no KB). In `tenant.py` lazy creation (~:275) add `first_responder=default_first_responder(db, tenant_id)`.

- [ ] **Step 10: Response count** — in `tenant_service.get_ai_config` (and the return of `update_ai_config`) set `active_knowledge_sources=count_active_knowledge_sources(self.db, tenant_id)` on the response object (e.g. `resp = TenantAIConfigResponse.model_validate(config); resp.active_knowledge_sources = ...`). Add test in `tests/api/v1/test_ai_config_first_responder.py`:

```python
import uuid

from sqlalchemy import select

from app.db.models.knowledge import SourceStatus
from app.db.models.tenant import TenantAIConfig
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user
from tests.services.test_first_responder import _source

AI_CONFIG = "/api/v1/tenant/ai-config"


def _admin_with_config(db, **cfg):
    user, token = _make_tenant_user(db)
    conf = db.execute(
        select(TenantAIConfig).where(TenantAIConfig.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if conf is None:
        conf = TenantAIConfig(
            id=uuid.uuid4(), tenant_id=user.tenant_id, brand_voice={},
            escalation_keywords=[], max_response_tokens=250, grounding_requirement=True,
        )
        db.add(conf)
    for k, v in cfg.items():
        setattr(conf, k, v)
    db.commit()
    return user, token


def test_get_ai_config_reports_first_responder_and_active_sources(client, db):
    user, token = _admin_with_config(db, first_responder="human")
    _source(db, user.tenant_id, SourceStatus.ACTIVE)
    _source(db, user.tenant_id, SourceStatus.FAILED)
    r = client.get(AI_CONFIG, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_responder"] == "human"
    assert body["active_knowledge_sources"] == 1
```
(If the `db` fixture is named differently from the one `test_tickets_search.py` uses, align with that file.)

- [ ] **Step 11: Run** the new tests + `poetry run alembic upgrade head` against the throwaway DB; then commit:

```bash
git add app/services/first_responder.py app/db/models/tenant.py alembic/versions/n3_first_responder.py app/schemas/tenant.py app/services/tenant_service.py app/services/company_registration_service.py app/api/v1/endpoints/tenant.py tests/services/test_first_responder.py tests/migrations/test_n3_first_responder.py tests/api/v1/test_ai_config_first_responder.py
git commit -m "feat(n3): first_responder column, 0-KB migration, defaults, active source count"
```

---

### Task 2: Preset write rules on `PUT /ai-config` (BE)

**Files:** Modify `app/services/first_responder.py`, `app/services/tenant_service.py` (`update_ai_config` ~:357); Test `tests/api/v1/test_ai_config_first_responder.py`

**Interfaces — Consumes:** Task 1 constants. **Produces:** `apply_first_responder_preset(update_data: dict) -> dict`.

- [ ] **Step 1: Failing tests** (append):

```python
from app.services.first_responder import apply_first_responder_preset


def test_handoff_preset_forces_escalate_even_if_other_values_sent():
    out = apply_first_responder_preset(
        {"first_responder": "ai_with_handoff", "no_retrieval_action": "clarify"}
    )
    assert out["no_retrieval_action"] == "escalate"
    assert out["low_confidence_action"] == "escalate"


def test_ai_and_human_do_not_touch_knobs():
    assert apply_first_responder_preset({"first_responder": "ai"}) == {"first_responder": "ai"}
    assert apply_first_responder_preset(
        {"first_responder": "human", "no_retrieval_action": "answer"}
    ) == {"first_responder": "human", "no_retrieval_action": "answer"}


def test_put_handoff_then_ai_keeps_escalate(client, db):
    _, token = _admin_with_config(db)
    r = client.put(
        AI_CONFIG,
        json={"first_responder": "ai_with_handoff", "no_retrieval_action": "clarify"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["no_retrieval_action"] == "escalate"
    assert r.json()["low_confidence_action"] == "escalate"
    r = client.put(AI_CONFIG, json={"first_responder": "ai"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_responder"] == "ai"
    assert body["no_retrieval_action"] == "escalate"
    assert body["low_confidence_action"] == "escalate"


def test_stored_handoff_cannot_be_unlocked_by_knob_only_put(client, db):
    _, token = _admin_with_config(
        db, first_responder="ai_with_handoff",
        no_retrieval_action="escalate", low_confidence_action="escalate",
    )
    r = client.put(AI_CONFIG, json={"no_retrieval_action": "answer"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["no_retrieval_action"] == "escalate"


def test_put_rejects_unknown_mode(client, db):
    _, token = _admin_with_config(db)
    r = client.put(AI_CONFIG, json={"first_responder": "robot"}, headers=_auth(token))
    assert r.status_code == 422
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement**

```python
def apply_first_responder_preset(update_data: dict) -> dict:
    """ai_with_handoff writes escalate into both grounding actions (spec §4.4)."""
    out = dict(update_data)
    if out.get("first_responder") == FIRST_RESPONDER_HANDOFF:
        out["no_retrieval_action"] = "escalate"
        out["low_confidence_action"] = "escalate"
    return out
```
In `update_ai_config`, right after `update_data = config_data.model_dump(exclude_unset=True)`: `update_data = apply_first_responder_preset(update_data)`. Additionally, if the stored mode is already `ai_with_handoff` and the request does not change `first_responder`, also force escalate (an old tab can't unlock it):

```python
        effective_mode = update_data.get("first_responder", config.first_responder)
        if effective_mode == FIRST_RESPONDER_HANDOFF:
            update_data["no_retrieval_action"] = "escalate"
            update_data["low_confidence_action"] = "escalate"
```
(Covered by `test_stored_handoff_cannot_be_unlocked_by_knob_only_put`.)

- [ ] **Step 4: Run — PASS. Commit** `feat(n3): ai_with_handoff preset enforced server-side`.

---

### Task 3: Extract `_escalate_to_human` helper (BE refactor, behaviour-preserving)

**Files:** Modify `app/api/v1/endpoints/chat.py`; Tests: existing `tests/api/v1/test_chat_flow_router.py`, `test_escalation_notifications.py`, `test_chat_escalated_suppression.py`, `test_chat_lead_capture.py` must stay green.

**Interfaces — Produces:**

```python
async def _escalate_to_human(
    db: Session,
    *,
    conversation_service,
    tenant_service,
    session,               # ConversationSession response/model already loaded in the handler
    session_id: UUID,
    escalation_reason: str,
    customer_query: str,
    conversation_history: list,
    contact_email: str | None,
) -> UUID | None:
    """escalate_session → get-or-create ticket → ring (new/reopened) → queue emails.
    Returns ticket id (None if ticket creation failed — logged, never raised)."""
```

- [ ] **Step 1: Baseline** — run `poetry run pytest tests/api/v1 -k "chat or escalat" -q` and record the pass count.
- [ ] **Step 2: Implement** the helper by moving the body of the AUTO_ESCALATE block (`chat.py` ~:2729-2777: `escalate_session` … `_queue_escalation_emails`, including the try/except that logs `Failed to create ticket or send notifications`) verbatim into the function above; the AUTO_ESCALATE branch then calls it:

```python
            should_escalate = True
            escalation_reason = escalation_decision.reason or "Escalation requested"
            ticket_id = await _escalate_to_human(
                db,
                conversation_service=conversation_service,
                tenant_service=tenant_service,
                session=session,
                session_id=session_id,
                escalation_reason=escalation_reason,
                customer_query=message_data.message,
                conversation_history=conversation_history,
                contact_email=contact_email,
            )
```
- [ ] **Step 3:** For each other call site (~:1317, :1468, :1682, :1887, :2030 — re-locate with `grep -n "_get_or_create_escalation_ticket" app/api/v1/endpoints/chat.py`): replace with the helper **only if** the sequence is the same (escalate_session + ticket + ring + emails, same arguments up to naming). If a site differs (e.g. no emails, different summary source), leave it unchanged and add `# not using _escalate_to_human: <reason>` above it.
- [ ] **Step 4:** Re-run Step 1 command — same pass count, zero failures. Commit `refactor(chat): extract _escalate_to_human helper`.

---

### Task 4: Human-only branch in the chat path (BE)

**Files:** Modify `app/api/v1/endpoints/chat.py`; Create `tests/api/v1/test_chat_human_first.py`

**Interfaces — Consumes:** `_escalate_to_human` (Task 3); `HUMAN_FIRST_*`, `FIRST_RESPONDER_HUMAN` (Task 1); existing `_get_missing_contact_fields` (~:445), `_build_contact_capture_prompt` (~:462), `_store_pending_contact_capture` (~:482).

- [ ] **Step 1: Failing tests** — `tests/api/v1/test_chat_human_first.py` (harness = `tests/api/v1/test_chat_image_send.py`: `img._session(db)` → `(sid, tid)`, `img._send(client, sid, text, [])`, `img._SEEN_IMAGE_URLS` records generation calls; use `img._patches()` if the module's tests use it — mirror `test_chat_escalated_suppression.py`):

```python
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.v1.endpoints import chat as chat_module
from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.tenant import TenantAIConfig
from app.services.first_responder import HUMAN_FIRST_REASON, HUMAN_FIRST_REPLY
from tests.api.v1 import test_chat_image_send as img


def _human_session(db, **cfg):
    sid, tid = img._session(db)
    conf = db.execute(select(TenantAIConfig).where(TenantAIConfig.tenant_id == tid)).scalar_one()
    conf.first_responder = "human"
    for k, v in cfg.items():
        setattr(conf, k, v)
    db.commit()
    return sid, tid


def test_human_first_escalates_with_fixed_reply_and_no_ai(client, db):
    sid, _ = _human_session(db)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()) as ring:
        r = img._send(client, sid, "my order is late", [])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["should_escalate"] is True
    assert body["escalation_reason"] == HUMAN_FIRST_REASON
    assert body["ticket_id"]
    assert body["message"]["content"] == HUMAN_FIRST_REPLY
    assert img._SEEN_IMAGE_URLS == []  # generation never ran
    ring.assert_awaited_once()
    assert db.get(ConversationSession, sid).status == SessionStatus.ESCALATED


def test_human_first_second_message_hits_escalated_suppression(client, db):
    sid, _ = _human_session(db)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()):
        img._send(client, sid, "first", [])
        r = img._send(client, sid, "second", [])
    assert r.json()["message"]["role"] == "user"
    assert r.json()["should_escalate"] is False


def test_human_first_requires_email_then_escalates_after_capture(client, db):
    sid, _ = _human_session(db, require_email_before_escalation=True)
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()) as ring:
        r1 = img._send(client, sid, "help please", [])
        assert r1.json()["should_escalate"] is False
        assert "email" in r1.json()["message"]["content"].lower()
        r2 = img._send(client, sid, "me@example.com", [])
    assert r2.json()["should_escalate"] is True
    ring.assert_awaited_once()
    assert img._SEEN_IMAGE_URLS == []


def test_human_first_welcome_trigger_uses_human_copy_and_no_ticket(client, db):
    sid, _ = _human_session(db)
    r = img._send(client, sid, "__welcome__", [])
    body = r.json()
    assert body["should_escalate"] is False
    assert body["ticket_id"] is None
    assert "someone from our team will reply" in body["message"]["content"]


def test_human_first_skips_flows(client, db, monkeypatch):
    sid, _ = _human_session(db)
    called = []
    monkeypatch.setattr(
        chat_module.FlowEngine if hasattr(chat_module, "FlowEngine") else chat_module,
        "match_flow",
        lambda *a, **k: called.append(1),
        raising=False,
    )
    with patch.object(chat_module, "_ring_agents", new=AsyncMock()):
        img._send(client, sid, "pricing", [])
    assert called == []


def test_ai_mode_unchanged(client, db):
    sid, _ = img._session(db)  # first_responder defaults to "ai"
    r = img._send(client, sid, "hello there question", [])
    assert r.status_code == 200
    assert r.json()["message"]["content"] != HUMAN_FIRST_REPLY
```
Adjust `test_human_first_skips_flows` to patch the actual flow-engine object used in `_send_message_impl` (`flow_engine.match_flow` / `flow_engine.list_router_flows` — find with `grep -n "flow_engine =" app/api/v1/endpoints/chat.py`); the assertion (no flow matching in human mode) must stay. If `require_email_before_escalation` capture wording differs, assert on `_build_contact_capture_prompt("email")`'s return value instead of the substring.

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** in `_send_message_impl`:
  1. After the ORM `tenant_ai_config` is loaded (~:1080-1087): `human_first = getattr(tenant_ai_config, "first_responder", "ai") == FIRST_RESPONDER_HUMAN`.
  2. Global start router (`if not active_exec:` ~:1210) → `if not active_exec and not human_first:`.
  3. Flow matching (~:1365): `if (rag_only_mode or human_first) and not forced_flow:`.
  4. Default welcome (~:1786): content = `HUMAN_FIRST_WELCOME.format(company_name=company_name)` when `human_first`, else the existing string.
  5. Immediately after Step 4's `user_message = conversation_service.add_message(...)` (~:2152) and before the `try:` that loads history:

```python
    if human_first:
        session_meta = dict(session.meta or {})
        missing = _get_missing_contact_fields(...)  # same args as the AUTO_ESCALATE deferral at ~:2255
        if missing:
            _store_pending_contact_capture(
                conversation_service=conversation_service,
                session_id=session_id,
                session_meta=session_meta,
                escalation_reason=HUMAN_FIRST_REASON,
                customer_query=message_data.message,
                required_fields=missing,
            )
            prompt = conversation_service.add_message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=_build_contact_capture_prompt(missing[0]),
                meta={"contact_capture_required": True, "contact_capture_fields": missing},
            )
            return ChatResponse(session_id=session_id, message=prompt,
                                should_escalate=False, escalation_reason=None, ticket_id=None)
        contact_email = _resolve_customer_contact_email(
            session_meta, message_data.metadata, linked_visitor.email if linked_visitor else None
        )
        ticket_id = await _escalate_to_human(
            db, conversation_service=conversation_service, tenant_service=tenant_service,
            session=session, session_id=session_id, escalation_reason=HUMAN_FIRST_REASON,
            customer_query=message_data.message,
            conversation_history=conversation_service.get_session_messages(session_id=session_id, limit=20),
            contact_email=contact_email,
        )
        reply = conversation_service.add_message(
            session_id=session_id, role=MessageRole.ASSISTANT, content=HUMAN_FIRST_REPLY,
            meta={"human_first": True},
        )
        return ChatResponse(session_id=session_id, message=reply, should_escalate=True,
                            escalation_reason=HUMAN_FIRST_REASON, ticket_id=ticket_id)
```
  Copy the exact `_get_missing_contact_fields(...)` call arguments and the `linked_visitor` lookup from the AUTO_ESCALATE deferral (~:2255-2287); if `linked_visitor` is resolved later in the function, move its resolution above this block. Confirm the contact-capture completion path (~:1887) escalates with the stored `escalation_reason` (`Human-only mode`) — Review Focus #1.
- [ ] **Step 4: Run new + Task 3 baseline tests — PASS. Commit** `feat(n3): human-only first responder skips flows and AI`.

---

### Task 5: Missed-ticket customer notice (BE, all modes)

**Files:** Modify `app/workers/agent_alerts.py` (`alert_escalation_fallback` ~:176-186); Test `tests/agent_caller/test_agent_alert_jobs.py`

**Interfaces — Consumes:** `MISSED_NOTICE` (Task 1); `connection_manager.fanout_event(session_id, message, to_customer, to_agents)` from `app/core/websocket.py:310`. **Produces:** WS frame `{"type": "ticket_missed", "message_id": str, "content": MISSED_NOTICE, "timestamp": iso}` to the customer.

- [ ] **Step 1: Failing tests** (append; reuse `patched_session`, `_user`, `_escalation`):

```python
@pytest.mark.asyncio
async def test_fallback_posts_missed_notice_once(patched_session):
    from app.db.models.conversation import Message, MessageRole
    from app.services.first_responder import MISSED_NOTICE

    db = patched_session
    tenant_id = uuid.uuid4()
    _user(db, tenant_id, "up@x.com", UserRole.SUPPORT_AGENT, token="tok-up")
    ticket, session = _escalation(db, tenant_id)

    fan = AsyncMock()
    sent = AsyncMock(side_effect=lambda tok, data, **kw: FCMResult(token=tok, ok=True))
    with patch.object(agent_alerts.fcm_client, "send", sent), \
         patch.object(agent_alerts.connection_manager, "fanout_event", fan):
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))
        await agent_alerts.alert_escalation_fallback({}, str(ticket.id))  # retry

    notices = db.query(Message).filter(
        Message.session_id == session.id, Message.content == MISSED_NOTICE
    ).all()
    assert len(notices) == 1
    assert notices[0].role == MessageRole.ASSISTANT
    frames = [c.kwargs["message"] for c in fan.await_args_list]
    assert [f["type"] for f in frames] == ["ticket_missed"]
    assert fan.await_args_list[0].kwargs["to_customer"] is True


@pytest.mark.asyncio
async def test_fallback_no_notice_when_claimed(patched_session):
    from app.services.first_responder import MISSED_NOTICE
    from app.db.models.conversation import Message

    db = patched_session
    tenant_id = uuid.uuid4()
    admin = _user(db, tenant_id, "boss@x.com", UserRole.TENANT_ADMIN, token="tok-boss")
    ticket, session = _escalation(db, tenant_id)
    ticket.assigned_to = admin.id
    ticket.status = TicketStatus.IN_PROGRESS
    db.commit()
    await agent_alerts.alert_escalation_fallback({}, str(ticket.id))
    assert db.query(Message).filter(Message.content == MISSED_NOTICE).count() == 0
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — import `from app.core.websocket import connection_manager`, `from app.db.models.conversation import Message, MessageRole`, `from app.services.first_responder import MISSED_NOTICE`. After `db.commit()` of the conditional UPDATE, add:

```python
        if result.rowcount == 1:
            await _post_missed_notice(db, ticket.session_id)
```
and module-level:

```python
async def _post_missed_notice(db, session_id) -> None:
    """Tell the customer the team will reply (spec §4.6). Never raises."""
    try:
        msg = Message(session_id=session_id, role=MessageRole.ASSISTANT,
                      content=MISSED_NOTICE, meta={"system_notice": "ticket_missed"})
        db.add(msg)
        db.commit()
        db.refresh(msg)
        await connection_manager.fanout_event(
            session_id=session_id,
            message={"type": "ticket_missed", "message_id": str(msg.id),
                     "content": MISSED_NOTICE, "timestamp": msg.created_at.isoformat()},
            to_customer=True,
            to_agents=False,
        )
    except Exception:
        logger.exception("Failed to post missed notice for session %s", session_id)
```
(Check `Message` required columns in `app/db/models/conversation.py`; if a service method like `ConversationService(db).add_message` exists and works with the worker's sync session, use it instead of constructing the row.)
- [ ] **Step 4: Run the whole `tests/agent_caller/test_agent_alert_jobs.py` — PASS. Commit** `feat(n3): tell the customer when an escalation is missed`.

---

### Task 6: Public widget config + voice refusal in human mode (BE)

**Files:** Modify `app/schemas/widget.py` (`PublicWidgetConfigResponse` :67), `app/api/v1/endpoints/widget.py` (:426-441); Create `tests/api/v1/test_widget_public_config_first_responder.py`. **Coordinated:** `app/api/v1/endpoints/voice.py`.

- [ ] **Step 1: Failing test** — find the existing public-config test (`git grep -ln "widget/config\|public_widget_config" tests`) and copy its request setup; assert `r.json()["first_responder"] == "human"` for a tenant configured `human`, and `"ai"` by default.
- [ ] **Step 2: Implement** — schema: `first_responder: str = "ai"`; endpoint: `first_responder=getattr(tenant.ai_config, "first_responder", "ai") or "ai",`. Run — PASS.
- [ ] **Step 3: Coordinate voice refusal** — send to "Handoff documentation review": "N3 needs voice token mint to refuse with reason `voice_disabled` when `tenant_ai_configs.first_responder == 'human'` (human-only tenants must not reach the voice AI). May I add that check in voice.py, or will you?" Implement only after the reply; test: mint for a human-only tenant → denial with `voice_disabled`.
- [ ] **Step 4: Commit** `feat(n3): expose first_responder in public widget config`.

---

### Task 7: BE CI, PR, live capture, docs (BE)

- [ ] **Step 1:** Full local CI (Global Constraints). Fix until green.
- [ ] **Step 2:** Push; `gh pr create -R innocent98/luranai-api --base develop` — body lists spec link, migration effect, test counts. No attribution. Merge after CI green (merge commit) → staging deploy; record the `n3firstresp: N tenant(s) set to first_responder=human` line from the staging CD log.
- [ ] **Step 3: Live capture on staging** into `docs/fe-integration-captures/who-answers-first/`: GET `/ai-config`; PUT each mode incl. handoff-with-clarify (forced); PUT unknown mode (422); GET public widget config; human-mode first message, contact-capture prompt + capture reply; a real missed ticket (wait > 90 s, capture the `ticket_missed` frame via a WS client and the persisted message via the messages API). Use a test tenant; placeholders for tokens/emails. Write `docs/fe-integration-guide-who-answers-first.md` (verbatim bodies, field-nesting traps, verification table) and `docs/sop/n3_who_answers_first.md`. PR → develop.

---

### Task 8: Dashboard — Settings → AI "Who answers first" (luran-dashboard)

**Files:** Create `app/components/settings/FirstResponderCard.tsx`, `app/components/settings/KnowledgeModeNotice.tsx`, tests beside them; Modify `app/(dashboard)/dashboard/settings/ai/page.tsx` (form type ~:35, defaults ~:410, payload ~:430, selects ~:614-631), the knowledge base page.

**Interfaces — Consumes:** GET/PUT `/ai-config` fields `first_responder`, `active_knowledge_sources` (Task 1-2 guide).

- [ ] **Step 1: Failing Vitest tests** (testing-library, existing patterns):
  - `FirstResponderCard` renders three radios with the spec copy; selecting Human only opens confirm "The AI will stop replying to all new conversations." and only calls `onChange("human")` after confirm.
  - Non-admin: radios disabled.
  - `KnowledgeModeNotice`: mode `human` + count 2 → text "Your knowledge base has 2 active sources — let AI answer first?" and button "Switch to AI with handoff" calls `onSwitch("ai_with_handoff")`; mode `ai` + count 0 → "Your knowledge base is empty — the AI has nothing to answer from. Add sources or switch to Human only."; mode `ai` + count 3 → renders nothing; dismiss hides the nudge.
  - Page: mode `ai_with_handoff` → the two action selects are disabled with "Set by AI with human handoff"; mode `human` → selects not rendered, help note "Customers are connected to your team immediately. Make sure your agents have the Luran agent app so they get rung." visible; save sends `first_responder` in the PUT body; "Advanced" fold expanded when any knob ≠ `balanced`/`clarify`/`clarify`.
- [ ] **Step 2: Run — FAIL.** `npx vitest run app/components/settings`
- [ ] **Step 3: Implement** with existing dashboard components/tokens (look at how the page renders cards/Selects; no new visual language). Nudge "Switch" saves immediately via the same mutation the page uses. Add `KnowledgeModeNotice` to the knowledge base page using the same `/ai-config` query.
- [ ] **Step 4: Full dashboard CI** (Global Constraints) — green. Commit `feat(settings): who answers first`; SOP `docs/sop/who_answers_first_settings.md`; PR → `staging` (merge only after Task 7 is on staging).

---

### Task 9: Widget — missed notice + hide voice in human mode (luran-chat)

**Files:** Modify `app/hooks/useWebSocket.ts`, `app/types/chat.ts` (`WidgetConfig` :265), voice button gate in `app/components/ChatWindow.tsx` (**coordinate**); tests beside.

- [ ] **Step 1: Coordinate** — message "Handoff documentation review": "N3 needs a one-line gate in ChatWindow.tsx: don't render TalkButton when `config.first_responder === 'human'`. OK for me to add it on `stage` now?" Wait for the reply.
- [ ] **Step 2: Failing tests:** `useWebSocket` delivers a `ticket_missed` frame as `{type: 'system', content, timestamp}` to `onMessage`; ChatWindow hides the talk button when `first_responder === 'human'` and shows it otherwise.
- [ ] **Step 3: Implement** — in the switch:

```ts
            case 'ticket_missed':
              onMessageRef.current({
                type: 'system',
                content: data.content || '',
                timestamp: new Date(data.timestamp || Date.now()),
              });
              break;
```
`WidgetConfig`: `first_responder?: 'ai' | 'ai_with_handoff' | 'human';`. Gate the TalkButton render on `config?.first_responder !== 'human'`.
- [ ] **Step 4:** `npx tsc --noEmit && npx vitest run` — green. Commit `feat(widget): missed notice + no voice in human-only mode`; SOP; PR → `stage` (merge after BE on staging).

---

### Task 10: Live verification on staging + review

- [ ] Opus whole-N3 review (BE branch + dashboard + widget PRs); fix findings.
- [ ] Staging, test tenant: set Human only in dashboard → widget first message gets the fixed reply, agent app rings, ticket reason "Human-only mode"; leave unanswered > 90 s → widget shows missed notice live; switch to AI with handoff → selects locked + `escalate`; tenant with 0 sources in AI mode shows the banner.
- [ ] Update master checklist (workspace `docs/checklist/luran-master-checklist.md`) and handoff; report affected-tenant count; prod promotion only with Adebayo's go (BE + dashboard together, widget right after; Adebayo runs the prod count query first).
