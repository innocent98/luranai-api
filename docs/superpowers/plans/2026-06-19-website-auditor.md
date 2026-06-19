# AI Website Auditor (Stage 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a self-contained AI Website Auditor that crawls a business URL, synthesizes a `BusinessProfile`, adaptively classifies pages, detects content gaps, deterministically scores AI-readiness, and (dashboard-only) imports staged entries into the existing RAG knowledge base — running identically as a public 50-page lead tool and a plan-capped dashboard tool.
**Architecture:** Logic lives in a cohesive `app/auditor/` package; staging is isolated in new `AuditReport`/`AuditEntry` tables that never touch RAG until explicit import; a single multi-phase ARQ job (`run_website_audit`) streams progress through the existing `ProcessingJob` channel; all LLM calls route through a task-named `app/auditor/llm.py` seam; the existing `RAGService.assemble_prompt` is touched only by an additive, guarded "About this business" block at chat time.
**Tech Stack:** Python, FastAPI, SQLAlchemy (sync `Session`), Alembic, ARQ, Playwright, Qdrant, Postgres, OpenAI

## Global Constraints

- Branch `feat/stage4-website-auditor` off `develop`.
- Per-task commits (user decision 2026-06-19): each task ENDS WITH A COMMIT on `feat/stage4-website-auditor` (message `feat(auditor): <task summary>`); all per-task commits are kept in the final PR (granular history). Do NOT `git push` until the end; open the PR after the final task. Where a task's "Step 5" says `git add`, do a `git commit` instead.
- NO `Co-Authored-By: Claude` trailer (and no "Generated with Claude Code" line) in any commit or PR body.
- All `SQLEnum` columns use `values_callable=lambda x: [e.value for e in x]` (mirror `app/db/models/automation.py`).
- PII (`Lead.email`, `Lead.name`, `AuditReport.email`) uses `EncryptedString` + a deterministic `email_hash` (SHA256 of lowercased email) for lookup.
- Reuse `app.utils.network_security.resolve_and_validate_host` on the root URL AND every discovered URL before any fetch.
- All auditor LLM calls route through `app/auditor/llm.py` (task → model: `profile_synthesis`/`gap_analysis`/`expected_map` → gpt-4o; `page_classify` → gpt-4o-mini).
- Auditor makes NO currency assumption — extracted pricing stores currency exactly as found, in `AuditEntry.meta`.
- `kind` → `source_type` map (pure dict): `faq→faq`, `guide→help_doc`, `policy→policy`, `product|pricing|contact|testimonial→other`.
- Public crawl scope = 50 pages (`AUDITOR_PUBLIC_MAX_PAGES`); dashboard scope = `TenantCapabilities.max_audit_pages_per_crawl`.
- Claim staleness window = 7 days (`AUDITOR_CLAIM_STALENESS_DAYS`); unclaimed purge = 30 days (`AUDITOR_UNCLAIMED_PURGE_DAYS`).
- Service layer is SYNC `Session`; crawler/LLM/embedding calls are async. The ARQ job (`async def fn(ctx, ...)`) opens its own `SessionLocal()` exactly like `app/workers/document_processor.py`.
- Error envelope on every endpoint: `{"success": false, "error": {"code": "UPPER_SNAKE", "message": "...", "details": {...}}}` — never leak stack traces.
- **Migration chaining:** the repo has a single, verified Alembic head `2b3514240056` (confirmed 2026-06-19 by static graph parse of all 57 version files — fully converged, no unmerged heads, no merge migration needed). This plan's first new migration chains its `down_revision` off `2b3514240056`; each subsequent migration chains off the previous one in this plan. Before writing ANY migration, run `alembic heads` to re-confirm a single head, then chain off it. Do NOT add a merge migration. Hand-review every autogenerate.

---

### Task 1: Dependencies + config settings
**Files:**
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/pyproject.toml`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/core/config.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.AUDITOR_PUBLIC_MAX_PAGES: int`, `settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES: int`, `settings.AUDITOR_CLAIM_STALENESS_DAYS: int`, `settings.AUDITOR_UNCLAIMED_PURGE_DAYS: int`, `settings.AUDITOR_PER_PAGE_TIMEOUT_S: int`, `settings.AUDITOR_CRAWL_RPS: int`, `settings.AUDITOR_MODEL_PROFILE: str`, `settings.AUDITOR_MODEL_CLASSIFY: str`, `settings.AUDITOR_MODEL_GAPS: str`. New deps `playwright`, `trafilatura`, `beautifulsoup4` importable.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_config.py
from app.core.config import settings


def test_auditor_settings_defaults():
    assert settings.AUDITOR_PUBLIC_MAX_PAGES == 50
    assert settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES == 500
    assert settings.AUDITOR_CLAIM_STALENESS_DAYS == 7
    assert settings.AUDITOR_UNCLAIMED_PURGE_DAYS == 30
    assert settings.AUDITOR_PER_PAGE_TIMEOUT_S == 10
    assert settings.AUDITOR_CRAWL_RPS == 1
    assert settings.AUDITOR_MODEL_PROFILE == "gpt-4o"
    assert settings.AUDITOR_MODEL_CLASSIFY == "gpt-4o-mini"
    assert settings.AUDITOR_MODEL_GAPS == "gpt-4o"


def test_auditor_dependencies_importable():
    import bs4  # noqa: F401
    import trafilatura  # noqa: F401
    from playwright.async_api import async_playwright  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_config.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'AUDITOR_PUBLIC_MAX_PAGES'` (and/or `ModuleNotFoundError: No module named 'trafilatura'`).

- [ ] **Step 3: Write minimal implementation**
Add to `pyproject.toml` `[project].dependencies` (or the equivalent dependency table the file uses): `"playwright>=1.44"`, `"trafilatura>=1.9"`, `"beautifulsoup4>=4.12"`. Then install: `pip install playwright trafilatura beautifulsoup4 && python -m playwright install chromium`.
Add to `app/core/config.py` inside `class Settings`, immediately after the `AUTOMATION_OUTBOUND_HOST_ALLOWLIST` field (before `get_trial_expiry_reminder_days`):
```python
    # AI Website Auditor (Stage 4)
    AUDITOR_PUBLIC_MAX_PAGES: int = 50
    AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES: int = 500
    AUDITOR_CLAIM_STALENESS_DAYS: int = 7
    AUDITOR_UNCLAIMED_PURGE_DAYS: int = 30
    AUDITOR_PER_PAGE_TIMEOUT_S: int = 10
    AUDITOR_CRAWL_RPS: int = 1
    AUDITOR_MODEL_PROFILE: str = "gpt-4o"
    AUDITOR_MODEL_CLASSIFY: str = "gpt-4o-mini"
    AUDITOR_MODEL_GAPS: str = "gpt-4o"
```
Create empty `tests/auditor/__init__.py` and `app/auditor/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_config.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage changes**
Run: `git add pyproject.toml app/core/config.py app/auditor/__init__.py tests/auditor/`

---

### Task 2: Plan capability columns — `max_audit_pages_per_crawl`, `max_audits_per_month`
**Files:**
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/tenant.py` (`TenantCapabilities`)
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/subscription_plan.py` (`SubscriptionPlan`)
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/scripts/seed_data.py`
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/alembic/versions/<rev>_add_auditor_capability_columns.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_plan_capabilities.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TenantCapabilities.max_audit_pages_per_crawl: int | None`, `TenantCapabilities.max_audits_per_month: int | None`, `SubscriptionPlan.max_audit_pages_per_crawl: int | None`, `SubscriptionPlan.max_audits_per_month: int | None`. Per-tier seed values used by later tasks: trial=50/2, lite=200/5, professional=500/15, growth=500/30, enterprise=None/None.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_plan_capabilities.py
import uuid

from app.db.models.subscription_plan import SubscriptionPlan
from app.db.models.tenant import TenantCapabilities


def test_capability_columns_exist(db):
    cap = TenantCapabilities(
        tenant_id=uuid.uuid4(),
        max_audit_pages_per_crawl=200,
        max_audits_per_month=5,
    )
    db.add(cap)
    db.flush()
    assert cap.max_audit_pages_per_crawl == 200
    assert cap.max_audits_per_month == 5


def test_plan_columns_exist_and_serialize(db):
    plan = SubscriptionPlan(
        tier="unittest_tier",
        display_name="Unit",
        description="x",
        monthly_price_kobo=0,
        annual_price_kobo=0,
        max_response_tokens=500,
        max_audit_pages_per_crawl=500,
        max_audits_per_month=15,
    )
    db.add(plan)
    db.flush()
    assert plan.get_capabilities()["max_audit_pages_per_crawl"] == 500
    assert plan.get_capabilities()["max_audits_per_month"] == 15
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_plan_capabilities.py -q`
Expected: FAIL — `TypeError: 'max_audit_pages_per_crawl' is an invalid keyword argument for TenantCapabilities`.

- [ ] **Step 3: Write minimal implementation**
In `app/db/models/tenant.py`, add to `TenantCapabilities` after `max_webhooks_per_flow`:
```python
    max_audit_pages_per_crawl: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max pages an authed audit crawl may scan (null = unlimited)",
    )
    max_audits_per_month: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max dashboard audits per calendar month (null = unlimited)",
    )
```
In `app/db/models/subscription_plan.py`, add to `SubscriptionPlan` after `max_webhooks_per_flow`:
```python
    max_audit_pages_per_crawl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_audits_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
```
Add both keys to the dicts returned by `to_dict()["capabilities"]` and `get_capabilities()`:
```python
            "max_audit_pages_per_crawl": self.max_audit_pages_per_crawl,
            "max_audits_per_month": self.max_audits_per_month,
```
In `scripts/seed_data.py`, add to each tier dict: trial `"max_audit_pages_per_crawl": 50, "max_audits_per_month": 2`; lite `200, 5`; professional `500, 15`; growth `500, 30`; enterprise `None, None`.
Create the migration (after running `alembic heads` to confirm the single head `2b3514240056`; see Global Constraints). This is the first new migration, so its `down_revision` is `"2b3514240056"`. Mirror `z3a4b5c6d7e8`:
```python
"""add auditor capability columns

Revision ID: <rev>
Revises: <resolved_head>
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "<rev>"
down_revision = "<resolved_head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscription_plans", sa.Column("max_audit_pages_per_crawl", sa.Integer(), nullable=True))
    op.add_column("subscription_plans", sa.Column("max_audits_per_month", sa.Integer(), nullable=True))
    op.add_column("tenant_capabilities", sa.Column("max_audit_pages_per_crawl", sa.Integer(), nullable=True))
    op.add_column("tenant_capabilities", sa.Column("max_audits_per_month", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE subscription_plans SET
            max_audit_pages_per_crawl = CASE tier
                WHEN 'trial' THEN 50 WHEN 'lite' THEN 200
                WHEN 'professional' THEN 500 WHEN 'growth' THEN 500 ELSE NULL END,
            max_audits_per_month = CASE tier
                WHEN 'trial' THEN 2 WHEN 'lite' THEN 5
                WHEN 'professional' THEN 15 WHEN 'growth' THEN 30 ELSE NULL END
        """
    )
    op.execute(
        """
        UPDATE tenant_capabilities tc
        SET max_audit_pages_per_crawl = sp.max_audit_pages_per_crawl,
            max_audits_per_month = sp.max_audits_per_month
        FROM tenants t
        JOIN subscription_plans sp ON sp.tier = t.subscription_tier
        WHERE tc.tenant_id = t.id
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_capabilities", "max_audits_per_month")
    op.drop_column("tenant_capabilities", "max_audit_pages_per_crawl")
    op.drop_column("subscription_plans", "max_audits_per_month")
    op.drop_column("subscription_plans", "max_audit_pages_per_crawl")
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_plan_capabilities.py -q && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: PASS (2 passed); migration round-trips cleanly.

- [ ] **Step 5: Stage changes**
Run: `git add app/db/models/tenant.py app/db/models/subscription_plan.py scripts/seed_data.py alembic/versions/ tests/auditor/test_plan_capabilities.py`

---

### Task 3: `BusinessProfile` model + migration
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/business_profile.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/__init__.py` (register for metadata)
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/alembic/versions/<rev>_create_business_profiles.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_business_profile_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `BusinessProfile` (`__tablename__ = "business_profiles"`), `BusinessProfileSource` enum (`AUDITOR="auditor"`, `MANUAL="manual"`). Columns: `tenant_id` (FK unique), `summary`, `industry`, `offerings` (json), `audience` (json), `positioning`, `key_entities` (json), `tone` (json), `source` (enum), `confidence` (float), `model_version` (str), `raw` (json), `refreshed_at`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_business_profile_model.py
import uuid

from app.db.models.business_profile import BusinessProfile, BusinessProfileSource


def test_business_profile_persists(db):
    bp = BusinessProfile(
        tenant_id=uuid.uuid4(),
        summary="A Lagos fintech",
        industry="fintech",
        offerings=["wallet", "bills"],
        audience=["SMEs"],
        positioning="fast and local",
        key_entities=["LuranPay"],
        tone={"style": "warm"},
        source=BusinessProfileSource.AUDITOR,
        confidence=0.82,
        model_version="gpt-4o",
        raw={"k": "v"},
    )
    db.add(bp)
    db.flush()
    db.refresh(bp)
    assert bp.id is not None
    assert bp.source.value == "auditor"
    assert bp.offerings == ["wallet", "bills"]
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_business_profile_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db.models.business_profile'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/db/models/business_profile.py
"""BusinessProfile model — per-tenant synthesized 'what this business is/does'."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class BusinessProfileSource(str, enum.Enum):
    AUDITOR = "auditor"
    MANUAL = "manual"


class BusinessProfile(Base, UUIDMixin, TimestampMixin):
    """One synthesized business profile per tenant. Not PII -> not encrypted."""

    __tablename__ = "business_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    offerings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    audience: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    positioning: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_entities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tone: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[BusinessProfileSource] = mapped_column(
        SQLEnum(
            BusinessProfileSource,
            name="business_profile_source",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=BusinessProfileSource.AUDITOR,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<BusinessProfile(tenant_id={self.tenant_id}, industry={self.industry})>"
```
Register the model in `app/db/models/__init__.py` (add `from app.db.models.business_profile import BusinessProfile, BusinessProfileSource` alongside existing model imports so `Base.metadata` includes it). Create the migration via `op.create_table("business_profiles", ...)` mirroring `0c1be6a3a289` style (columns above; `sa.Enum(..., name="business_profile_source")`; FK to `tenants.id` with `ondelete="CASCADE"`; unique index on `tenant_id`; `id` UUID PK; `created_at`/`updated_at`/`refreshed_at` with `server_default=sa.func.now()`). Chain `down_revision` off the head from Task 2.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_business_profile_model.py -q && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: PASS (1 passed); migration round-trips cleanly.

- [ ] **Step 5: Stage changes**
Run: `git add app/db/models/business_profile.py app/db/models/__init__.py alembic/versions/ tests/auditor/test_business_profile_model.py`

---

### Task 4: `AuditReport` + `AuditEntry` models + enums + migration
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/audit.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/__init__.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/job.py` (make `ProcessingJob.tenant_id` nullable)
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/alembic/versions/<rev>_create_audit_reports_entries.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_audit_models.py`

> Note: a model named `app.db.models.audit` (AnalyticsMetric) is imported in `document_processor.py` as `from app.db.models.audit import AnalyticsMetric`. CHECK FIRST: if `app/db/models/audit.py` already exists, ADD the new classes to it rather than overwriting. If it does not (AnalyticsMetric lives elsewhere), create the file fresh. Run `python -c "import app.db.models.audit"` and `grep -rn "class AnalyticsMetric" app/db/models/` before writing.

**Interfaces:**
- Consumes: `BusinessProfile` (FK), `ProcessingJob` (FK).
- Produces: enums `AuditScope` (`PUBLIC="public"`, `DASHBOARD="dashboard"`), `AuditStatus` (`QUEUED="queued"`, `CRAWLING="crawling"`, `ANALYZING="analyzing"`, `COMPLETED="completed"`, `FAILED="failed"`), `AuditEntryStatus` (`PROPOSED="proposed"`, `ACTIVE="active"`, `LOCKED="locked"`, `SAVED="saved"`, `IMPORTED="imported"`, `DISCARDED="discarded"`), `AuditEntryKind` (`FAQ="faq"`, `POLICY="policy"`, `PRODUCT="product"`, `PRICING="pricing"`, `GUIDE="guide"`, `CONTACT="contact"`, `TESTIMONIAL="testimonial"`, `OTHER="other"`). Models `AuditReport` (`audit_reports`), `AuditEntry` (`audit_entries`) with relationship `AuditReport.entries`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_audit_models.py
import uuid

from app.db.models.audit import (
    AuditEntry,
    AuditEntryKind,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)


def test_audit_report_and_entry_persist(db):
    report = AuditReport(
        url="https://example.com",
        domain="example.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.QUEUED,
        claim_token="tok_abc",
    )
    db.add(report)
    db.flush()
    entry = AuditEntry(
        audit_report_id=report.id,
        url="https://example.com/faq",
        title="FAQ",
        category="customer onboarding faq",
        kind=AuditEntryKind.FAQ,
        content="Q: ... A: ...",
        content_hash="a" * 64,
        importance_rank=1,
        status=AuditEntryStatus.PROPOSED,
        meta={"currency": "NGN"},
    )
    db.add(entry)
    db.flush()
    db.refresh(report)
    assert report.scope.value == "public"
    assert report.entries[0].kind.value == "faq"
    assert report.entries[0].status.value == "proposed"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_audit_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'AuditReport' from 'app.db.models.audit'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/db/models/audit.py  (append to existing file if AnalyticsMetric lives here)
"""Website Auditor staging models: AuditReport + AuditEntry."""

import enum
import uuid

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class AuditScope(str, enum.Enum):
    PUBLIC = "public"
    DASHBOARD = "dashboard"


class AuditStatus(str, enum.Enum):
    QUEUED = "queued"
    CRAWLING = "crawling"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class AuditEntryStatus(str, enum.Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    LOCKED = "locked"
    SAVED = "saved"
    IMPORTED = "imported"
    DISCARDED = "discarded"


class AuditEntryKind(str, enum.Enum):
    FAQ = "faq"
    POLICY = "policy"
    PRODUCT = "product"
    PRICING = "pricing"
    GUIDE = "guide"
    CONTACT = "contact"
    TESTIMONIAL = "testimonial"
    OTHER = "other"


def _enum(e, name):
    return SQLEnum(e, name=name, values_callable=lambda x: [m.value for m in x])


class AuditReport(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "audit_reports"
    __table_args__ = (
        Index("ix_audit_reports_tenant_status", "tenant_id", "status"),
        Index("ix_audit_reports_claim_token", "claim_token"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[AuditScope] = mapped_column(_enum(AuditScope, "audit_scope"), nullable=False)
    status: Mapped[AuditStatus] = mapped_column(
        _enum(AuditStatus, "audit_status"), default=AuditStatus.QUEUED, nullable=False, index=True
    )
    pages_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    business_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_profiles.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    support_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projected_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gaps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    imported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    imported_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    entries: Mapped[list["AuditEntry"]] = relationship(
        "AuditEntry", back_populates="report", cascade="all, delete-orphan"
    )


class AuditEntry(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_entries_report_status", "audit_report_id", "status"),
        Index("ix_audit_entries_report_hash", "audit_report_id", "content_hash"),
    )

    audit_report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audit_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[AuditEntryKind] = mapped_column(
        _enum(AuditEntryKind, "audit_entry_kind"), default=AuditEntryKind.OTHER, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    importance_rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[AuditEntryStatus] = mapped_column(
        _enum(AuditEntryStatus, "audit_entry_status"),
        default=AuditEntryStatus.PROPOSED,
        nullable=False,
        index=True,
    )
    knowledge_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    report: Mapped["AuditReport"] = relationship("AuditReport", back_populates="entries")
```
Register both in `app/db/models/__init__.py`. Create migration with `op.create_table("audit_reports", ...)` then `op.create_table("audit_entries", ...)` and the two `Index` definitions per table, mirroring `0c1be6a3a289`. The `leads.id` FK is forward-referenced — Task 5 creates `leads`; to keep this migration self-consistent, make `lead_id` a plain UUID column here and ADD the FK constraint in Task 5's migration (`op.create_foreign_key(...)`), OR order Task 5's migration before this one. Choose: create `audit_reports.lead_id` as nullable UUID with NO FK in this migration; add the FK in Task 5. Chain `down_revision` off Task 3's head.

- [ ] **Step 3b: Write the failing test for nullable `ProcessingJob.tenant_id`**
Rationale: public (pre-signup) audits have no tenant but still need a `ProcessingJob` progress row; the WS/progress channel keys on `job_id`, not `tenant_id`. So `ProcessingJob.tenant_id` must be nullable (Task 15's `start_audit` passes `tenant_id=None`).
```python
# append to tests/auditor/test_audit_models.py
from app.db.models.job import JobStatus, JobType, ProcessingJob


def test_processing_job_allows_null_tenant(db):
    job = ProcessingJob(
        tenant_id=None,
        job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED,
        input_data={"kind": "website_audit"},
    )
    db.add(job)
    db.flush()
    db.refresh(job)
    assert job.id is not None
    assert job.tenant_id is None
```
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_audit_models.py::test_processing_job_allows_null_tenant -q`
Expected: FAIL — `IntegrityError`/`NOT NULL constraint` on `processing_jobs.tenant_id` (column is currently `nullable=False`).

- [ ] **Step 3c: Make `ProcessingJob.tenant_id` nullable (model + migration)**
In `app/db/models/job.py`, change the `tenant_id` column from `nullable=False` to `nullable=True`:
```python
    tenant_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
```
Add to the upgrade/downgrade of THIS task's migration (the `create_audit_reports_entries` migration — import `from sqlalchemy.dialects import postgresql` at the top):
```python
def upgrade() -> None:
    # ... existing create_table calls ...
    op.alter_column(
        "processing_jobs", "tenant_id",
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "processing_jobs", "tenant_id",
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )
    # ... existing drop_table calls ...
```

- [ ] **Step 3d: Run the nullable-tenant test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && alembic upgrade head && pytest tests/auditor/test_audit_models.py::test_processing_job_allows_null_tenant -q`
Expected: PASS (1 passed).

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_audit_models.py -q && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: PASS (1 passed); migration round-trips cleanly.

- [ ] **Step 5: Stage changes**
Run: `git add app/db/models/audit.py app/db/models/job.py app/db/models/__init__.py alembic/versions/ tests/auditor/test_audit_models.py`

---

### Task 5: `Lead` model + migration (full Stage 3 schema)
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/lead.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/db/models/__init__.py`
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/alembic/versions/<rev>_create_leads.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_lead_model.py`

**Interfaces:**
- Consumes: `AuditReport` (FK back-reference via `audit_report_id`).
- Produces: `Lead` (`leads`), `LeadStatus` enum (`NEW="new"`, `CONTACTED="contacted"`, `QUALIFIED="qualified"`, `CONVERTED="converted"`). PII columns `name`/`email` via `EncryptedString`, plus `email_hash` (indexed). Helper `Lead.compute_email_hash(email: str) -> str` (staticmethod, SHA256 of `email.strip().lower()`).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_lead_model.py
import uuid

from app.db.models.lead import Lead, LeadStatus


def test_lead_persists_with_encrypted_pii_and_hash(db):
    h = Lead.compute_email_hash("Owner@Example.com")
    lead = Lead(
        tenant_id=None,
        name="Ada Owner",
        email="Owner@Example.com",
        email_hash=h,
        source="public_auditor",
        utm={"utm_source": "x"},
        status=LeadStatus.NEW,
    )
    db.add(lead)
    db.flush()
    db.refresh(lead)
    assert lead.id is not None
    assert lead.email == "Owner@Example.com"  # decrypts transparently
    assert lead.email_hash == Lead.compute_email_hash("owner@example.com")
    assert lead.status.value == "new"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_lead_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db.models.lead'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/db/models/lead.py
"""Lead model — full Stage 3 schema; auditor populates a subset."""

import enum
import hashlib
import uuid

from sqlalchemy import JSON, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.db.base import Base, TimestampMixin, UUIDMixin


class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CONVERTED = "converted"


class Lead(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_tenant_email_hash", "tenant_id", "email_hash"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    name: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(
        SQLEnum(LeadStatus, name="lead_status", values_callable=lambda x: [e.value for e in x]),
        default=LeadStatus.NEW,
        nullable=False,
    )
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    utm: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    crm_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    audit_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_reports.id", ondelete="SET NULL"), nullable=True
    )

    @staticmethod
    def compute_email_hash(email: str) -> str:
        return hashlib.sha256(email.strip().lower().encode()).hexdigest()
```
Register in `app/db/models/__init__.py`. Create migration: `op.create_table("leads", ...)` (mirror `0c1be6a3a289`), then `op.create_foreign_key("fk_audit_reports_lead_id", "audit_reports", "leads", ["lead_id"], ["id"], ondelete="SET NULL")` to close the forward reference from Task 4. `down_revision` chains off Task 4's head; `downgrade` drops the FK then the table.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_lead_model.py -q && alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: PASS (1 passed); migration round-trips cleanly.

- [ ] **Step 5: Stage changes**
Run: `git add app/db/models/lead.py app/db/models/__init__.py alembic/versions/ tests/auditor/test_lead_model.py`

---

### Task 6: `app/auditor/llm.py` — task-named model seam
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/llm.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_llm_seam.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_MODEL_*`, `settings.OPENAI_API_KEY`.
- Produces: `async def complete_json(task: str, system: str, user: str, *, temperature: float = 0.2) -> dict`; module constant `TASK_MODEL_MAP: dict[str, str]` keyed by `"profile_synthesis"`, `"page_classify"`, `"gap_analysis"`, `"expected_map"`; `def model_for_task(task: str) -> str`. Later tasks mock by `patch("app.auditor.llm.complete_json", ...)`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_llm_seam.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auditor import llm


def test_model_for_task_routes_to_configured_models():
    assert llm.model_for_task("profile_synthesis") == "gpt-4o"
    assert llm.model_for_task("page_classify") == "gpt-4o-mini"
    assert llm.model_for_task("gap_analysis") == "gpt-4o"
    assert llm.model_for_task("expected_map") == "gpt-4o"


def test_model_for_task_unknown_raises():
    with pytest.raises(ValueError):
        llm.model_for_task("nope")


@pytest.mark.asyncio
async def test_complete_json_parses_response_and_uses_task_model():
    fake_msg = MagicMock()
    fake_msg.content = '{"ok": true, "n": 3}'
    fake_choice = MagicMock(message=fake_msg)
    fake_resp = MagicMock(choices=[fake_choice])
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    with patch("app.auditor.llm._client", fake_client):
        out = await llm.complete_json("page_classify", "sys", "usr")
    assert out == {"ok": True, "n": 3}
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["response_format"] == {"type": "json_object"}
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_llm_seam.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.llm'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/llm.py
"""Task-named LLM seam for the auditor. One interface; per-task model routing."""

import json

from loguru import logger
from openai import AsyncOpenAI

from app.core.config import settings
from app.utils.rate_limiter import openai_llm_rate_limiter

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

TASK_MODEL_MAP: dict[str, str] = {
    "profile_synthesis": settings.AUDITOR_MODEL_PROFILE,
    "page_classify": settings.AUDITOR_MODEL_CLASSIFY,
    "gap_analysis": settings.AUDITOR_MODEL_GAPS,
    "expected_map": settings.AUDITOR_MODEL_GAPS,
}


def model_for_task(task: str) -> str:
    try:
        return TASK_MODEL_MAP[task]
    except KeyError as exc:
        raise ValueError(f"Unknown auditor LLM task: {task}") from exc


async def complete_json(
    task: str, system: str, user: str, *, temperature: float = 0.2
) -> dict:
    """Call the task's configured model and parse a JSON object response."""
    model = model_for_task(task)
    await openai_llm_rate_limiter.acquire()
    resp = await _client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error("Auditor LLM task {} returned non-JSON: {}", task, content[:200])
        return {}
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_llm_seam.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/llm.py tests/auditor/test_llm_seam.py`

---

### Task 7: `app/auditor/discovery.py` — URL discovery + SSRF guard
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/discovery.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_discovery.py`

**Interfaces:**
- Consumes: `resolve_and_validate_host`.
- Produces: `def normalize_url(raw: str, base: str | None = None) -> str | None` (lowercase host, strip fragment, drop tracking query params, return `None` for non-http(s)); `def is_structurally_prunable(url: str) -> bool` (login/checkout/cart/admin/logout/asset/`?`-dup); `def guard_url(url: str) -> bool` (True if `resolve_and_validate_host` passes, False on `ValueError`); `async def discover(root_url: str, max_pages: int, fetch_text=None) -> list[str]` — sitemap-first then BFS depth-3, prune, SSRF-guard each, dedupe, capped at `max_pages`. `fetch_text(url) -> str | None` is an injectable async fetcher (default uses `httpx`), enabling fixture tests with no network.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_discovery.py
import pytest

from app.auditor import discovery


def test_normalize_strips_fragment_and_tracking():
    out = discovery.normalize_url("https://Example.com/a?utm_source=x&id=5#frag")
    assert out == "https://example.com/a?id=5"


def test_normalize_rejects_non_http():
    assert discovery.normalize_url("ftp://example.com/x") is None
    assert discovery.normalize_url("mailto:a@b.com") is None


def test_structural_prune():
    assert discovery.is_structurally_prunable("https://x.com/login")
    assert discovery.is_structurally_prunable("https://x.com/cart")
    assert discovery.is_structurally_prunable("https://x.com/assets/app.js")
    assert not discovery.is_structurally_prunable("https://x.com/about")


@pytest.mark.asyncio
async def test_discover_uses_sitemap_prunes_caps_and_guards(monkeypatch):
    pages = {
        "https://ex.com/sitemap.xml": (
            "<urlset><url><loc>https://ex.com/</loc></url>"
            "<url><loc>https://ex.com/about</loc></url>"
            "<url><loc>https://ex.com/login</loc></url>"
            "<url><loc>https://ex.com/pricing</loc></url></urlset>"
        ),
    }

    async def fake_fetch(url):
        return pages.get(url)

    monkeypatch.setattr(discovery, "guard_url", lambda u: True)
    found = await discovery.discover("https://ex.com", max_pages=2, fetch_text=fake_fetch)
    assert "https://ex.com/login" not in found  # pruned
    assert len(found) == 2  # capped
    assert "https://ex.com/" in found


@pytest.mark.asyncio
async def test_discover_drops_ssrf_blocked_urls(monkeypatch):
    pages = {"https://ex.com/sitemap.xml": "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"}

    async def fake_fetch(url):
        return pages.get(url)

    monkeypatch.setattr(discovery, "guard_url", lambda u: False)
    found = await discovery.discover("https://ex.com", max_pages=10, fetch_text=fake_fetch)
    assert found == []  # everything blocked by SSRF guard
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.discovery'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/discovery.py
"""URL discovery: sitemap-first, BFS depth-3 fallback, structural prune, SSRF guard."""

from __future__ import annotations

import re
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import httpx
from loguru import logger

from app.utils.network_security import resolve_and_validate_host

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref")
_PRUNE_PATH_RE = re.compile(
    r"/(login|logout|signin|sign-in|register|signup|sign-up|cart|checkout|admin|account|wishlist)\b",
    re.IGNORECASE,
)
_ASSET_RE = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|pdf|zip|mp4|webp)(\?|$)", re.I)
_MAX_DEPTH = 3


def normalize_url(raw: str, base: str | None = None) -> str | None:
    if base:
        raw = urljoin(base, raw)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    return urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        "",
        urlencode(query_pairs),
        "",
    ))


def is_structurally_prunable(url: str) -> bool:
    parsed = urlparse(url)
    if _ASSET_RE.search(parsed.path):
        return True
    if _PRUNE_PATH_RE.search(parsed.path):
        return True
    return False


def guard_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    try:
        resolve_and_validate_host(parsed.hostname)
        return True
    except ValueError:
        logger.warning("Auditor SSRF guard blocked URL: {}", url)
        return False


async def _default_fetch_text(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.debug("discovery fetch failed for {}: {}", url, exc)
    return None


def _parse_sitemap(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml, re.IGNORECASE)


def _extract_links(html: str, base: str) -> list[str]:
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    out = []
    for href in hrefs:
        n = normalize_url(href, base=base)
        if n:
            out.append(n)
    return out


async def discover(root_url: str, max_pages: int, fetch_text=None) -> list[str]:
    """Return up to max_pages discovered, pruned, SSRF-guarded URLs (root first)."""
    fetch_text = fetch_text or _default_fetch_text
    root = normalize_url(root_url)
    if not root or not guard_url(root):
        return []
    root_host = urlparse(root).netloc

    found: list[str] = []
    seen: set[str] = set()

    def _accept(url: str) -> bool:
        if url in seen:
            return False
        if urlparse(url).netloc != root_host:
            return False
        if is_structurally_prunable(url):
            seen.add(url)
            return False
        if not guard_url(url):
            seen.add(url)
            return False
        return True

    if _accept(root):
        seen.add(root)
        found.append(root)

    # Phase 1: sitemap.xml
    sitemap = await fetch_text(urljoin(root + "/", "sitemap.xml"))
    if sitemap:
        for loc in _parse_sitemap(sitemap):
            n = normalize_url(loc)
            if n and _accept(n):
                seen.add(n)
                found.append(n)
                if len(found) >= max_pages:
                    return found[:max_pages]

    # Phase 2: BFS depth-3 fallback to fill remaining budget
    queue: deque[tuple[str, int]] = deque((u, 0) for u in list(found))
    while queue and len(found) < max_pages:
        url, depth = queue.popleft()
        if depth >= _MAX_DEPTH:
            continue
        html = await fetch_text(url)
        if not html:
            continue
        for link in _extract_links(html, url):
            if len(found) >= max_pages:
                break
            if _accept(link):
                seen.add(link)
                found.append(link)
                queue.append((link, depth + 1))

    return found[:max_pages]
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_discovery.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/discovery.py tests/auditor/test_discovery.py`

---

### Task 8: `app/auditor/crawler.py` — Playwright render + robots + politeness
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/crawler.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_crawler.py`

**Interfaces:**
- Consumes: `settings.AUDITOR_PER_PAGE_TIMEOUT_S`, `settings.AUDITOR_CRAWL_RPS`, `guard_url`.
- Produces: dataclass `RawPage(url: str, status: int, html: str | None, bytes_len: int, error: str | None)`; `async def fetch_robots(domain_root: str, fetch_text=None) -> set[str]` (returns disallowed path prefixes); `def is_allowed_by_robots(url: str, disallowed: set[str]) -> bool`; `async def render_page(url: str, *, page_renderer=None, max_bytes: int = 2_000_000) -> RawPage`; `async def crawl(urls: list[str], *, rps: int | None = None, page_renderer=None, robots_disallowed: set[str] | None = None) -> list[RawPage]`. `page_renderer(url) -> tuple[int, str]` is an injectable async seam wrapping Playwright; tests pass a fake.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_crawler.py
import pytest

from app.auditor import crawler


def test_robots_blocks_disallowed_path():
    disallowed = {"/private", "/admin"}
    assert not crawler.is_allowed_by_robots("https://x.com/private/page", disallowed)
    assert crawler.is_allowed_by_robots("https://x.com/about", disallowed)


@pytest.mark.asyncio
async def test_fetch_robots_parses_disallow(monkeypatch):
    async def fake_fetch(url):
        return "User-agent: *\nDisallow: /secret\nDisallow: /tmp\nAllow: /\n"
    out = await crawler.fetch_robots("https://x.com", fetch_text=fake_fetch)
    assert "/secret" in out and "/tmp" in out


@pytest.mark.asyncio
async def test_render_page_enforces_size_cap():
    async def big_renderer(url):
        return 200, "x" * 50
    page = await crawler.render_page("https://x.com/a", page_renderer=big_renderer, max_bytes=10)
    assert page.error == "SIZE_CAP_EXCEEDED"
    assert page.html is None


@pytest.mark.asyncio
async def test_crawl_renders_each_url(monkeypatch):
    async def renderer(url):
        return 200, f"<html>{url}</html>"
    pages = await crawler.crawl(
        ["https://x.com/a", "https://x.com/b"], rps=1000, page_renderer=renderer
    )
    assert len(pages) == 2
    assert pages[0].status == 200 and pages[0].html.startswith("<html>")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_crawler.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.crawler'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/crawler.py
"""Playwright render + robots.txt + 1rps politeness + per-page timeout/size caps."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from app.core.config import settings


@dataclass
class RawPage:
    url: str
    status: int
    html: str | None
    bytes_len: int
    error: str | None = None


async def _default_fetch_text(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            return resp.text if resp.status_code == 200 else None
    except Exception as exc:
        logger.debug("robots fetch failed {}: {}", url, exc)
        return None


async def fetch_robots(domain_root: str, fetch_text=None) -> set[str]:
    fetch_text = fetch_text or _default_fetch_text
    body = await fetch_text(urljoin(domain_root.rstrip("/") + "/", "robots.txt"))
    disallowed: set[str] = set()
    if not body:
        return disallowed
    applies = False
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            applies = value == "*"
        elif key == "disallow" and applies and value:
            disallowed.add(value)
    return disallowed


def is_allowed_by_robots(url: str, disallowed: set[str]) -> bool:
    path = urlparse(url).path or "/"
    return not any(path.startswith(rule) for rule in disallowed)


async def _playwright_render(url: str) -> tuple[int, str]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            resp = await page.goto(
                url, timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S * 1000, wait_until="domcontentloaded"
            )
            html = await page.content()
            return (resp.status if resp else 0), html
        finally:
            await browser.close()


async def render_page(url: str, *, page_renderer=None, max_bytes: int = 2_000_000) -> RawPage:
    renderer = page_renderer or _playwright_render
    try:
        status, html = await asyncio.wait_for(
            renderer(url), timeout=settings.AUDITOR_PER_PAGE_TIMEOUT_S + 2
        )
    except asyncio.TimeoutError:
        return RawPage(url=url, status=0, html=None, bytes_len=0, error="TIMEOUT")
    except Exception as exc:
        return RawPage(url=url, status=0, html=None, bytes_len=0, error=str(exc))
    byte_len = len(html.encode("utf-8"))
    if byte_len > max_bytes:
        return RawPage(url=url, status=status, html=None, bytes_len=byte_len, error="SIZE_CAP_EXCEEDED")
    return RawPage(url=url, status=status, html=html, bytes_len=byte_len)


async def crawl(
    urls: list[str],
    *,
    rps: int | None = None,
    page_renderer=None,
    robots_disallowed: set[str] | None = None,
) -> list[RawPage]:
    rps = rps or settings.AUDITOR_CRAWL_RPS
    delay = 1.0 / rps if rps > 0 else 0.0
    robots_disallowed = robots_disallowed or set()
    pages: list[RawPage] = []
    for url in urls:
        if not is_allowed_by_robots(url, robots_disallowed):
            pages.append(RawPage(url=url, status=0, html=None, bytes_len=0, error="ROBOTS_BLOCKED"))
            continue
        pages.append(await render_page(url, page_renderer=page_renderer))
        if delay:
            await asyncio.sleep(delay)
    return pages
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_crawler.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/crawler.py tests/auditor/test_crawler.py`

---

### Task 9: `app/auditor/extraction.py` — main-content + content_hash + dedup
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/extraction.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_extraction.py`

**Interfaces:**
- Consumes: `RawPage`.
- Produces: dataclass `ExtractedPage(url: str, title: str | None, text: str, content_hash: str)`; `def content_hash(text: str) -> str` (SHA256 hex, matches `KnowledgeService._compute_content_hash`); `def extract_main(html: str) -> tuple[str | None, str]` (title, main text via trafilatura with bs4 title fallback); `def extract_pages(pages: list[RawPage]) -> list[ExtractedPage]` (skips error/empty pages, dedupes by `content_hash`).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_extraction.py
from app.auditor import extraction
from app.auditor.crawler import RawPage


def test_content_hash_is_sha256_hex():
    h = extraction.content_hash("hello world")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_extract_main_returns_title_and_text():
    html = "<html><head><title>About Us</title></head><body><article><p>We sell widgets in Lagos.</p></article></body></html>"
    title, text = extraction.extract_main(html)
    assert title == "About Us"
    assert "widgets" in text


def test_extract_pages_dedupes_identical_content():
    html = "<html><head><title>T</title></head><body><article><p>Same body content here for dedup.</p></article></body></html>"
    pages = [
        RawPage(url="https://x.com/a", status=200, html=html, bytes_len=10),
        RawPage(url="https://x.com/b", status=200, html=html, bytes_len=10),
        RawPage(url="https://x.com/err", status=0, html=None, bytes_len=0, error="TIMEOUT"),
    ]
    out = extraction.extract_pages(pages)
    assert len(out) == 1  # duplicate + error dropped
    assert out[0].content_hash == extraction.content_hash(out[0].text)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_extraction.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.extraction'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/extraction.py
"""Main-content extraction (trafilatura) + content_hash + dedup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import trafilatura
from bs4 import BeautifulSoup

from app.auditor.crawler import RawPage


@dataclass
class ExtractedPage:
    url: str
    title: str | None
    text: str
    content_hash: str


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def extract_main(html: str) -> tuple[str | None, str]:
    text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if not text:
        text = soup.get_text(separator=" ", strip=True)
    return title, text.strip()


def extract_pages(pages: list[RawPage]) -> list[ExtractedPage]:
    out: list[ExtractedPage] = []
    seen_hashes: set[str] = set()
    for page in pages:
        if page.error or not page.html:
            continue
        title, text = extract_main(page.html)
        if not text:
            continue
        h = content_hash(text)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append(ExtractedPage(url=page.url, title=title, text=text, content_hash=h))
    return out
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_extraction.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/extraction.py tests/auditor/test_extraction.py`

---

### Task 10: `app/auditor/profile.py` — BusinessProfile synthesis + persist + reuse
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/profile.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_profile.py`

**Interfaces:**
- Consumes: `ExtractedPage`, `llm.complete_json`, `BusinessProfile`.
- Produces: `async def synthesize_profile(pages: list[ExtractedPage], *, max_sample_chars: int = 12000) -> dict` (returns dict with keys `summary, industry, offerings, audience, positioning, key_entities, tone, confidence`); `def persist_profile(db: Session, tenant_id: UUID, data: dict, *, model_version: str) -> BusinessProfile` (upsert one-per-tenant, sets `source=AUDITOR`, bumps `refreshed_at`); `def content_signature(pages: list[ExtractedPage]) -> str` (stable hash of sorted content_hashes); `async def synthesize_or_reuse(db, tenant_id, pages, *, prior_signature: str | None) -> tuple[BusinessProfile, str]` (reuses persisted profile when signature unchanged).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_profile.py
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import profile
from app.auditor.extraction import ExtractedPage
from app.db.models.business_profile import BusinessProfile


def _pages():
    return [
        ExtractedPage(url="https://x.com/", title="Home", text="We are a Lagos fintech.", content_hash="h1"),
        ExtractedPage(url="https://x.com/about", title="About", text="Wallets and bills.", content_hash="h2"),
    ]


@pytest.mark.asyncio
async def test_synthesize_profile_returns_structured_dict():
    fake = {
        "summary": "Lagos fintech", "industry": "fintech",
        "offerings": ["wallet"], "audience": ["SMEs"], "positioning": "fast",
        "key_entities": ["LuranPay"], "tone": {"style": "warm"}, "confidence": 0.8,
    }
    with patch("app.auditor.profile.complete_json", AsyncMock(return_value=fake)):
        out = await profile.synthesize_profile(_pages())
    assert out["industry"] == "fintech"
    assert out["offerings"] == ["wallet"]


def test_persist_profile_upserts_one_per_tenant(db):
    tid = uuid.uuid4()
    data = {"summary": "s", "industry": "i", "offerings": [], "audience": [],
            "positioning": "p", "key_entities": [], "tone": {}, "confidence": 0.5}
    bp1 = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()
    data["industry"] = "updated"
    bp2 = profile.persist_profile(db, tid, data, model_version="gpt-4o")
    db.flush()
    assert bp1.id == bp2.id  # upsert, not duplicate
    assert db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tid).count() == 1
    assert bp2.industry == "updated"


def test_content_signature_stable_regardless_of_order():
    a = profile.content_signature(_pages())
    b = profile.content_signature(list(reversed(_pages())))
    assert a == b
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_profile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.profile'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/profile.py
"""BusinessProfile synthesis via the LLM seam; persist + reuse-unless-changed."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.auditor.extraction import ExtractedPage
from app.auditor.llm import complete_json
from app.db.models.business_profile import BusinessProfile, BusinessProfileSource

_SYSTEM = (
    "You are a business analyst. Read the website excerpts and return a JSON object describing "
    "the business. Keys: summary (string), industry (string), offerings (array of strings), "
    "audience (array of strings), positioning (string), key_entities (array of strings), "
    "tone (object), confidence (0-1 float). Make no currency assumptions."
)


def _build_sample(pages: list[ExtractedPage], max_sample_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for page in pages:
        block = f"URL: {page.url}\nTITLE: {page.title or ''}\n{page.text}\n---\n"
        if total + len(block) > max_sample_chars:
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


async def synthesize_profile(pages: list[ExtractedPage], *, max_sample_chars: int = 12000) -> dict:
    sample = _build_sample(pages, max_sample_chars)
    data = await complete_json("profile_synthesis", _SYSTEM, sample, temperature=0.1)
    return {
        "summary": data.get("summary"),
        "industry": data.get("industry"),
        "offerings": data.get("offerings") or [],
        "audience": data.get("audience") or [],
        "positioning": data.get("positioning"),
        "key_entities": data.get("key_entities") or [],
        "tone": data.get("tone") or {},
        "confidence": data.get("confidence"),
    }


def persist_profile(db: Session, tenant_id: UUID, data: dict, *, model_version: str) -> BusinessProfile:
    bp = db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tenant_id).one_or_none()
    if bp is None:
        bp = BusinessProfile(tenant_id=tenant_id, source=BusinessProfileSource.AUDITOR)
        db.add(bp)
    bp.summary = data.get("summary")
    bp.industry = data.get("industry")
    bp.offerings = data.get("offerings") or []
    bp.audience = data.get("audience") or []
    bp.positioning = data.get("positioning")
    bp.key_entities = data.get("key_entities") or []
    bp.tone = data.get("tone") or {}
    bp.confidence = data.get("confidence")
    bp.model_version = model_version
    bp.raw = data
    return bp


def content_signature(pages: list[ExtractedPage]) -> str:
    joined = "|".join(sorted(p.content_hash for p in pages))
    return hashlib.sha256(joined.encode()).hexdigest()


async def synthesize_or_reuse(
    db: Session, tenant_id: UUID, pages: list[ExtractedPage], *, prior_signature: str | None
) -> tuple[BusinessProfile, str]:
    sig = content_signature(pages)
    existing = db.query(BusinessProfile).filter(BusinessProfile.tenant_id == tenant_id).one_or_none()
    if existing is not None and prior_signature == sig:
        return existing, sig
    from app.auditor.llm import model_for_task

    data = await synthesize_profile(pages)
    bp = persist_profile(db, tenant_id, data, model_version=model_for_task("profile_synthesis"))
    return bp, sig
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_profile.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/profile.py tests/auditor/test_profile.py`

---

### Task 11: `app/auditor/classify.py` — adaptive classification + kind map
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/classify.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_classify.py`

**Interfaces:**
- Consumes: `ExtractedPage`, `llm.complete_json`, `BusinessProfile`, `AuditEntryKind`, `SourceType`.
- Produces: pure dict `KIND_TO_SOURCE_TYPE: dict[AuditEntryKind, SourceType]`; `def map_kind(kind: AuditEntryKind) -> SourceType`; dataclass `Classification(category: str, kind: AuditEntryKind, importance_rank: int, keep: bool)`; `async def classify_page(page: ExtractedPage, profile: dict) -> Classification`; `async def classify_pages(pages: list[ExtractedPage], profile: dict) -> list[tuple[ExtractedPage, Classification]]` (only `keep=True`).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_classify.py
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import classify
from app.auditor.extraction import ExtractedPage
from app.db.models.audit import AuditEntryKind
from app.db.models.knowledge import SourceType


def test_kind_to_source_type_map_is_exhaustive():
    for kind in AuditEntryKind:
        assert kind in classify.KIND_TO_SOURCE_TYPE
    assert classify.map_kind(AuditEntryKind.FAQ) == SourceType.FAQ
    assert classify.map_kind(AuditEntryKind.GUIDE) == SourceType.HELP_DOC
    assert classify.map_kind(AuditEntryKind.POLICY) == SourceType.POLICY
    for k in (AuditEntryKind.PRODUCT, AuditEntryKind.PRICING, AuditEntryKind.CONTACT, AuditEntryKind.TESTIMONIAL):
        assert classify.map_kind(k) == SourceType.OTHER


@pytest.mark.asyncio
async def test_classify_page_parses_llm_output():
    fake = {"category": "onboarding faq", "kind": "faq", "importance_rank": 2, "keep": True}
    page = ExtractedPage(url="https://x.com/faq", title="FAQ", text="...", content_hash="h")
    with patch("app.auditor.classify.complete_json", AsyncMock(return_value=fake)):
        c = await classify.classify_page(page, {"industry": "fintech"})
    assert c.kind == AuditEntryKind.FAQ
    assert c.category == "onboarding faq"
    assert c.keep is True


@pytest.mark.asyncio
async def test_classify_pages_filters_skip():
    keep = {"category": "c", "kind": "product", "importance_rank": 1, "keep": True}
    skip = {"category": "c", "kind": "other", "importance_rank": 9, "keep": False}
    pages = [
        ExtractedPage(url="https://x.com/a", title="A", text="t", content_hash="h1"),
        ExtractedPage(url="https://x.com/b", title="B", text="t", content_hash="h2"),
    ]
    with patch("app.auditor.classify.complete_json", AsyncMock(side_effect=[keep, skip])):
        out = await classify.classify_pages(pages, {})
    assert len(out) == 1
    assert out[0][0].url == "https://x.com/a"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_classify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.classify'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/classify.py
"""Adaptive per-page classification + normalized-kind -> source_type map."""

from __future__ import annotations

from dataclasses import dataclass

from app.auditor.extraction import ExtractedPage
from app.auditor.llm import complete_json
from app.db.models.audit import AuditEntryKind
from app.db.models.knowledge import SourceType

KIND_TO_SOURCE_TYPE: dict[AuditEntryKind, SourceType] = {
    AuditEntryKind.FAQ: SourceType.FAQ,
    AuditEntryKind.GUIDE: SourceType.HELP_DOC,
    AuditEntryKind.POLICY: SourceType.POLICY,
    AuditEntryKind.PRODUCT: SourceType.OTHER,
    AuditEntryKind.PRICING: SourceType.OTHER,
    AuditEntryKind.CONTACT: SourceType.OTHER,
    AuditEntryKind.TESTIMONIAL: SourceType.OTHER,
    AuditEntryKind.OTHER: SourceType.OTHER,
}


def map_kind(kind: AuditEntryKind) -> SourceType:
    return KIND_TO_SOURCE_TYPE[kind]


@dataclass
class Classification:
    category: str
    kind: AuditEntryKind
    importance_rank: int
    keep: bool


_SYSTEM = (
    "You classify a single web page for a specific business. Given the business profile and the page, "
    "return JSON: category (free-form business-specific label, string), kind (one of "
    "faq|policy|product|pricing|guide|contact|testimonial|other), importance_rank (1=most important), "
    "keep (boolean — false for irrelevant/boilerplate pages). Adapt to THIS business; no fixed taxonomy."
)


def _coerce_kind(raw: str) -> AuditEntryKind:
    try:
        return AuditEntryKind(str(raw).lower())
    except ValueError:
        return AuditEntryKind.OTHER


async def classify_page(page: ExtractedPage, profile: dict) -> Classification:
    user = (
        f"BUSINESS PROFILE: {profile}\n\nPAGE URL: {page.url}\nPAGE TITLE: {page.title or ''}\n"
        f"PAGE TEXT:\n{page.text[:4000]}"
    )
    data = await complete_json("page_classify", _SYSTEM, user, temperature=0.1)
    return Classification(
        category=str(data.get("category") or "uncategorized"),
        kind=_coerce_kind(data.get("kind", "other")),
        importance_rank=int(data.get("importance_rank") or 99),
        keep=bool(data.get("keep", True)),
    )


async def classify_pages(
    pages: list[ExtractedPage], profile: dict
) -> list[tuple[ExtractedPage, Classification]]:
    out: list[tuple[ExtractedPage, Classification]] = []
    for page in pages:
        c = await classify_page(page, profile)
        if c.keep:
            out.append((page, c))
    return out
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_classify.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/classify.py tests/auditor/test_classify.py`

---

### Task 12: `app/auditor/gaps.py` — expected-content map + gap analysis
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/gaps.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_gaps.py`

**Interfaces:**
- Consumes: `llm.complete_json`.
- Produces: `async def build_expected_map(profile: dict, *, unanswered_questions: list[str] | None = None) -> list[dict]` (each item `{item, dimensions, weight, kind, demand_count}`); `def compute_gaps(expected_map: list[dict], found_kinds: set[str], found_categories: set[str]) -> list[dict]` PURE — `gap = expected − found`, each gap `{title, why_it_matters, recommended_content, kind, demand_count, impact}`, ranked by `impact × demand_count` descending. `found` match is by `item`/`kind` membership.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_gaps.py
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import gaps


def test_compute_gaps_is_expected_minus_found_ranked():
    expected = [
        {"item": "Refund policy", "dimensions": ["support"], "weight": 0.3, "kind": "policy",
         "why_it_matters": "trust", "recommended_content": "Add a refund policy page", "demand_count": 4},
        {"item": "Pricing page", "dimensions": ["conversion"], "weight": 0.5, "kind": "pricing",
         "why_it_matters": "convert", "recommended_content": "Publish pricing", "demand_count": 2},
        {"item": "FAQ", "dimensions": ["knowledge"], "weight": 0.2, "kind": "faq",
         "why_it_matters": "deflect", "recommended_content": "Add FAQ", "demand_count": 1},
    ]
    found_kinds = {"faq"}  # FAQ already present -> not a gap
    result = gaps.compute_gaps(expected, found_kinds, set())
    titles = [g["title"] for g in result]
    assert "FAQ" not in titles
    # ranked by impact(weight) x demand_count: refund(0.3*4=1.2) > pricing(0.5*2=1.0)
    assert titles == ["Refund policy", "Pricing page"]


def test_compute_gaps_empty_when_all_found():
    expected = [{"item": "FAQ", "dimensions": ["knowledge"], "weight": 0.2, "kind": "faq",
                 "why_it_matters": "x", "recommended_content": "y", "demand_count": 1}]
    assert gaps.compute_gaps(expected, {"faq"}, set()) == []


@pytest.mark.asyncio
async def test_build_expected_map_passes_unanswered_questions():
    fake = {"expected": [{"item": "Pricing", "dimensions": ["conversion"], "weight": 0.4,
                          "kind": "pricing", "demand_count": 0}]}
    with patch("app.auditor.gaps.complete_json", AsyncMock(return_value=fake)) as m:
        out = await gaps.build_expected_map({"industry": "saas"}, unanswered_questions=["do you offer trials?"])
    assert out[0]["item"] == "Pricing"
    assert "do you offer trials?" in m.call_args.args[2]  # user prompt carries questions
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_gaps.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.gaps'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/gaps.py
"""Expected-content map (LLM) + pure gap analysis (expected - found, impact x demand)."""

from __future__ import annotations

from app.auditor.llm import complete_json

_SYSTEM = (
    "You define the content a business like this SHOULD publish to serve and convert customers. "
    "Return JSON {\"expected\": [...]} where each item has: item (string), dimensions (subset of "
    "[knowledge, support, conversion]), weight (0-1 float importance), kind (faq|policy|product|"
    "pricing|guide|contact|testimonial|other), why_it_matters (string), recommended_content (string), "
    "demand_count (int, 0 unless a question count is supplied). Adapt to THIS business."
)


async def build_expected_map(profile: dict, *, unanswered_questions: list[str] | None = None) -> list[dict]:
    questions = unanswered_questions or []
    user = f"BUSINESS PROFILE: {profile}\n\nUNANSWERED CUSTOMER QUESTIONS: {questions}"
    data = await complete_json("expected_map", _SYSTEM, user, temperature=0.1)
    return data.get("expected") or []


def compute_gaps(
    expected_map: list[dict], found_kinds: set[str], found_categories: set[str]
) -> list[dict]:
    """Pure: gap = expected - found; rank by weight (impact) x demand_count."""
    gaps_out: list[dict] = []
    for item in expected_map:
        kind = str(item.get("kind", "other"))
        label = str(item.get("item", "")).lower()
        if kind in found_kinds or label in {c.lower() for c in found_categories}:
            continue
        weight = float(item.get("weight") or 0.0)
        demand = int(item.get("demand_count") or 0)
        impact = weight
        gaps_out.append({
            "title": item.get("item"),
            "why_it_matters": item.get("why_it_matters"),
            "recommended_content": item.get("recommended_content"),
            "kind": kind,
            "demand_count": demand,
            "impact": impact,
        })
    gaps_out.sort(key=lambda g: g["impact"] * max(g["demand_count"], 1), reverse=True)
    return gaps_out
```
> Ranking note: tests use `impact × demand_count` but rank with `max(demand_count, 1)` so a 0-demand onboarding state still orders by impact. The test fixtures all use demand_count >= 1, so ordering matches `weight × demand_count` exactly; verify the asserted order holds.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_gaps.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/gaps.py tests/auditor/test_gaps.py`

---

### Task 13: `app/auditor/scoring.py` — pure deterministic scoring
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/scoring.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_scoring.py`

**Interfaces:**
- Consumes: nothing (pure, no LLM, no network).
- Produces: `def score(expected_map: list[dict], found_items: set[str]) -> dict` returning `{"knowledge": int, "support": int, "conversion": int, "overall": int, "projected": int}`. `found_items` = lowercased expected-item labels currently satisfied. Weights: overall = knowledge·0.40 + support·0.35 + conversion·0.25. `projected` = same arithmetic with ALL expected items treated as found (i.e. all recommended gaps added) → always 100 when expected_map non-empty, but compute it through the same code path (not hard-coded).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_scoring.py
from app.auditor import scoring


def _expected():
    return [
        {"item": "FAQ", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "Guide", "dimensions": ["knowledge"], "weight": 1.0},
        {"item": "Refund policy", "dimensions": ["support"], "weight": 1.0},
        {"item": "Pricing", "dimensions": ["conversion"], "weight": 1.0},
    ]


def test_full_coverage_scores_100():
    s = scoring.score(_expected(), {"faq", "guide", "refund policy", "pricing"})
    assert s == {"knowledge": 100, "support": 100, "conversion": 100, "overall": 100, "projected": 100}


def test_partial_coverage_dimension_math():
    # knowledge: 1 of 2 found -> 50; support 0 -> 0; conversion 1 of 1 -> 100
    s = scoring.score(_expected(), {"faq", "pricing"})
    assert s["knowledge"] == 50
    assert s["support"] == 0
    assert s["conversion"] == 100
    # overall = 50*0.40 + 0*0.35 + 100*0.25 = 20 + 0 + 25 = 45
    assert s["overall"] == 45
    assert s["projected"] == 100  # all gaps added


def test_empty_expected_map_is_zero_not_crash():
    s = scoring.score([], set())
    assert s == {"knowledge": 0, "support": 0, "conversion": 0, "overall": 0, "projected": 0}


def test_determinism_same_inputs_same_output():
    a = scoring.score(_expected(), {"faq"})
    b = scoring.score(_expected(), {"faq"})
    assert a == b
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_scoring.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.scoring'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/scoring.py
"""Pure deterministic AI-readiness scoring. No LLM, no network."""

from __future__ import annotations

_DIMENSIONS = ("knowledge", "support", "conversion")
_OVERALL_WEIGHTS = {"knowledge": 0.40, "support": 0.35, "conversion": 0.25}


def _dimension_score(expected_map: list[dict], found_items: set[str], dimension: str) -> int:
    total_weight = 0.0
    found_weight = 0.0
    for item in expected_map:
        if dimension not in (item.get("dimensions") or []):
            continue
        weight = float(item.get("weight") or 0.0)
        total_weight += weight
        if str(item.get("item", "")).lower() in found_items:
            found_weight += weight
    if total_weight == 0.0:
        return 0
    return round(found_weight / total_weight * 100)


def score(expected_map: list[dict], found_items: set[str]) -> dict:
    found_items = {f.lower() for f in found_items}
    dims = {d: _dimension_score(expected_map, found_items, d) for d in _DIMENSIONS}
    overall = round(sum(dims[d] * _OVERALL_WEIGHTS[d] for d in _DIMENSIONS))

    all_items = {str(i.get("item", "")).lower() for i in expected_map}
    proj_dims = {d: _dimension_score(expected_map, all_items, d) for d in _DIMENSIONS}
    projected = round(sum(proj_dims[d] * _OVERALL_WEIGHTS[d] for d in _DIMENSIONS))

    return {
        "knowledge": dims["knowledge"],
        "support": dims["support"],
        "conversion": dims["conversion"],
        "overall": overall,
        "projected": projected,
    }
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_scoring.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/scoring.py tests/auditor/test_scoring.py`

---

### Task 14: `app/auditor/pipeline.py` + ARQ job `run_website_audit`
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/pipeline.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/workers/document_processor.py` (register `run_website_audit` in `WorkerSettings.functions`)
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_pipeline.py`

**Interfaces:**
- Consumes: `discovery.discover`, `crawler.crawl`/`fetch_robots`, `extraction.extract_pages`, `profile.synthesize_or_reuse`, `classify.classify_pages`, `gaps.build_expected_map`/`compute_gaps`, `scoring.score`, models `AuditReport`/`AuditEntry`/`ProcessingJob`.
- Produces: `async def run_audit(db: Session, audit_report_id: UUID, *, page_renderer=None, fetch_text=None) -> AuditReport` (testable core, dependency-injectable); ARQ entrypoint `async def run_website_audit(ctx: dict, audit_report_id: str) -> dict` (opens `SessionLocal()`, calls `run_audit`, mirrors `process_document_ingestion` job/progress lifecycle). Phase order writes `AuditStatus` CRAWLING→ANALYZING→COMPLETED and updates `ProcessingJob.total_items`/`processed_items`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_pipeline.py
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auditor import pipeline
from app.db.models.audit import AuditReport, AuditScope, AuditStatus, AuditEntryStatus
from app.db.models.job import JobStatus, JobType, ProcessingJob


@pytest.mark.asyncio
async def test_run_audit_end_to_end_with_mocked_llm(db):
    tenant_id = uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(), tenant_id=tenant_id, job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.QUEUED, input_data={},
    )
    db.add(job)
    report = AuditReport(
        tenant_id=tenant_id, url="https://ex.com", domain="ex.com",
        scope=AuditScope.DASHBOARD, status=AuditStatus.QUEUED, processing_job_id=job.id,
    )
    db.add(report)
    db.flush()

    async def fake_fetch(url):
        if url.endswith("sitemap.xml"):
            return "<urlset><url><loc>https://ex.com/about</loc></url></urlset>"
        return None

    async def fake_renderer(url):
        return 200, "<html><head><title>About</title></head><body><article><p>Lagos fintech, wallets.</p></article></body></html>"

    prof = {"summary": "fintech", "industry": "fintech", "offerings": ["wallet"],
            "audience": [], "positioning": "", "key_entities": [], "tone": {}, "confidence": 0.8}
    cls = {"category": "about", "kind": "product", "importance_rank": 1, "keep": True}
    expected = {"expected": [{"item": "Refund policy", "dimensions": ["support"], "weight": 1.0,
                              "kind": "policy", "why_it_matters": "trust",
                              "recommended_content": "add it", "demand_count": 2}]}

    with patch("app.auditor.profile.complete_json", AsyncMock(return_value=prof)), \
         patch("app.auditor.classify.complete_json", AsyncMock(return_value=cls)), \
         patch("app.auditor.gaps.complete_json", AsyncMock(return_value=expected)):
        out = await pipeline.run_audit(
            db, report.id, page_renderer=fake_renderer, fetch_text=fake_fetch
        )

    db.refresh(out)
    assert out.status == AuditStatus.COMPLETED
    assert out.pages_scanned >= 1
    assert out.business_profile_id is not None
    assert out.overall_score is not None
    assert len(out.gaps) == 1 and out.gaps[0]["title"] == "Refund policy"
    entries = [e for e in out.entries if e.status == AuditEntryStatus.PROPOSED]
    assert len(entries) >= 1
    db.refresh(job)
    assert job.status == JobStatus.COMPLETED
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.pipeline'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/pipeline.py
"""Single multi-phase auditor pipeline + ARQ entrypoint."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.auditor import classify, crawler, discovery, extraction, gaps, profile, scoring
from app.core.config import settings
from app.db.models.audit import (
    AuditEntry,
    AuditEntryStatus,
    AuditReport,
    AuditScope,
    AuditStatus,
)
from app.db.models.job import JobStatus, ProcessingJob


def _max_pages(db: Session, report: AuditReport) -> int:
    if report.scope == AuditScope.PUBLIC:
        return settings.AUDITOR_PUBLIC_MAX_PAGES
    from app.db.models.tenant import TenantCapabilities

    cap = (
        db.query(TenantCapabilities)
        .filter(TenantCapabilities.tenant_id == report.tenant_id)
        .one_or_none()
    )
    if cap and cap.max_audit_pages_per_crawl:
        return cap.max_audit_pages_per_crawl
    return settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES


def _update_job(db: Session, job_id: UUID | None, **fields) -> None:
    if not job_id:
        return
    job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).one_or_none()
    if not job:
        return
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


async def run_audit(db: Session, audit_report_id: UUID, *, page_renderer=None, fetch_text=None) -> AuditReport:
    report = db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
    report.status = AuditStatus.CRAWLING
    db.commit()
    _update_job(db, report.processing_job_id, status=JobStatus.PROCESSING, started_at=datetime.now(UTC))

    # Phase 1: discovery
    urls = await discovery.discover(report.url, _max_pages(db, report), fetch_text=fetch_text)
    report.pages_discovered = len(urls)
    _update_job(db, report.processing_job_id, total_items=len(urls), processed_items=0)
    db.commit()

    # Phase 2: crawl + extract
    robots = await crawler.fetch_robots(report.url, fetch_text=fetch_text)
    raw_pages = await crawler.crawl(urls, page_renderer=page_renderer, robots_disallowed=robots)
    extracted = extraction.extract_pages(raw_pages)
    report.pages_scanned = len(extracted)
    _update_job(db, report.processing_job_id, processed_items=len(extracted))
    db.commit()

    report.status = AuditStatus.ANALYZING
    db.commit()

    # Phase 3: profile
    profile_obj, _sig = await profile.synthesize_or_reuse(
        db, report.tenant_id or report.id, extracted, prior_signature=None
    )
    db.flush()
    report.business_profile_id = profile_obj.id
    profile_dict = {
        "summary": profile_obj.summary, "industry": profile_obj.industry,
        "offerings": profile_obj.offerings, "audience": profile_obj.audience,
        "positioning": profile_obj.positioning, "key_entities": profile_obj.key_entities,
    }

    # Phase 4: classify -> staged entries
    classified = await classify.classify_pages(extracted, profile_dict)
    found_kinds: set[str] = set()
    found_categories: set[str] = set()
    for page, c in classified:
        found_kinds.add(c.kind.value)
        found_categories.add(c.category)
        db.add(AuditEntry(
            audit_report_id=report.id,
            tenant_id=report.tenant_id,
            url=page.url,
            title=page.title,
            category=c.category,
            kind=c.kind,
            content=page.text,
            content_hash=page.content_hash,
            importance_rank=c.importance_rank,
            status=AuditEntryStatus.PROPOSED,
            meta={"extraction_confidence": 1.0},
        ))
    db.flush()

    # Phase 5/6: expected map + gaps
    expected_map = await gaps.build_expected_map(profile_dict, unanswered_questions=[])
    report.gaps = gaps.compute_gaps(expected_map, found_kinds, found_categories)

    # Phase 7: scoring (found = expected items already covered by kind/category)
    found_items = {
        str(i.get("item", "")).lower()
        for i in expected_map
        if str(i.get("kind", "")) in found_kinds
    }
    s = scoring.score(expected_map, found_items)
    report.knowledge_score = s["knowledge"]
    report.support_score = s["support"]
    report.conversion_score = s["conversion"]
    report.overall_score = s["overall"]
    report.projected_score = s["projected"]
    report.summary_stats = {"kinds": sorted(found_kinds), "entries": len(classified)}

    # Phase 8: complete
    report.status = AuditStatus.COMPLETED
    report.completed_at = datetime.now(UTC)
    db.commit()
    _update_job(
        db, report.processing_job_id, status=JobStatus.COMPLETED, completed_at=datetime.now(UTC)
    )
    db.refresh(report)
    return report


async def run_website_audit(ctx: dict, audit_report_id: str) -> dict:
    from app.db.session import SessionLocal

    db: Session = SessionLocal()
    try:
        report = await run_audit(db, UUID(audit_report_id))
        return {"success": True, "audit_report_id": str(report.id), "status": report.status.value}
    except Exception as exc:
        logger.error("run_website_audit failed for {}: {}", audit_report_id, exc)
        rep = db.query(AuditReport).filter(AuditReport.id == UUID(audit_report_id)).one_or_none()
        if rep:
            rep.status = AuditStatus.FAILED
            _update_job(db, rep.processing_job_id, status=JobStatus.FAILED, error_message=str(exc))
            db.commit()
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
```
Register in `WorkerSettings.functions` in `app/workers/document_processor.py`: add `from app.auditor.pipeline import run_website_audit` near the WorkerSettings imports, then add `run_website_audit,` to the `functions = [...]` list.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_pipeline.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/pipeline.py app/workers/document_processor.py tests/auditor/test_pipeline.py`

---

### Task 15: `app/auditor/service.py` — `start_audit`
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/service.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_service_start.py`

**Interfaces:**
- Consumes: models `AuditReport`/`ProcessingJob`, `AuditScope`/`AuditStatus`, `JobType.DOCUMENT_INGESTION`.
- Produces: class `AuditService.__init__(self, db: Session)`; `def start_audit(self, *, url: str, scope: AuditScope, tenant_id: UUID | None, created_by: UUID | None = None) -> AuditReport` — normalizes URL/domain, creates `ProcessingJob` + `AuditReport(status=QUEUED)`, generates `claim_token` for public scope, returns the report. ENQUEUE is done by the caller seam `enqueue_audit(report_id)` (module function, mockable) so tests don't hit Redis.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_service_start.py
import uuid
from unittest.mock import patch

from app.auditor.service import AuditService
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import ProcessingJob


def test_start_audit_public_creates_report_job_and_claim_token(db):
    with patch("app.auditor.service.enqueue_audit") as mock_enqueue:
        svc = AuditService(db)
        report = svc.start_audit(url="https://Example.com/", scope=AuditScope.PUBLIC, tenant_id=None)
    db.flush()
    assert report.scope == AuditScope.PUBLIC
    assert report.status == AuditStatus.QUEUED
    assert report.domain == "example.com"
    assert report.claim_token  # public reports are claimable
    assert report.processing_job_id is not None
    assert db.query(ProcessingJob).filter(ProcessingJob.id == report.processing_job_id).count() == 1
    mock_enqueue.assert_called_once_with(report.id)


def test_start_audit_dashboard_has_tenant_no_claim_token(db):
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit"):
        report = AuditService(db).start_audit(
            url="https://shop.example.com", scope=AuditScope.DASHBOARD, tenant_id=tid
        )
    db.flush()
    assert report.tenant_id == tid
    assert report.claim_token is None
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_start.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auditor.service'`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/auditor/service.py
"""Auditor service: start audits (public + dashboard), import, claim, purge."""

from __future__ import annotations

import secrets
from urllib.parse import urlparse
from uuid import UUID

from loguru import logger
from sqlalchemy.orm import Session

from app.auditor.discovery import normalize_url
from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.job import JobType, ProcessingJob


def enqueue_audit(report_id: UUID) -> None:
    """Enqueue the ARQ job. Separated for test mockability."""
    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import settings

    async def _enqueue() -> None:
        redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await redis.enqueue_job("run_website_audit", str(report_id))

    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(_enqueue())  # already in loop (endpoint context)
    except RuntimeError:
        asyncio.run(_enqueue())


class AuditService:
    def __init__(self, db: Session):
        self.db = db

    def start_audit(
        self, *, url: str, scope: AuditScope, tenant_id: UUID | None, created_by: UUID | None = None
    ) -> AuditReport:
        normalized = normalize_url(url)
        if not normalized:
            raise ValueError("Invalid URL")
        domain = urlparse(normalized).netloc

        job = ProcessingJob(
            tenant_id=tenant_id,  # None for public (pre-signup) audits; column is nullable (Task 4)
            job_type=JobType.DOCUMENT_INGESTION,
            input_data={"kind": "website_audit", "url": normalized},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()

        report = AuditReport(
            tenant_id=tenant_id,
            url=normalized,
            domain=domain,
            scope=scope,
            status=AuditStatus.QUEUED,
            processing_job_id=job.id,
            claim_token=secrets.token_urlsafe(24) if scope == AuditScope.PUBLIC else None,
        )
        self.db.add(report)
        self.db.flush()
        self.db.commit()
        logger.info("Started {} audit {} for {}", scope.value, report.id, domain)
        enqueue_audit(report.id)
        return report
```
> Note: `ProcessingJob.tenant_id` is nullable (made so in Task 4). Public (pre-signup) audits have no tenant but still need a `ProcessingJob` progress row, so `start_audit` passes `tenant_id=None` directly — NOT a sentinel `UUID(int=0)`. The WS/progress channel keys on `job_id`, not `tenant_id`, so a null tenant does not affect progress streaming.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_start.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/service.py tests/auditor/test_service_start.py`

---

### Task 16: `import_entries` + plan-gating + idempotency + materialize to RAG
**Files:**
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/service.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_service_import.py`

**Interfaces:**
- Consumes: `AuditReport`/`AuditEntry`, `TenantCapabilities.max_knowledge_sources`, `classify.map_kind`, `KnowledgeSource`/`SourceStatus`, `extraction.content_hash`.
- Produces: `def preselect_entries(self, audit_report_id: UUID) -> tuple[list[AuditEntry], list[AuditEntry]]` (top-N by `importance_rank` → `active`, rest → `locked`, using `max_knowledge_sources`; None = unlimited); `def import_entries(self, audit_report_id: UUID, *, uploaded_by: UUID | None = None) -> list[UUID]` (idempotent per `(audit_report_id, entry_id)`: skips already-`imported`; for each `active` entry creates `KnowledgeSource(source_category="url", source_type=map_kind(kind), status=PROCESSING)`, links `knowledge_source_id`, sets entry `IMPORTED`, enqueues embedding via `enqueue_document_ingestion` seam; dedup re-scan by `content_hash`+`url`); `def unlock_after_upgrade(self, audit_report_id: UUID) -> int` (locked→saved eligibility count when `max_knowledge_sources` rises).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_service_import.py
import uuid
from unittest.mock import patch

from app.auditor.service import AuditService
from app.db.models.audit import (
    AuditEntry, AuditEntryKind, AuditEntryStatus, AuditReport, AuditScope, AuditStatus,
)
from app.db.models.knowledge import KnowledgeSource
from app.db.models.tenant import TenantCapabilities


def _setup(db, max_sources):
    tid = uuid.uuid4()
    db.add(TenantCapabilities(tenant_id=tid, max_knowledge_sources=max_sources))
    report = AuditReport(tenant_id=tid, url="https://ex.com", domain="ex.com",
                         scope=AuditScope.DASHBOARD, status=AuditStatus.COMPLETED)
    db.add(report)
    db.flush()
    for i in range(3):
        db.add(AuditEntry(audit_report_id=report.id, tenant_id=tid, url=f"https://ex.com/{i}",
                          title=f"P{i}", category="c", kind=AuditEntryKind.FAQ,
                          content=f"content {i}", content_hash=("%064d" % i),
                          importance_rank=i + 1, status=AuditEntryStatus.PROPOSED))
    db.flush()
    return tid, report


def test_preselect_respects_max_knowledge_sources(db):
    _tid, report = _setup(db, max_sources=2)
    active, locked = AuditService(db).preselect_entries(report.id)
    db.flush()
    assert len(active) == 2 and len(locked) == 1
    assert active[0].importance_rank <= active[1].importance_rank
    assert all(e.status == AuditEntryStatus.ACTIVE for e in active)
    assert locked[0].status == AuditEntryStatus.LOCKED


def test_import_is_idempotent(db):
    _tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()
    with patch("app.auditor.service.enqueue_document_ingestion"):
        first = svc.import_entries(report.id)
        second = svc.import_entries(report.id)  # re-run
    db.flush()
    assert len(first) == 2
    assert second == []  # nothing new imported
    assert db.query(KnowledgeSource).filter(KnowledgeSource.tenant_id == report.tenant_id).count() == 2


def test_upgrade_unlock_counts_newly_eligible(db):
    tid, report = _setup(db, max_sources=2)
    svc = AuditService(db)
    svc.preselect_entries(report.id)
    db.flush()
    cap = db.query(TenantCapabilities).filter(TenantCapabilities.tenant_id == tid).one()
    cap.max_knowledge_sources = 3
    db.flush()
    assert svc.unlock_after_upgrade(report.id) == 1
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_import.py -q`
Expected: FAIL — `AttributeError: 'AuditService' object has no attribute 'preselect_entries'`.

- [ ] **Step 3: Write minimal implementation**
Append to `app/auditor/service.py`:
```python
def enqueue_document_ingestion(job_id: UUID) -> None:
    """Enqueue the existing document_processor job. Separated for mockability."""
    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import settings

    async def _enqueue() -> None:
        redis = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await redis.enqueue_job("process_document_ingestion", str(job_id))

    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(_enqueue())
    except RuntimeError:
        asyncio.run(_enqueue())
```
Add these methods to `AuditService`:
```python
    def _max_sources(self, tenant_id: UUID) -> int | None:
        from app.db.models.tenant import TenantCapabilities

        cap = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .one_or_none()
        )
        return cap.max_knowledge_sources if cap else 0

    def preselect_entries(self, audit_report_id: UUID):
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        entries = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status.in_([AuditEntryStatus.PROPOSED, AuditEntryStatus.ACTIVE, AuditEntryStatus.LOCKED]),
            )
            .order_by(AuditEntry.importance_rank.asc())
            .all()
        )
        limit = self._max_sources(report.tenant_id)
        active, locked = [], []
        for idx, entry in enumerate(entries):
            if limit is None or idx < limit:
                entry.status = AuditEntryStatus.ACTIVE
                active.append(entry)
            else:
                entry.status = AuditEntryStatus.LOCKED
                locked.append(entry)
        self.db.flush()
        return active, locked

    def import_entries(self, audit_report_id: UUID, *, uploaded_by: UUID | None = None):
        from app.auditor.classify import map_kind
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport
        from app.db.models.job import JobType, ProcessingJob
        from app.db.models.knowledge import KnowledgeSource, SourceStatus

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        active = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .all()
        )
        imported_ids: list[UUID] = []
        for entry in active:
            # idempotency / re-scan dedup: skip if a source with same (tenant, content_hash) exists
            dup = (
                self.db.query(KnowledgeSource)
                .filter(
                    KnowledgeSource.tenant_id == report.tenant_id,
                    KnowledgeSource.content_hash == entry.content_hash,
                    KnowledgeSource.status != SourceStatus.INACTIVE,
                )
                .one_or_none()
            )
            if dup is not None:
                entry.status = AuditEntryStatus.IMPORTED
                entry.knowledge_source_id = dup.id
                continue
            source = KnowledgeSource(
                tenant_id=report.tenant_id,
                name=entry.title or entry.url,
                source_type=map_kind(entry.kind),
                source_category="url",
                source_url=entry.url,
                content_hash=entry.content_hash,
                version=1,
                status=SourceStatus.PROCESSING,
                meta={"auditor_category": entry.category, "audit_entry_id": str(entry.id)},
                uploaded_by=uploaded_by,
            )
            self.db.add(source)
            self.db.flush()
            entry.status = AuditEntryStatus.IMPORTED
            entry.knowledge_source_id = source.id

            job = ProcessingJob(
                tenant_id=report.tenant_id,
                job_type=JobType.DOCUMENT_INGESTION,
                input_data={
                    "tenant_id": str(report.tenant_id),
                    "document_text": entry.content,
                    "document_name": entry.title or entry.url,
                    "source_type": map_kind(entry.kind).value,
                    "source_category": "url",
                    "source_url": entry.url,
                    "metadata": {"auditor_category": entry.category},
                },
                created_by=uploaded_by,
            )
            self.db.add(job)
            self.db.flush()
            imported_ids.append(entry.id)
            enqueue_document_ingestion(job.id)

        report.imported = True
        from datetime import UTC, datetime

        report.imported_at = datetime.now(UTC)
        self.db.commit()
        return imported_ids

    def unlock_after_upgrade(self, audit_report_id: UUID) -> int:
        from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport

        report = self.db.query(AuditReport).filter(AuditReport.id == audit_report_id).one()
        limit = self._max_sources(report.tenant_id)
        active_count = (
            self.db.query(AuditEntry)
            .filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.ACTIVE,
            )
            .count()
        )
        if limit is None:
            newly = self.db.query(AuditEntry).filter(
                AuditEntry.audit_report_id == audit_report_id,
                AuditEntry.status == AuditEntryStatus.LOCKED,
            ).count()
            return newly
        return max(0, limit - active_count)
```
> The `import_entries` re-scan dedup relies on the existing `KnowledgeSource` unique `(tenant_id, content_hash, version)` constraint and reuses `document_processor`'s job shape exactly (`document_text`/`document_name`/`source_type`/`source_category`/`source_url`/`metadata` keys) — verify against `app/workers/document_processor.py` input_data parsing.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_import.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/service.py tests/auditor/test_service_import.py`

---

### Task 17: Public endpoints + schemas + rate limit
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/schemas/auditor.py`
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/api/v1/endpoints/public_auditor.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/api/v1/api.py` (register router)
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_public_endpoints.py`

**Interfaces:**
- Consumes: `AuditService`, `AuditReport`/`AuditStatus`, `Lead`, `RedisRateLimiter`, `email_service`.
- Produces: Pydantic schemas `PublicAuditRequest(url: HttpUrl)`, `AuditScoresResponse`, `PublicReportResponse` (scores + counts + blurred gap titles, NO raw content), `LeadCaptureRequest(email: EmailStr)`; endpoints `POST /public/audit`, `GET /public/audit/{id}`, `POST /public/audit/{id}/email`. Module-level limiter `public_audit_limiter = RedisRateLimiter("public_audit", max_calls=3, window_seconds=86400)`; the POST acquires it keyed on `request.client.host`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_public_endpoints.py
import uuid
from unittest.mock import AsyncMock, patch

from app.db.models.audit import AuditReport, AuditScope, AuditStatus


def test_post_public_audit_starts_and_returns_id(client, db):
    with patch("app.api.v1.endpoints.public_auditor.public_audit_limiter.acquire", new=AsyncMock()), \
         patch("app.auditor.service.enqueue_audit"):
        resp = client.post("/api/v1/public/audit", json={"url": "https://example.com"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["success"] is True
    assert "audit_report_id" in body["data"]


def test_get_public_report_returns_scores_and_blurred_gaps(client, db):
    report = AuditReport(
        url="https://ex.com", domain="ex.com", scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED, overall_score=74, projected_score=91,
        knowledge_score=70, support_score=60, conversion_score=80,
        gaps=[{"title": "Refund policy", "why_it_matters": "trust",
               "recommended_content": "secret detail", "kind": "policy", "demand_count": 3}],
    )
    db.add(report)
    db.commit()
    resp = client.get(f"/api/v1/public/audit/{report.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overall_score"] == 74
    assert data["gaps"][0]["title"] == "Refund policy"
    assert "recommended_content" not in data["gaps"][0]  # blurred: no raw recs in public


def test_post_email_creates_lead(client, db):
    report = AuditReport(url="https://ex.com", domain="ex.com", scope=AuditScope.PUBLIC,
                        status=AuditStatus.COMPLETED, overall_score=74)
    db.add(report)
    db.commit()
    with patch("app.api.v1.endpoints.public_auditor.email_service.send_templated_email", return_value=True):
        resp = client.post(f"/api/v1/public/audit/{report.id}/email", json={"email": "owner@example.com"})
    assert resp.status_code == 200
    from app.db.models.lead import Lead
    assert db.query(Lead).filter(Lead.audit_report_id == report.id).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_public_endpoints.py -q`
Expected: FAIL — `404` (router not registered) / `ModuleNotFoundError: app.api.v1.endpoints.public_auditor`.

- [ ] **Step 3: Write minimal implementation**
```python
# app/schemas/auditor.py
"""Pydantic schemas for the Website Auditor endpoints."""

from uuid import UUID

from pydantic import BaseModel, EmailStr, HttpUrl


class PublicAuditRequest(BaseModel):
    url: HttpUrl


class BlurredGap(BaseModel):
    title: str
    kind: str
    demand_count: int


class AuditScoresResponse(BaseModel):
    knowledge_score: int | None
    support_score: int | None
    conversion_score: int | None
    overall_score: int | None
    projected_score: int | None


class PublicReportResponse(AuditScoresResponse):
    id: UUID
    status: str
    pages_scanned: int
    gaps: list[BlurredGap]


class LeadCaptureRequest(BaseModel):
    email: EmailStr
```
```python
# app/api/v1/endpoints/public_auditor.py
"""Public (no-auth) Website Auditor endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auditor.service import AuditService
from app.db.models.audit import AuditReport, AuditScope
from app.db.models.lead import Lead
from app.db.session import get_db
from app.schemas.auditor import LeadCaptureRequest, PublicAuditRequest, PublicReportResponse
from app.services.email_service import email_service
from app.utils.rate_limiter import RedisRateLimiter

router = APIRouter()
public_audit_limiter = RedisRateLimiter("public_audit", max_calls=3, window_seconds=86400)


def _err(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(status_code=http, content={"success": False, "error": {"code": code, "message": message, "details": {}}})


@router.post("/audit", status_code=status.HTTP_202_ACCEPTED)
async def start_public_audit(payload: PublicAuditRequest, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    await public_audit_limiter.acquire(client_ip)
    try:
        report = AuditService(db).start_audit(url=str(payload.url), scope=AuditScope.PUBLIC, tenant_id=None)
    except ValueError as exc:
        return _err("INVALID_URL", str(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"success": True, "data": {"audit_report_id": str(report.id), "claim_token": report.claim_token}},
    )


@router.get("/audit/{audit_id}")
def get_public_report(audit_id: UUID, db: Session = Depends(get_db)):
    report = db.query(AuditReport).filter(AuditReport.id == audit_id, AuditReport.scope == AuditScope.PUBLIC).one_or_none()
    if not report:
        return _err("AUDIT_NOT_FOUND", "Audit not found", status.HTTP_404_NOT_FOUND)
    blurred = [{"title": g.get("title"), "kind": g.get("kind"), "demand_count": g.get("demand_count", 0)} for g in (report.gaps or [])]
    data = PublicReportResponse(
        id=report.id, status=report.status.value, pages_scanned=report.pages_scanned,
        knowledge_score=report.knowledge_score, support_score=report.support_score,
        conversion_score=report.conversion_score, overall_score=report.overall_score,
        projected_score=report.projected_score, gaps=blurred,
    )
    return {"success": True, "data": data.model_dump(mode="json")}


@router.post("/audit/{audit_id}/email")
def capture_lead(audit_id: UUID, payload: LeadCaptureRequest, db: Session = Depends(get_db)):
    report = db.query(AuditReport).filter(AuditReport.id == audit_id, AuditReport.scope == AuditScope.PUBLIC).one_or_none()
    if not report:
        return _err("AUDIT_NOT_FOUND", "Audit not found", status.HTTP_404_NOT_FOUND)
    email = str(payload.email)
    lead = Lead(
        tenant_id=None, email=email, email_hash=Lead.compute_email_hash(email),
        source="public_auditor", audit_report_id=report.id,
    )
    db.add(lead)
    db.flush()
    report.email = email
    report.email_hash = Lead.compute_email_hash(email)
    report.lead_id = lead.id
    db.commit()
    email_service.send_templated_email(
        to_email=email, subject="Your AI Website Audit report",
        template_name="platform_broadcast.html",
        context={"recipient_name": email, "subject": "Your AI Website Audit report",
                 "heading": "Your audit is ready", "body_html": f"Overall score: {report.overall_score}",
                 "banner_label": "Website Audit", "accent_color": "#1d4ed8", "current_year": 2026},
    )
    return {"success": True, "data": {"lead_id": str(lead.id)}}
```
Register in `app/api/v1/api.py`: add `public_auditor` to the `from app.api.v1.endpoints import (...)` block and `api_router.include_router(public_auditor.router, prefix="/public", tags=["public-auditor"])`.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_public_endpoints.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/schemas/auditor.py app/api/v1/endpoints/public_auditor.py app/api/v1/api.py tests/auditor/test_public_endpoints.py`

---

### Task 18: Claim path (incremental resume) + unclaimed-purge cron
**Files:**
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/auditor/service.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/workers/document_processor.py` (purge cron)
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_service_claim.py`

**Interfaces:**
- Consumes: `AuditReport`/`AuditEntry`, `BusinessProfile`, `settings.AUDITOR_CLAIM_STALENESS_DAYS`/`AUDITOR_UNCLAIMED_PURGE_DAYS`.
- Produces: `def claim_audit(self, claim_token: str, tenant_id: UUID) -> AuditReport` — re-parents report+entries+profile to `tenant_id`; if `created_at` within staleness window → incremental resume (re-enqueue with reuse); else fresh full crawl (new job). Returns the (re-parented) report. ARQ cron `async def purge_unclaimed_audits(ctx: dict) -> dict` — deletes public reports with `tenant_id IS NULL` older than purge window. Registered in `WorkerSettings.functions` + `cron_jobs` (daily).

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_service_claim.py
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.auditor.service import AuditService
from app.db.models.audit import (
    AuditEntry, AuditEntryKind, AuditEntryStatus, AuditReport, AuditScope, AuditStatus,
)


def _public_report(db, *, age_days: int):
    report = AuditReport(tenant_id=None, url="https://ex.com", domain="ex.com",
                        scope=AuditScope.PUBLIC, status=AuditStatus.COMPLETED, claim_token="tok123")
    db.add(report)
    db.flush()
    report.created_at = datetime.now(UTC) - timedelta(days=age_days)
    db.add(AuditEntry(audit_report_id=report.id, tenant_id=None, url="https://ex.com/a", title="A",
                      category="c", kind=AuditEntryKind.FAQ, content="x", content_hash="h" * 64,
                      importance_rank=1, status=AuditEntryStatus.PROPOSED))
    db.flush()
    return report


def test_claim_fresh_reparents_report_and_entries(db):
    report = _public_report(db, age_days=1)
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit"):
        claimed = AuditService(db).claim_audit("tok123", tid)
    db.flush()
    assert claimed.tenant_id == tid
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).all()
    assert all(e.tenant_id == tid for e in entries)


def test_claim_stale_triggers_fresh_crawl(db):
    report = _public_report(db, age_days=10)  # > 7 day staleness
    tid = uuid.uuid4()
    with patch("app.auditor.service.enqueue_audit") as mock_enqueue:
        AuditService(db).claim_audit("tok123", tid)
    mock_enqueue.assert_called_once()  # fresh crawl re-enqueued


def test_purge_removes_old_unclaimed(db):
    from app.auditor.service import purge_unclaimed_audits_sync
    old = _public_report(db, age_days=40)
    db.flush()
    removed = purge_unclaimed_audits_sync(db)
    assert removed >= 1
    assert db.query(AuditReport).filter(AuditReport.id == old.id).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_claim.py -q`
Expected: FAIL — `AttributeError: 'AuditService' object has no attribute 'claim_audit'`.

- [ ] **Step 3: Write minimal implementation**
Append to `app/auditor/service.py`:
```python
    def claim_audit(self, claim_token: str, tenant_id: UUID) -> AuditReport:
        from datetime import UTC, datetime, timedelta

        from app.core.config import settings
        from app.db.models.audit import AuditEntry, AuditReport
        from app.db.models.business_profile import BusinessProfile
        from app.db.models.job import JobType, ProcessingJob
        from app.utils.datetime import ensure_utc_aware

        report = (
            self.db.query(AuditReport)
            .filter(AuditReport.claim_token == claim_token, AuditReport.tenant_id.is_(None))
            .one_or_none()
        )
        if report is None:
            raise ValueError("Invalid or already-claimed token")

        report.tenant_id = tenant_id
        self.db.query(AuditEntry).filter(AuditEntry.audit_report_id == report.id).update(
            {AuditEntry.tenant_id: tenant_id}
        )
        if report.business_profile_id:
            bp = self.db.query(BusinessProfile).filter(BusinessProfile.id == report.business_profile_id).one_or_none()
            if bp and bp.tenant_id is None:
                bp.tenant_id = tenant_id

        created = ensure_utc_aware(report.created_at)
        stale = created and (datetime.now(UTC) - created) > timedelta(days=settings.AUDITOR_CLAIM_STALENESS_DAYS)

        job = ProcessingJob(
            tenant_id=tenant_id, job_type=JobType.DOCUMENT_INGESTION,
            input_data={"kind": "website_audit", "resume": not stale, "url": report.url},
        )
        self.db.add(job)
        self.db.flush()
        report.processing_job_id = job.id
        self.db.commit()
        enqueue_audit(report.id)
        return report


def purge_unclaimed_audits_sync(db) -> int:
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.db.models.audit import AuditReport

    cutoff = datetime.now(UTC) - timedelta(days=settings.AUDITOR_UNCLAIMED_PURGE_DAYS)
    stale = (
        db.query(AuditReport)
        .filter(AuditReport.tenant_id.is_(None), AuditReport.created_at < cutoff)
        .all()
    )
    count = len(stale)
    for report in stale:
        db.delete(report)  # cascade deletes entries
    db.commit()
    return count
```
Add the cron worker to `app/workers/document_processor.py`:
```python
async def purge_unclaimed_audits(ctx: dict) -> dict:
    """Daily: drop public audit reports with no tenant after the purge window."""
    from app.auditor.service import purge_unclaimed_audits_sync
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        removed = purge_unclaimed_audits_sync(db)
        logger.info("purge_unclaimed_audits removed {} reports", removed)
        return {"status": "success", "removed": removed}
    finally:
        db.close()
```
Add `purge_unclaimed_audits` to `WorkerSettings.functions` and a `cron(purge_unclaimed_audits, hour=3, minute=30, run_at_startup=False)` to `cron_jobs`.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_service_claim.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/auditor/service.py app/workers/document_processor.py tests/auditor/test_service_claim.py`

---

### Task 19: Dashboard endpoints (authed)
**Files:**
- Create: `/Users/adebayovictor/Documents/dev/luran/luran/app/api/v1/endpoints/auditor.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/schemas/auditor.py`
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/api/v1/api.py`
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_dashboard_endpoints.py`

**Interfaces:**
- Consumes: `AuditService`, `get_current_tenant_admin`, `get_db`, `AuditReport`/`AuditEntry`.
- Produces: schemas `DashboardAuditRequest(url: HttpUrl)`, `EntryEditRequest(content: str | None, category: str | None, status: str | None)`, `AuditEntryResponse`, `GapResponse`; endpoints (all under `/audit`, tenant-admin auth, ownership re-checked by `tenant_id`): `POST /audit`, `GET /audit`, `GET /audit/{id}/entries`, `GET /audit/{id}/gaps`, `PATCH /audit/entries/{id}`, `POST /audit/{id}/import`. "Generate outline" forward-stub `POST /audit/{id}/gaps/{gap_index}/outline` → `501` `{"detail": "Available with Marketing (Stage 6)"}`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_dashboard_endpoints.py
from unittest.mock import patch

from app.db.models.audit import (
    AuditEntry, AuditEntryKind, AuditEntryStatus, AuditReport, AuditScope, AuditStatus,
)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_post_audit_requires_auth(pg_client):
    resp = pg_client.post("/api/v1/audit", json={"url": "https://example.com"})
    assert resp.status_code in (401, 403)


def test_post_audit_starts_dashboard_scope(pg_client, regular_user_token, db_session):
    with patch("app.auditor.service.enqueue_audit"):
        resp = pg_client.post("/api/v1/audit", json={"url": "https://example.com"}, headers=_auth(regular_user_token))
    assert resp.status_code == 202
    assert resp.json()["success"] is True


def test_generate_outline_is_forward_stub(pg_client, regular_user_token, db_session):
    from app.db.models.user import User
    user = db_session.query(User).first()
    report = AuditReport(tenant_id=user.tenant_id, url="https://ex.com", domain="ex.com",
                        scope=AuditScope.DASHBOARD, status=AuditStatus.COMPLETED, gaps=[{"title": "x"}])
    db_session.add(report)
    db_session.commit()
    resp = pg_client.post(f"/api/v1/audit/{report.id}/gaps/0/outline", headers=_auth(regular_user_token))
    assert resp.status_code == 501
    assert "Stage 6" in resp.json()["detail"]
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_dashboard_endpoints.py -q`
Expected: FAIL — `404` (router not registered).

- [ ] **Step 3: Write minimal implementation**
Add schemas to `app/schemas/auditor.py`:
```python
class DashboardAuditRequest(BaseModel):
    url: HttpUrl


class EntryEditRequest(BaseModel):
    content: str | None = None
    category: str | None = None
    status: str | None = None
```
```python
# app/api/v1/endpoints/auditor.py
"""Authenticated (tenant-admin) Website Auditor endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin
from app.auditor.service import AuditService
from app.db.models.audit import AuditEntry, AuditEntryStatus, AuditReport, AuditScope
from app.db.session import get_db
from app.schemas.auditor import DashboardAuditRequest, EntryEditRequest
from app.schemas.user import UserResponse

router = APIRouter()


def _owned_report(db: Session, audit_id: UUID, tenant_id) -> AuditReport:
    report = db.query(AuditReport).filter(AuditReport.id == audit_id, AuditReport.tenant_id == tenant_id).one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Audit not found")
    return report


@router.post("/audit", status_code=status.HTTP_202_ACCEPTED)
def start_dashboard_audit(payload: DashboardAuditRequest, db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    report = AuditService(db).start_audit(url=str(payload.url), scope=AuditScope.DASHBOARD, tenant_id=user.tenant_id, created_by=user.id)
    return {"success": True, "data": {"audit_report_id": str(report.id)}}


@router.get("/audit")
def list_audits(db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    rows = db.query(AuditReport).filter(AuditReport.tenant_id == user.tenant_id).order_by(AuditReport.created_at.desc()).all()
    return {"success": True, "data": [{"id": str(r.id), "url": r.url, "status": r.status.value,
                                       "overall_score": r.overall_score, "created_at": r.created_at.isoformat()} for r in rows]}


@router.get("/audit/{audit_id}/entries")
def list_entries(audit_id: UUID, db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    _owned_report(db, audit_id, user.tenant_id)
    entries = db.query(AuditEntry).filter(AuditEntry.audit_report_id == audit_id).order_by(AuditEntry.importance_rank.asc()).all()
    return {"success": True, "data": [{"id": str(e.id), "url": e.url, "title": e.title, "category": e.category,
                                       "kind": e.kind.value, "status": e.status.value, "importance_rank": e.importance_rank,
                                       "edited": e.edited} for e in entries]}


@router.get("/audit/{audit_id}/gaps")
def list_gaps(audit_id: UUID, db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    report = _owned_report(db, audit_id, user.tenant_id)
    return {"success": True, "data": report.gaps or []}


@router.patch("/audit/entries/{entry_id}")
def edit_entry(entry_id: UUID, payload: EntryEditRequest, db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    entry = db.query(AuditEntry).filter(AuditEntry.id == entry_id, AuditEntry.tenant_id == user.tenant_id).one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if payload.content is not None:
        entry.content = payload.content
    if payload.category is not None:
        entry.category = payload.category
    if payload.status is not None:
        entry.status = AuditEntryStatus(payload.status)
    entry.edited = True
    db.commit()
    return {"success": True, "data": {"id": str(entry.id), "edited": True}}


@router.post("/audit/{audit_id}/import")
def import_audit(audit_id: UUID, db: Session = Depends(get_db), user: UserResponse = Depends(get_current_tenant_admin)):
    _owned_report(db, audit_id, user.tenant_id)
    svc = AuditService(db)
    svc.preselect_entries(audit_id)
    imported = svc.import_entries(audit_id, uploaded_by=user.id)
    return {"success": True, "data": {"imported_count": len(imported)}}


@router.post("/audit/{audit_id}/gaps/{gap_index}/outline", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def generate_outline(audit_id: UUID, gap_index: int, user: UserResponse = Depends(get_current_tenant_admin)):
    raise HTTPException(status_code=501, detail="Available with Marketing (Stage 6)")
```
Register in `app/api/v1/api.py`: add `auditor` to the endpoints import block and `api_router.include_router(auditor.router, tags=["auditor"])` (no prefix — routes are self-prefixed with `/audit`).

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_dashboard_endpoints.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Stage changes**
Run: `git add app/api/v1/endpoints/auditor.py app/schemas/auditor.py app/api/v1/api.py tests/auditor/test_dashboard_endpoints.py`

---

### Task 20: Chat grounding — additive "About this business" block (regression-safe)
**Files:**
- Modify: `/Users/adebayovictor/Documents/dev/luran/luran/app/services/rag_service.py` (`assemble_prompt`)
- Test: `/Users/adebayovictor/Documents/dev/luran/luran/tests/auditor/test_chat_grounding.py`

**Interfaces:**
- Consumes: `BusinessProfile`.
- Produces: `assemble_prompt(..., business_profile: BusinessProfile | None = None)` — when a profile is present, appends a compact "About this business" block (summary + offerings + key_entities, ~150-token budget) to `tenant_config_parts` BEFORE the existing escalation/grounding lines remain intact; no-op when `business_profile is None`. New helper `RAGService._build_business_block(profile: BusinessProfile, *, token_budget: int = 150) -> str`.

- [ ] **Step 1: Write the failing test**
```python
# tests/auditor/test_chat_grounding.py
import uuid
from types import SimpleNamespace

from app.db.models.business_profile import BusinessProfile, BusinessProfileSource
from app.services.rag_service import RAGService


def _ai_config():
    return SimpleNamespace(
        brand_voice=None, allowed_topics=None, blocked_topics=None,
        grounding_requirement=False, grounding_mode="balanced",
        low_confidence_action="clarify", no_retrieval_action="clarify",
        custom_instructions=None,
    )


def _profile():
    return BusinessProfile(
        tenant_id=uuid.uuid4(), summary="Lagos fintech", industry="fintech",
        offerings=["wallet", "bills"], audience=[], positioning="",
        key_entities=["LuranPay"], tone={}, source=BusinessProfileSource.AUDITOR,
    )


def test_grounding_block_present_when_profile_supplied(db):
    svc = RAGService(db)
    out = svc.assemble_prompt(
        ai_config=_ai_config(), user_query="hi", retrieved_chunks=[], conversation_history=[],
        company_name="LuranPay", business_profile=_profile(),
    )
    assert "About this business" in out.full_prompt
    assert "wallet" in out.full_prompt
    # escalation/grounding lines unchanged
    assert "No-retrieval action: clarify" in out.full_prompt


def test_grounding_is_noop_when_profile_absent(db):
    svc = RAGService(db)
    out = svc.assemble_prompt(
        ai_config=_ai_config(), user_query="hi", retrieved_chunks=[], conversation_history=[],
        company_name="LuranPay",
    )
    assert "About this business" not in out.full_prompt
    assert "No-retrieval action: clarify" in out.full_prompt  # baseline behavior intact
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_chat_grounding.py -q`
Expected: FAIL — `TypeError: assemble_prompt() got an unexpected keyword argument 'business_profile'`.

- [ ] **Step 3: Write minimal implementation**
In `app/services/rag_service.py`, add the `business_profile` parameter to `assemble_prompt` (after `max_history_turns: int = 10`):
```python
        max_history_turns: int = 10,
        business_profile: "BusinessProfile | None" = None,
```
Add the import guard at top of the method body (or module TYPE_CHECKING block): `from app.db.models.business_profile import BusinessProfile` (place the runtime import inside the method to avoid a circular import; reference type via string in the signature).
Add the helper to the class:
```python
    def _build_business_block(self, profile, *, token_budget: int = 150) -> str:
        offerings = ", ".join((profile.offerings or [])[:6])
        entities = ", ".join((profile.key_entities or [])[:6])
        parts = ["About this business:"]
        if profile.summary:
            parts.append(profile.summary)
        if offerings:
            parts.append(f"Offerings: {offerings}")
        if entities:
            parts.append(f"Key entities: {entities}")
        block = " ".join(parts)
        # enforce ~150-token budget by truncation
        while count_tokens(block) > token_budget and len(block) > 0:
            block = block[: int(len(block) * 0.9)]
        return block
```
Insert the block append immediately AFTER `tenant_config_parts.append(f"Company: {company_name}")` / the description append and BEFORE the brand_voice handling, guarded:
```python
        if business_profile is not None:
            tenant_config_parts.append(self._build_business_block(business_profile))
```
> Placement before brand_voice keeps the grounding block ahead of KB context while leaving all escalation/grounding/topic lines byte-identical — the regression test asserts this.

- [ ] **Step 4: Run test to verify it passes**
Run: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest tests/auditor/test_chat_grounding.py -q && pytest tests/ -k "rag or assemble_prompt" -q`
Expected: PASS (2 passed); existing RAG prompt tests still green (no regression).

- [ ] **Step 5: Stage changes**
Run: `git add app/services/rag_service.py tests/auditor/test_chat_grounding.py`

---

## Final whole-feature gate (after all 20 tasks)

- [ ] Run full suite: `cd /Users/adebayovictor/Documents/dev/luran/luran && pytest -q` — all green (no regressions outside `tests/auditor/`).
- [ ] Lint/type: `ruff check app/auditor app/api/v1/endpoints/public_auditor.py app/api/v1/endpoints/auditor.py app/schemas/auditor.py && mypy app/auditor` — clean.
- [ ] Migration round-trip across all new migrations: `alembic upgrade head && alembic downgrade -4 && alembic upgrade head`.
- [ ] Cross-cutting review: SSRF guard invoked on root + every discovered URL; RAG never sees `locked`/`proposed` entries (only `imported`→`KnowledgeSource`); error envelope shape consistent; auditor makes no currency assumption; tenant isolation on every dashboard query (`tenant_id` filter).
- [ ] Commit ONCE (no `Co-Authored-By: Claude`), push branch `feat/stage4-website-auditor`, open PR off `develop`.
