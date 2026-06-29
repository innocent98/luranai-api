# Stage 3 Slice 4 — Quotation Generator (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute an exact in-chat quotation from the tenant's pricing config when the agent detects buying intent with enough info, persist it, and return it in `meta.quote`.

**Architecture:** A pure pricing engine (`compute_quote`) does deterministic integer money math; the chat handler, on an `offer_quote` LLM signal (gated by `quotation_enabled`), computes + persists a `Quote` (linked to the lead) and embeds it in `meta.quote`. No separate endpoint; no PDF/email (Slice 5).

**Tech Stack:** FastAPI, **synchronous** SQLAlchemy ORM, Pydantic v2, Alembic, pytest, OpenAI structured output.

## Global Constraints

- **Sync** SQLAlchemy (`db.execute(select(...)).scalar_one_or_none()`, `db.add/flush/commit/refresh`). No `await`/`AsyncSession`.
- Services follow `LeadService`/`MeetingService`: `class XService:` `def __init__(self, db: Session)`.
- **Money is integer arithmetic** (whole NGN) — no floats anywhere in the engine.
- Server-authoritative gate: `meta.quote` appears ONLY when `quotation_enabled` AND the model signal AND a computable quote (matching tier + parseable team_size). Never otherwise.
- Support / sales-off / quotation-off path unchanged (PRD §291). Slice 2 (lead capture) and Slice 3 (`offer_meeting`) behavior must remain green.
- Lowercase status strings. No Claude co-author trailer.
- Migrations: `poetry run alembic revision -m "..."` then hand-write upgrade/downgrade; `poetry run alembic upgrade head`. For a JSON/Integer `server_default`, do nullable-add → backfill → set NOT NULL (do NOT put `:`-containing literals in `sa.text` — they're treated as bind params; see the Slice 3 availability migration).
- Tests: `poetry run pytest <path> -v` with `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db`. Use `db_session` + `pg_client` fixtures from `tests/conftest.py` (the `get_db` override) — mirror `tests/api/v1/test_chat_lead_capture.py` for endpoint tests that mock the sales generation seam.
- This branch (`feat/stage3-quote-backend`) STACKS on `feat/stage3-meeting-backend` (Slice 3). The `Meeting`/`offer_meeting` code is already present — build alongside it, don't modify it.

---

### Task 1: `Quote` model + `Lead.quote_id` + `annual_discount_pct` config + migration

**Files:**
- Create: `app/db/models/quote.py`
- Modify: `app/db/models/lead.py`, `app/db/models/sales_agent_config.py`, `app/db/models/__init__.py`
- Create: `alembic/versions/<generated>_add_quotes_and_pricing.py`
- Test: `tests/sales/test_quote_model.py`

**Interfaces:**
- Produces: `Quote` (table `quotes`: `id, tenant_id, lead_id, conversation_id, currency, line_items (JSON), total (Integer), status, created_at`); `QuoteStatus` enum (`draft`/`sent`); `Lead.quote_id` (nullable uuid); `TenantSalesAgentConfig.annual_discount_pct` (Integer default 0).

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_quote_model.py
from uuid import uuid4

from app.db.models.quote import Quote, QuoteStatus
from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant
from app.db.models.lead import Lead


def _tenant(db):
    u = uuid4().hex[:8]
    t = Tenant(name=f"t_{u}", display_name=f"T {u}", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush(); db.refresh(t)
    return t


def test_quote_persists_lowercase_status(db_session):
    t = _tenant(db_session)
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="a@b.com")
    db_session.add(lead); db_session.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, conversation_id=lead.conversation_id,
              currency="NGN", line_items=[{"label": "Growth", "detail": "11–50 users", "amount": 45000}],
              total=45000, status=QuoteStatus.DRAFT)
    db_session.add(q); db_session.flush(); db_session.refresh(q)
    assert q.id is not None and q.total == 45000
    assert QuoteStatus.DRAFT.value == "draft"


def test_config_annual_discount_default(db_session):
    t = _tenant(db_session)
    cfg = TenantSalesAgentConfig.default_for(t.id)
    assert cfg.annual_discount_pct == 0
    assert cfg.pricing_tiers == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_quote_model.py -v`
Expected: FAIL (no `app.db.models.quote`).

- [ ] **Step 3: Implement the model**

```python
# app/db/models/quote.py
"""Quote — an in-chat quotation computed from tenant pricing (Stage 3 Slice 4)."""

import enum
import uuid

from sqlalchemy import JSON, Enum as SQLEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class QuoteStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"


class Quote(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "quotes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="NGN")
    line_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[QuoteStatus] = mapped_column(
        SQLEnum(QuoteStatus, name="quote_status", values_callable=lambda x: [e.value for e in x]),
        default=QuoteStatus.DRAFT, nullable=False)

    def __repr__(self) -> str:
        return f"<Quote(id={self.id}, tenant_id={self.tenant_id}, total={self.total})>"
```

- [ ] **Step 4: Add `Lead.quote_id` and config `annual_discount_pct`**

In `app/db/models/lead.py`, after `meeting_id`:
```python
    quote_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
```
In `app/db/models/sales_agent_config.py`, add the column (after `currency`):
```python
    annual_discount_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Annual-commitment discount percent applied to quotes")
```
Add `Integer` to the sqlalchemy import. In `default_for(...)`, add `annual_discount_pct=0,` to the kwargs. (`pricing_tiers=[]` and `currency="NGN"` already there.)

- [ ] **Step 5: Register model + migration**

Add `from app.db.models.quote import Quote, QuoteStatus` to `app/db/models/__init__.py` (mirror `Meeting`), update `__all__`. Then `poetry run alembic revision -m "add quotes table and pricing config"` and hand-write upgrade/downgrade:

```python
def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("draft", "sent", name="quote_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quotes_tenant_id", "quotes", ["tenant_id"])
    op.create_index("ix_quotes_lead_id", "quotes", ["lead_id"])
    op.create_index("ix_quotes_conversation_id", "quotes", ["conversation_id"])
    op.add_column("leads", sa.Column("quote_id", sa.UUID(), nullable=True))
    op.add_column("tenant_sales_agent_config",
                  sa.Column("annual_discount_pct", sa.Integer(), nullable=False, server_default="0"))
    op.alter_column("tenant_sales_agent_config", "annual_discount_pct", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_sales_agent_config", "annual_discount_pct")
    op.drop_column("leads", "quote_id")
    op.drop_table("quotes")
    sa.Enum(name="quote_status").drop(op.get_bind(), checkfirst=True)
```
Reconcile `created_at`/`updated_at` to the real `TimestampMixin` (read `app/db/base.py`).

- [ ] **Step 6: Apply + test**

Run: `poetry run alembic upgrade head` then `poetry run pytest tests/sales/test_quote_model.py -v`. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/db/models/quote.py app/db/models/lead.py app/db/models/sales_agent_config.py app/db/models/__init__.py alembic/versions/ tests/sales/test_quote_model.py
git commit -m "feat(sales): Quote model + lead.quote_id + annual_discount_pct + migration"
```

---

### Task 2: Pricing engine (pure `compute_quote`)

**Files:**
- Create: `app/sales/pricing.py`
- Test: `tests/sales/test_pricing.py`

**Interfaces:**
- Produces: `compute_quote(pricing_tiers: list[dict], annual_discount_pct: int, currency: str, *, team_size: int, plan_interest: str | None) -> dict | None` → `{currency, line_items: [{label, detail, amount}], total}` or `None` when no tier matches / tiers empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_pricing.py
from app.sales.pricing import compute_quote

TIERS = [
    {"name": "Starter", "base_price": 20000, "min_users": 1, "max_users": 10, "per_seat_overage_price": 2500},
    {"name": "Growth", "base_price": 45000, "min_users": 11, "max_users": 50, "per_seat_overage_price": 3500},
]


def test_base_only_no_overage_no_discount():
    q = compute_quote(TIERS, 0, "NGN", team_size=30, plan_interest="Growth")
    assert q["total"] == 45000
    assert q["line_items"] == [{"label": "Growth", "detail": "11–50 users", "amount": 45000}]


def test_overage_applied():
    q = compute_quote(TIERS, 0, "NGN", team_size=58, plan_interest="Growth")
    # base 45000 + (58-50)*3500 = 45000 + 28000 = 73000
    assert q["total"] == 73000
    labels = [li["label"] for li in q["line_items"]]
    assert "Extra seats (×8)" in labels


def test_discount_applied():
    q = compute_quote(TIERS, 15, "NGN", team_size=58, plan_interest="Growth")
    # subtotal 73000; discount 73000*15//100 = 10950; total 62050
    assert q["total"] == 62050
    disc = [li for li in q["line_items"] if li["amount"] < 0]
    assert disc and disc[0]["amount"] == -10950 and disc[0]["detail"] == "−15%"


def test_match_by_team_size_when_no_plan():
    q = compute_quote(TIERS, 0, "NGN", team_size=5, plan_interest=None)
    assert q["line_items"][0]["label"] == "Starter"


def test_empty_tiers_returns_none():
    assert compute_quote([], 0, "NGN", team_size=10, plan_interest="Growth") is None


def test_no_match_returns_none():
    # team_size out of all ranges and plan name unknown
    assert compute_quote(TIERS, 0, "NGN", team_size=9999, plan_interest="Enterprise") is None


def test_integer_math_no_float():
    q = compute_quote(TIERS, 15, "NGN", team_size=58, plan_interest="Growth")
    assert all(isinstance(li["amount"], int) for li in q["line_items"])
    assert isinstance(q["total"], int)
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_pricing.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# app/sales/pricing.py
"""Pure deterministic quote computation (Stage 3 Slice 4). Integer money math only."""


def _match_tier(tiers: list[dict], team_size: int, plan_interest: str | None) -> dict | None:
    if plan_interest:
        want = plan_interest.strip().lower()
        for t in tiers:
            if str(t.get("name", "")).strip().lower() == want:
                return t
    for t in tiers:
        if int(t.get("min_users", 0)) <= team_size <= int(t.get("max_users", 0)):
            return t
    return None


def compute_quote(
    pricing_tiers: list[dict],
    annual_discount_pct: int,
    currency: str,
    *,
    team_size: int,
    plan_interest: str | None,
) -> dict | None:
    tier = _match_tier(pricing_tiers or [], team_size, plan_interest)
    if tier is None:
        return None

    base = int(tier["base_price"])
    max_users = int(tier["max_users"])
    rate = int(tier.get("per_seat_overage_price", 0))
    overage_seats = max(0, team_size - max_users)
    overage = overage_seats * rate
    subtotal = base + overage
    pct = int(annual_discount_pct or 0)
    discount = subtotal * pct // 100
    total = subtotal - discount

    line_items = [{
        "label": tier["name"],
        "detail": f"{int(tier['min_users'])}–{max_users} users",
        "amount": base,
    }]
    if overage_seats > 0:
        line_items.append({
            "label": f"Extra seats (×{overage_seats})",
            "detail": f"₦{rate:,} / seat",
            "amount": overage,
        })
    if pct > 0 and discount > 0:
        line_items.append({
            "label": "Annual discount",
            "detail": f"−{pct}%",
            "amount": -discount,
        })
    return {"currency": currency, "line_items": line_items, "total": total}
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/sales/test_pricing.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app/sales/pricing.py tests/sales/test_pricing.py
git commit -m "feat(sales): pure deterministic pricing engine"
```

---

### Task 3: Piggyback `offer_quote` signal + `plan_interest` captured

**Files:**
- Modify: `app/sales/intent.py`
- Test: `tests/sales/test_quote_signal.py`

**Interfaces:**
- Produces: `signals.offer_quote: bool` and `captured.plan_interest: str|null` in `SALES_RESPONSE_SCHEMA`; directive guidance to gather team size + plan then set `offer_quote`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_quote_signal.py
import json
from app.sales.intent import SALES_RESPONSE_SCHEMA, parse_sales_output


def test_schema_has_offer_quote_and_plan_interest():
    sig = SALES_RESPONSE_SCHEMA["properties"]["signals"]
    cap = SALES_RESPONSE_SCHEMA["properties"]["captured"]
    assert "offer_quote" in sig["properties"] and "offer_quote" in sig["required"]
    assert "plan_interest" in cap["properties"] and "plan_interest" in cap["required"]
    assert sig["additionalProperties"] is False and cap["additionalProperties"] is False


def test_parse_passes_offer_quote_and_plan_interest():
    raw = json.dumps({
        "reply": "Sure!", "intent": "PRICING_INQUIRY",
        "signals": {"asked_features": False, "requested_demo": False, "mentioned_competitor": False,
                    "ready_to_buy": True, "offer_meeting": False, "offer_quote": True},
        "captured": {"name": None, "email": None, "company": None, "team_size": "50",
                     "industry": None, "plan_interest": "Growth"},
    })
    out = parse_sales_output(raw)
    assert out["signals"]["offer_quote"] is True
    assert out["captured"]["plan_interest"] == "Growth"
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_quote_signal.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `app/sales/intent.py`:
- In `SALES_RESPONSE_SCHEMA["properties"]["signals"]`: add `"offer_quote": {"type": "boolean"}` to `properties` and `"offer_quote"` to `required`.
- In `SALES_RESPONSE_SCHEMA["properties"]["captured"]`: add `"plan_interest": {"type": ["string", "null"]}` to `properties` and `"plan_interest"` to `required`.
- In `build_sales_directive`, append:
```python
"7. For pricing/purchase intent: gather the prospect's team size and which "
"plan they're interested in, then set signals.offer_quote=true once you have "
"enough to quote. Put the plan name in captured.plan_interest.\n"
```
(Keep the Slice 3 `offer_meeting` directive line intact.)

- [ ] **Step 4: Run tests + Slice 2/3 schema tests**

Run: `poetry run pytest tests/sales/test_quote_signal.py tests/sales/test_sales_prompt.py tests/sales/test_offer_meeting_signal.py -v`
Expected: PASS. (If `test_sales_prompt.py::test_schema_has_required_keys` pins the exact signals/captured key sets, UPDATE it to include `offer_quote`/`plan_interest` — it's an intentional schema extension.)

- [ ] **Step 5: Commit**

```bash
git add app/sales/intent.py tests/sales/test_quote_signal.py tests/sales/test_sales_prompt.py
git commit -m "feat(sales): offer_quote signal + plan_interest captured field"
```

---

### Task 4: `QuoteService` + chat orchestration + `meta.quote`

**Files:**
- Create: `app/services/quote_service.py`
- Modify: `app/api/v1/endpoints/chat.py`, `app/schemas/conversation.py`
- Test: `tests/api/v1/test_chat_quote.py`

**Interfaces:**
- Consumes: `compute_quote` (Task 2), `Quote`/`QuoteStatus` (Task 1).
- Produces: `QuoteService(db).create_quote(tenant_id, lead, *, currency, line_items, total) -> Quote` (persists, sets `lead.quote_id`); chat sets `meta.quote` when gated.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/v1/test_chat_quote.py
# Mirror tests/api/v1/test_chat_lead_capture.py for the sales-generation mock + seeding.
# Seed a tenant + TenantSalesAgentConfig with agent_enabled=True, lead_capture_enabled=True,
# quotation_enabled toggled per test, pricing_tiers=[Growth 11-50 base 45000 overage 3500],
# annual_discount_pct=15. Mock generate_sales_response to return intent=PRICING_INQUIRY,
# signals.offer_quote=True, captured.team_size="58", captured.plan_interest="Growth".

def test_quote_in_meta_when_enabled(seed_quote_tenant, sales_mock, client):
    # quotation_enabled=True → response meta.quote present with total 62050, Quote persisted, lead.quote_id set
    ...

def test_no_quote_when_quotation_disabled(...):
    # quotation_enabled=False → "quote" not in meta even though offer_quote=True
    ...

def test_no_quote_when_pricing_unconfigured(...):
    # pricing_tiers=[] → no meta.quote
    ...
```

Write these against the REAL mock/seed fixtures in `test_chat_lead_capture.py` (read it first — reuse `_sales_llm_patch`/`pg_client`/the config seed helper). Assert on `r.json()["message"]["meta"]`.

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/v1/test_chat_quote.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `QuoteService`**

```python
# app/services/quote_service.py
"""QuoteService — persist a computed quotation and link it to the lead (Stage 3 Slice 4)."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus


class QuoteService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_quote(self, tenant_id: UUID, lead: Lead, *, currency: str,
                     line_items: list, total: int) -> Quote:
        quote = Quote(
            tenant_id=tenant_id, lead_id=lead.id, conversation_id=lead.conversation_id,
            currency=currency, line_items=line_items, total=total, status=QuoteStatus.DRAFT,
        )
        self.db.add(quote)
        self.db.flush()
        lead.quote_id = quote.id
        self.db.commit()
        self.db.refresh(quote)
        return quote
```

- [ ] **Step 4: Wire chat orchestration**

In `app/api/v1/endpoints/chat.py`, alongside the Slice 3 `offer_meeting` meta block (inside `if _sales_mode and _sales_meta is not None:`), add a quote block:
```python
            _signals = _sales_meta.get("signals") or {}
            if bool(_signals.get("offer_quote")) and getattr(sales_agent_config, "quotation_enabled", False):
                _ts_raw = (lead.team_size if lead is not None else None) or (_sales_meta.get("captured") or {}).get("team_size")
                _team_size = None
                if _ts_raw is not None:
                    import re as _re
                    _m = _re.search(r"\d+", str(_ts_raw))
                    _team_size = int(_m.group()) if _m else None
                if _team_size is not None:
                    _q = compute_quote(
                        sales_agent_config.pricing_tiers or [],
                        getattr(sales_agent_config, "annual_discount_pct", 0),
                        sales_agent_config.currency,
                        team_size=_team_size,
                        plan_interest=(_sales_meta.get("captured") or {}).get("plan_interest"),
                    )
                    if _q is not None and lead is not None:
                        QuoteService(db).create_quote(
                            session.tenant_id, lead,
                            currency=_q["currency"], line_items=_q["line_items"], total=_q["total"])
                        _message_meta["quote"] = _q
```
Place it INSIDE the existing sales-meta block but ensure `lead` is resolved (the Slice 2/3 lead-orchestration runs earlier in the same block — reuse that `lead`). Import `compute_quote` and `QuoteService` at top. Wrap in the existing try/except so a failure degrades to a plain reply. Server gate authoritative: no `meta.quote` without `quotation_enabled`.

- [ ] **Step 5: Document meta + run tests**

In `app/schemas/conversation.py`, extend the `meta` description to note `'quote'` ({currency, line_items, total}). Run:
`poetry run pytest tests/api/v1/test_chat_quote.py tests/api/v1/test_chat_lead_capture.py tests/sales/ -v`
Expected: new tests PASS; Slice 2/3 tests still green.

- [ ] **Step 6: Commit**

```bash
git add app/services/quote_service.py app/api/v1/endpoints/chat.py app/schemas/conversation.py tests/api/v1/test_chat_quote.py
git commit -m "feat(sales): quote orchestration + meta.quote (gated by quotation_enabled)"
```

---

### Task 5: Full-suite verification

- [ ] **Step 1:** `poetry run alembic upgrade head` on a clean DB — applies.
- [ ] **Step 2:** `poetry run pytest -q -p no:cacheprovider --no-cov` (DATABASE_URL on 5433). Expected: all green; no regression to Slices 1–3.
- [ ] **Step 3:** Commit fixups if any: `git commit -am "test(sales): quotation suite green"`.

---

## Self-Review notes
- Spec §2 model → Task 1. §3 config → Task 1. §4 engine → Task 2. §5 flow → Tasks 3+4. §6 contract → Task 4. §7 invariants (integer math, gate, tenant scope, unchanged support) → Tasks 2/4. §8 testing → per task + Task 5. §10 acceptance covered.
- Names consistent: `Quote`/`QuoteStatus.DRAFT`, `compute_quote`, `QuoteService.create_quote`, `Lead.quote_id`, `annual_discount_pct`, `meta.quote`, `signals.offer_quote`, `captured.plan_interest`.
- Sync SQLAlchemy; integer money math; gate authoritative.
- Implementer MUST: confirm `TimestampMixin` cols for the migration; reuse the real `_sales_llm_patch`/seed fixtures from `test_chat_lead_capture.py` (Task 4); resolve the `lead` variable already in scope in the chat sales block; update `test_sales_prompt.py` key-set assertion if it pins exact sets (Task 3).
