# Stage 3 Slice 1 — Leads Core (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Light up the dormant `Lead` model with a scoring engine, tenant-scoped lead read/list/update endpoints, and a per-tenant Sales Agent config.

**Architecture:** A pure scoring engine (`app/sales/scoring.py`) computes a 0–100 score + a frozen 9-signal breakdown that is persisted on write. A `LeadService` orchestrates list/detail/status/recompute over the existing `Lead` table. A new `TenantSalesAgentConfig` model stores per-tenant sales settings. Two additive routers mount under `/api/v1` (`leads`, `sales`).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, Pydantic v2, Alembic, pytest, Poetry.

## Global Constraints

- API is additive only; stays on `/api/v1`; build as a self-contained module (no breaking changes to existing routes).
- All lead/config endpoints are tenant-scoped via the existing auth dependency. Cross-tenant or missing resource → **404** (`LEAD_NOT_FOUND`) — never 403, never leak existence.
- PII (`name`, `email`) uses the existing `EncryptedString` pattern; decrypt server-side only in responses.
- Currency for Luran's own pricing is **NGN** (`₦`) — cross-cutting rule X-1.
- Tier thresholds are the single source of truth, shared with FE: `cold < 31`, `warm 31–60`, `hot 61–85`, `sales_ready >= 86`.
- Commit messages MUST NOT include a Claude co-author trailer.
- Run `poetry run pytest` for tests; keep the existing suite green.

---

### Task 1: Scoring engine (pure)

**Files:**
- Create: `app/sales/__init__.py`, `app/sales/scoring.py`
- Test: `tests/sales/test_scoring.py`

**Interfaces:**
- Produces:
  - `SIGNALS: list[tuple[str, str, int]]` — `(key, label, weight)`, 9 entries.
  - `TIER_THRESHOLDS` and `tier_for(score: int) -> str` → one of `"cold"|"warm"|"hot"|"sales_ready"`.
  - `score_lead(lead, signals: dict[str, bool] | None = None) -> ScoreResult` where `ScoreResult = TypedDict("ScoreResult", {"score": int, "tier": str, "breakdown": list[dict]})` and each breakdown item is `{"key": str, "label": str, "weight": int, "fired": bool}`.
  - Field-based fired flags derive from the lead; conversation-derived ones (`asked_features`, `requested_demo`, `mentioned_competitor`, `ready_to_buy`) come from `signals`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_scoring.py
import types
from app.sales.scoring import score_lead, tier_for, SIGNALS

def _lead(**kw):
    base = dict(email=None, company=None, team_size=None, industry=None, utm=None)
    base.update(kw)
    return types.SimpleNamespace(**base)

def test_empty_lead_scores_zero_and_lists_all_signals():
    r = score_lead(_lead())
    assert r["score"] == 0
    assert r["tier"] == "cold"
    assert len(r["breakdown"]) == len(SIGNALS) == 9
    assert all(item["fired"] is False for item in r["breakdown"])

def test_field_signals_fire_from_lead_columns():
    r = score_lead(_lead(email="a@b.com", company="Acme", team_size=120,
                         industry="Bank", utm={"medium": "cpc"}))
    fired = {i["key"] for i in r["breakdown"] if i["fired"]}
    assert {"provided_email", "company_name", "team_over_50", "enterprise", "paid_ad"} <= fired
    # 20 + 10 + 15 + 20 + 5 = 70
    assert r["score"] == 70
    assert r["tier"] == "hot"

def test_conversation_signals_come_from_signals_arg():
    r = score_lead(_lead(email="a@b.com"),
                   signals={"requested_demo": True, "ready_to_buy": True})
    fired = {i["key"] for i in r["breakdown"] if i["fired"]}
    assert {"provided_email", "requested_demo", "ready_to_buy"} == fired
    assert r["score"] == 20 + 15 + 25  # 60 → warm

def test_score_caps_at_100():
    r = score_lead(
        _lead(email="a@b.com", company="Acme", team_size=120, industry="Bank",
              utm={"medium": "cpc"}),
        signals={"asked_features": True, "requested_demo": True,
                 "mentioned_competitor": True, "ready_to_buy": True},
    )  # 70 + 10 + 15 + 10 + 25 = 130
    assert r["score"] == 100
    assert r["tier"] == "sales_ready"

def test_tier_boundaries():
    assert tier_for(30) == "cold"
    assert tier_for(31) == "warm"
    assert tier_for(60) == "warm"
    assert tier_for(61) == "hot"
    assert tier_for(85) == "hot"
    assert tier_for(86) == "sales_ready"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/sales/test_scoring.py -v`
Expected: FAIL (`ModuleNotFoundError: app.sales.scoring`).

- [ ] **Step 3: Write minimal implementation**

```python
# app/sales/scoring.py
from typing import TypedDict

SIGNALS: list[tuple[str, str, int]] = [
    ("provided_email",       "Provided email",                20),
    ("company_name",         "Mentioned company name",        10),
    ("team_over_50",         "Team size > 50",                15),
    ("enterprise",           "Enterprise / corporate",        20),
    ("asked_features",       "Asked about specific features", 10),
    ("requested_demo",       "Requested a demo",              15),
    ("mentioned_competitor", "Mentioned a competitor",        10),
    ("ready_to_buy",         'Said "ready to buy"',           25),
    ("paid_ad",              "Came from a paid ad (UTM)",      5),
]

TIER_THRESHOLDS = {"warm": 31, "hot": 61, "sales_ready": 86}
_ENTERPRISE_HINTS = ("bank", "enterprise", "corporate", "corp", "plc", "group")
_PAID_HINTS = ("cpc", "ppc", "paid", "ad", "ads", "adwords", "google_ads")

class ScoreResult(TypedDict):
    score: int
    tier: str
    breakdown: list[dict]

def tier_for(score: int) -> str:
    if score >= TIER_THRESHOLDS["sales_ready"]:
        return "sales_ready"
    if score >= TIER_THRESHOLDS["hot"]:
        return "hot"
    if score >= TIER_THRESHOLDS["warm"]:
        return "warm"
    return "cold"

def _enterprise(lead) -> bool:
    blob = " ".join(filter(None, [getattr(lead, "industry", None), getattr(lead, "company", None)])).lower()
    return any(h in blob for h in _ENTERPRISE_HINTS)

def _paid_ad(lead) -> bool:
    utm = getattr(lead, "utm", None) or {}
    vals = " ".join(str(v).lower() for v in utm.values())
    return any(h in vals for h in _PAID_HINTS)

def score_lead(lead, signals: dict | None = None) -> ScoreResult:
    signals = signals or {}
    field_fired = {
        "provided_email": bool(getattr(lead, "email", None)),
        "company_name": bool(getattr(lead, "company", None)),
        "team_over_50": (getattr(lead, "team_size", None) or 0) > 50,
        "enterprise": _enterprise(lead),
        "paid_ad": _paid_ad(lead),
    }
    breakdown, total = [], 0
    for key, label, weight in SIGNALS:
        fired = field_fired.get(key, bool(signals.get(key, False)))
        if fired:
            total += weight
        breakdown.append({"key": key, "label": label, "weight": weight, "fired": fired})
    score = min(100, total)
    return {"score": score, "tier": tier_for(score), "breakdown": breakdown}
```

Add empty `app/sales/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `poetry run pytest tests/sales/test_scoring.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/sales/__init__.py app/sales/scoring.py tests/sales/test_scoring.py
git commit -m "feat(sales): add lead-scoring engine with 9-signal breakdown"
```

---

### Task 2: `score_breakdown` column + migration

**Files:**
- Modify: `app/db/models/lead.py`
- Create: `alembic/versions/<rev>_add_lead_score_breakdown.py`
- Test: `tests/sales/test_lead_score_column.py`

**Interfaces:**
- Produces: `Lead.score_breakdown: Mapped[dict | None]` (JSON column).

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_lead_score_column.py
import pytest
from app.db.models.lead import Lead

@pytest.mark.asyncio
async def test_lead_persists_score_breakdown(db_session, make_tenant):
    tenant = await make_tenant()
    lead = Lead(tenant_id=tenant.id, email="a@b.com", lead_score=70,
                score_breakdown=[{"key": "provided_email", "weight": 20, "fired": True}])
    db_session.add(lead)
    await db_session.commit()
    await db_session.refresh(lead)
    assert lead.score_breakdown[0]["key"] == "provided_email"
```

(Use the project's existing async DB + tenant fixtures; adjust fixture names to match `tests/conftest.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/sales/test_lead_score_column.py -v`
Expected: FAIL (`AttributeError`/unknown column `score_breakdown`).

- [ ] **Step 3: Add the column + migration**

In `app/db/models/lead.py`, alongside `lead_score`:
```python
from sqlalchemy import JSON
score_breakdown: Mapped[list | None] = mapped_column(JSON, nullable=True)
```
Generate the migration:
```bash
poetry run alembic revision -m "add lead score_breakdown" --autogenerate
```
Verify the upgrade adds `score_breakdown JSON NULL` to `leads` and the downgrade drops it. Hand-correct if autogenerate adds noise.

- [ ] **Step 4: Apply + run test**

Run: `poetry run alembic upgrade head && poetry run pytest tests/sales/test_lead_score_column.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db/models/lead.py alembic/versions/*score_breakdown*.py tests/sales/test_lead_score_column.py
git commit -m "feat(sales): persist frozen lead score_breakdown on Lead"
```

---

### Task 3: LeadService + score-on-capture integration

**Files:**
- Create: `app/services/lead_service.py`
- Modify: `app/api/v1/endpoints/public_auditor.py` (apply score where the Lead is created)
- Test: `tests/sales/test_lead_service.py`

**Interfaces:**
- Consumes: `score_lead` (Task 1), `Lead.score_breakdown` (Task 2).
- Produces:
  - `LeadService.apply_score(lead, signals=None) -> None` — sets `lead.lead_score`, `lead.tier` is derived (not stored), `lead.score_breakdown`.
  - `LeadService.list_leads(tenant_id, *, status=None, intent=None, tier=None, sort="score", limit=50, offset=0) -> tuple[list[Lead], int]`
  - `LeadService.get_lead(tenant_id, lead_id) -> Lead | None`
  - `LeadService.set_status(tenant_id, lead_id, status) -> Lead` (raises `ValueError` on illegal transition)
  - `LeadService.recompute(tenant_id, lead_id) -> Lead`
  - `LeadService.summary(tenant_id) -> dict` (`new_this_week, avg_score, sales_ready_count, pipeline_value=0, status_counts`)

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_lead_service.py
import pytest
from app.services.lead_service import LeadService
from app.db.models.lead import Lead, LeadStatus

@pytest.mark.asyncio
async def test_apply_score_sets_score_and_breakdown(db_session, make_tenant):
    t = await make_tenant()
    lead = Lead(tenant_id=t.id, email="a@b.com", company="Acme", team_size=120, industry="Bank")
    LeadService(db_session).apply_score(lead)
    assert lead.lead_score == 65  # 20+10+15+20
    assert any(b["key"] == "enterprise" and b["fired"] for b in lead.score_breakdown)

@pytest.mark.asyncio
async def test_list_is_tenant_scoped(db_session, make_tenant):
    a, b = await make_tenant(), await make_tenant()
    svc = LeadService(db_session)
    for t in (a, a, b):
        lead = Lead(tenant_id=t.id, email="x@y.com"); svc.apply_score(lead); db_session.add(lead)
    await db_session.commit()
    rows, total = await svc.list_leads(a.id)
    assert total == 2 and all(r.tenant_id == a.id for r in rows)

@pytest.mark.asyncio
async def test_illegal_status_transition_raises(db_session, make_tenant):
    t = await make_tenant()
    lead = Lead(tenant_id=t.id, status=LeadStatus.CONVERTED); db_session.add(lead); await db_session.commit()
    with pytest.raises(ValueError):
        await LeadService(db_session).set_status(t.id, lead.id, LeadStatus.NEW)
```

(`test_apply_score` expects 65: email20+company10+team15+enterprise20. Adjust if the enterprise heuristic on "Acme"/"Bank" differs — "bank" in industry fires it.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_lead_service.py -v`
Expected: FAIL (`ModuleNotFoundError: app.services.lead_service`).

- [ ] **Step 3: Implement `LeadService`**

Implement the methods per the Interfaces block: `apply_score` calls `score_lead` and assigns `lead_score`/`score_breakdown`; `list_leads` builds a tenant-filtered `select(Lead)` with optional `status`/`intent`/`tier` (tier via score range), `sort` (`score`→`lead_score.desc()`, `captured`→`created_at.desc()`), `limit/offset`, returns `(rows, total_count)`; `get_lead` filters by `tenant_id`+`id`; `set_status` validates transitions with an allowed-map `{NEW:{CONTACTED,QUALIFIED,CONVERTED}, CONTACTED:{QUALIFIED,CONVERTED}, QUALIFIED:{CONVERTED}, CONVERTED:set()}` and raises `ValueError` otherwise; `recompute` re-runs `apply_score` and commits; `summary` aggregates counts (use SQL `count`/`avg`, `new_this_week` via `created_at >= now-7d`, `sales_ready_count` via `lead_score >= 86`, `pipeline_value = 0`).

- [ ] **Step 4: Integrate score-on-capture**

In `app/api/v1/endpoints/public_auditor.py`, where `Lead(source="public_auditor", ...)` is created and before commit, call `LeadService(db).apply_score(lead)`.

- [ ] **Step 5: Run tests**

Run: `poetry run pytest tests/sales/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/lead_service.py app/api/v1/endpoints/public_auditor.py tests/sales/test_lead_service.py
git commit -m "feat(sales): LeadService (list/detail/status/score) + score-on-capture"
```

---

### Task 4: Leads list + summary endpoints

**Files:**
- Create: `app/schemas/lead.py`, `app/api/v1/endpoints/leads.py`
- Modify: `app/api/v1/api.py` (mount router)
- Test: `tests/api/test_leads_list.py`

**Interfaces:**
- Consumes: `LeadService` (Task 3).
- Produces routes: `GET /api/v1/leads`, `GET /api/v1/leads/summary`.
- Schemas: `LeadListItem`, `LeadListResponse {items, total, limit, offset}`, `LeadsSummary`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_leads_list.py
import pytest

@pytest.mark.asyncio
async def test_list_returns_only_callers_tenant(client, auth_headers, seed_leads):
    await seed_leads(tenant="me", n=3); await seed_leads(tenant="other", n=2)
    r = await client.get("/api/v1/leads", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["items"][0]["lead_score"] >= body["items"][-1]["lead_score"]  # score desc

@pytest.mark.asyncio
async def test_summary_shape(client, auth_headers, seed_leads):
    await seed_leads(tenant="me", n=5)
    r = await client.get("/api/v1/leads/summary", headers=auth_headers)
    b = r.json()
    assert {"new_this_week", "avg_score", "sales_ready_count", "pipeline_value", "status_counts"} <= b.keys()
    assert b["pipeline_value"] == 0
```

(Use the project's API test fixtures: `client`, `auth_headers`. Add a `seed_leads` fixture in this test file or `conftest.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/test_leads_list.py -v`
Expected: FAIL (404 — route not mounted).

- [ ] **Step 3: Implement schemas + router**

`app/schemas/lead.py`: define `LeadListItem` (`id, name, company, intent, lead_score, tier, status, source_label, created_at`), `LeadListResponse`, `LeadsSummary`. Add a `source_label` helper deriving from `source` + `utm`. `app/api/v1/endpoints/leads.py`: `router = APIRouter()`; `GET /` → `LeadService.list_leads(...)` with query params `status, intent, tier, sort, limit, offset`; `GET /summary` → `LeadService.summary(...)`. Wrap in the existing response envelope if the codebase uses one. Mount in `app/api/v1/api.py`: `api_router.include_router(leads.router, prefix="/leads", tags=["leads"])`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/api/test_leads_list.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/lead.py app/api/v1/endpoints/leads.py app/api/v1/api.py tests/api/test_leads_list.py
git commit -m "feat(sales): GET /leads list + /leads/summary endpoints"
```

---

### Task 5: Lead detail + status patch + recompute

**Files:**
- Modify: `app/schemas/lead.py`, `app/api/v1/endpoints/leads.py`
- Test: `tests/api/test_leads_detail.py`

**Interfaces:**
- Produces: `GET /api/v1/leads/{id}`, `PATCH /api/v1/leads/{id}`, `POST /api/v1/leads/{id}/score`.
- Schemas: `LeadDetail` (list fields + `email, team_size, industry, conversation_id, score_breakdown, source{landing_page, utm, channel, first_seen}, meeting=None, quote=None, crm{connected:false}`), `LeadStatusPatch {status}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_leads_detail.py
import pytest

@pytest.mark.asyncio
async def test_detail_cross_tenant_is_404(client, auth_headers, seed_leads):
    other = (await seed_leads(tenant="other", n=1))[0]
    r = await client.get(f"/api/v1/leads/{other.id}", headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "LEAD_NOT_FOUND"  # adapt to envelope

@pytest.mark.asyncio
async def test_detail_breakdown_sums_to_score(client, auth_headers, seed_leads):
    lead = (await seed_leads(tenant="me", n=1))[0]
    b = (await client.get(f"/api/v1/leads/{lead.id}", headers=auth_headers)).json()
    fired = sum(s["weight"] for s in b["score_breakdown"] if s["fired"])
    assert min(100, fired) == b["lead_score"]

@pytest.mark.asyncio
async def test_patch_status_and_illegal_transition(client, auth_headers, seed_leads):
    lead = (await seed_leads(tenant="me", n=1, status="NEW"))[0]
    ok = await client.patch(f"/api/v1/leads/{lead.id}", json={"status": "CONTACTED"}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["status"] == "CONTACTED"
    bad = await client.patch(f"/api/v1/leads/{lead.id}", json={"status": "NEW"}, headers=auth_headers)
    assert bad.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/test_leads_detail.py -v`
Expected: FAIL (404 route / missing schema).

- [ ] **Step 3: Implement**

Add `LeadDetail`, `LeadStatusPatch` to `app/schemas/lead.py`. In the router: `GET /{lead_id}` → `LeadService.get_lead`; if None → `HTTPException(404, {"code": "LEAD_NOT_FOUND"})` (match the auditor entry-detail handler's error shape). If `lead_score is None`, call `apply_score` + commit before serializing (lazy backfill). `PATCH /{lead_id}` → `set_status`, mapping `ValueError` → `HTTPException(422, {"code": "INVALID_STATUS_TRANSITION"})`. `POST /{lead_id}/score` → `recompute`. All return `LeadDetail`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/api/test_leads_detail.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/lead.py app/api/v1/endpoints/leads.py tests/api/test_leads_detail.py
git commit -m "feat(sales): GET/PATCH /leads/{id} + recompute endpoint"
```

---

### Task 6: `TenantSalesAgentConfig` model + migration

**Files:**
- Create: `app/db/models/sales_agent_config.py`, `alembic/versions/<rev>_create_sales_agent_config.py`
- Modify: `app/db/models/__init__.py`
- Test: `tests/sales/test_sales_config_model.py`

**Interfaces:**
- Produces: `TenantSalesAgentConfig` with the fields in spec §2.2 and a `DEFAULTS` dict / `default_for(tenant_id)` classmethod.

- [ ] **Step 1: Write the failing test**

```python
# tests/sales/test_sales_config_model.py
import pytest
from app.db.models.sales_agent_config import TenantSalesAgentConfig

@pytest.mark.asyncio
async def test_config_round_trips(db_session, make_tenant):
    t = await make_tenant()
    cfg = TenantSalesAgentConfig.default_for(t.id)
    db_session.add(cfg); await db_session.commit(); await db_session.refresh(cfg)
    assert cfg.currency == "NGN"
    assert "PRICING_INQUIRY" in cfg.trigger_intents
    assert cfg.notify_channels["email"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_sales_config_model.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement model + migration**

Create the model per spec §2.2 (unique `tenant_id`, JSON columns, `currency` default `"NGN"`, `default_for(tenant_id)` returning a populated instance with the 3 PRD qualification questions and default trigger intents). Register it in `app/db/models/__init__.py`. Generate + verify migration:
```bash
poetry run alembic revision -m "create sales_agent_config" --autogenerate
poetry run alembic upgrade head
```

- [ ] **Step 4: Run test**

Run: `poetry run pytest tests/sales/test_sales_config_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db/models/sales_agent_config.py app/db/models/__init__.py alembic/versions/*sales_agent_config*.py tests/sales/test_sales_config_model.py
git commit -m "feat(sales): TenantSalesAgentConfig model + migration"
```

---

### Task 7: Sales config GET/PATCH endpoints

**Files:**
- Create: `app/schemas/sales_config.py`, `app/api/v1/endpoints/sales.py`
- Modify: `app/api/v1/api.py`
- Test: `tests/api/test_sales_config.py`

**Interfaces:**
- Consumes: `TenantSalesAgentConfig` (Task 6).
- Produces: `GET /api/v1/sales/config`, `PATCH /api/v1/sales/config`. Schemas `SalesConfig` (read), `SalesConfigPatch` (partial).

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_sales_config.py
import pytest

@pytest.mark.asyncio
async def test_get_returns_default_then_patch_persists(client, auth_headers):
    g = await client.get("/api/v1/sales/config", headers=auth_headers)
    assert g.status_code == 200 and g.json()["currency"] == "NGN"
    p = await client.patch("/api/v1/sales/config",
                           json={"agent_enabled": True, "pricing_tiers": [{"name": "Growth", "price": 45000, "unit": "mo", "range": "11–50 users"}]},
                           headers=auth_headers)
    assert p.status_code == 200 and p.json()["agent_enabled"] is True
    g2 = await client.get("/api/v1/sales/config", headers=auth_headers)
    assert g2.json()["pricing_tiers"][0]["price"] == 45000
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/api/test_sales_config.py -v`
Expected: FAIL (404).

- [ ] **Step 3: Implement**

`app/schemas/sales_config.py`: `SalesConfig` (full mirror), `SalesConfigPatch` (all-optional). `app/api/v1/endpoints/sales.py`: `GET /config` → load tenant's row or return `TenantSalesAgentConfig.default_for(tenant_id)` serialized (do not persist on GET); `PATCH /config` → upsert (create from default if none, apply provided fields, commit). Mount: `api_router.include_router(sales.router, prefix="/sales", tags=["sales"])`.

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/api/test_sales_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/sales_config.py app/api/v1/endpoints/sales.py app/api/v1/api.py tests/api/test_sales_config.py
git commit -m "feat(sales): GET/PATCH /sales/config endpoints"
```

---

### Task 8: Full-suite green + OpenAPI sanity

**Files:** none new (verification task).

- [ ] **Step 1: Run the full backend suite**

Run: `poetry run pytest`
Expected: all green (new + existing).

- [ ] **Step 2: Verify routes registered**

Run a quick check that `/api/v1/leads`, `/api/v1/leads/summary`, `/api/v1/leads/{id}`, `/api/v1/sales/config` appear in the OpenAPI schema (e.g. a small test hitting `/api/v1/openapi.json` or `app.routes`).

- [ ] **Step 3: Commit (if any fixups)**

```bash
git commit -am "test(sales): full-suite green for Leads Core backend"
```

---

## Self-Review notes
- Spec §3 scoring → Task 1. §2.1 column → Task 2. §5 score-on-capture + §4 list/summary/detail/patch/recompute → Tasks 3–5. §2.2 config model → Task 6. §4 config endpoints → Task 7. §7 testing distributed across tasks; §8 verification → Task 8. All spec sections covered.
- Tier thresholds, signal keys, and `LeadService` method names are consistent across tasks.
- Adjust fixture names (`make_tenant`, `client`, `auth_headers`, `db_session`, `seed_leads`) to the repo's actual `tests/conftest.py` during Task 3/4 — the first API task should add a `seed_leads` helper if none exists.
