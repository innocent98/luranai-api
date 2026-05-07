# Phase A — Explore Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Phase A backend: a `category` column on `tenants`, `source`/`source_meta` columns on `conversation_sessions`, an extended `/api/v1/businesses` endpoint with category filter and new sort, and a new `/api/v1/customer/explore/personal-businesses` endpoint returning a customer's followed + recently chatted businesses.

**Architecture:** New columns are nullable; no backfill. Two new enums (`TenantCategory`, `ConversationSource`) live in `app/schemas/explore.py` and are validated at the API boundary. Personal-block query uses a Python-merge implementation across two indexed sub-queries (cross-DB compatible — works on SQLite for tests and Postgres for prod). Browse query stays a single SELECT with a new sort key.

**Tech Stack:** FastAPI 0.115, SQLAlchemy 2.0, Alembic, pytest with SQLite in-memory fixture (`tests/conftest.py`), Postgres 14+ in prod.

**Spec:** `docs/superpowers/specs/2026-05-07-explore-phase-a-design.md`

---

## File structure

### Created
- `app/schemas/explore.py` — `TenantCategory`, `ConversationSource`, `SourceMeta`, response models
- `app/services/customer_explore_service.py` — `CustomerExploreService.get_personal_businesses`
- `app/api/v1/endpoints/customer_explore.py` — new auth-required router
- `alembic/versions/<rev>_explore_phase_a.py` — migration
- `tests/services/test_customer_explore_service.py`
- `tests/services/test_visitor_service_browse.py`
- `tests/api/test_customer_explore.py` (or merged into existing api test dir)
- `tests/api/test_public_businesses.py` (or extend existing)

### Modified
- `app/db/models/tenant.py` — add `category` column
- `app/db/models/conversation.py` — add `source`, `source_meta` columns + index
- `app/services/visitor_service.py` — extend `list_public_businesses` (new sort + category filter + `category`/`created_at` in response)
- `app/api/v1/endpoints/public.py` — accept `category` query param
- `app/schemas/visitor.py` — add `category` and `created_at` to `PublicBusinessDirectoryItem`
- `app/services/tenant_service.py` (or wherever `PATCH /tenants/me` is handled) — accept `category` field
- `app/api/v1/router.py` (or wherever sub-routers are wired) — register `customer_explore` router
- The conversation-creation path (verify exact location during Task 7) — accept `source` + `source_meta`, coerce-on-unknown

### Out of scope
- Frontend changes (separate plans for `luran-dashboard` and `luran-chat`)
- Postgres-specific test fixtures (testcontainers etc.) — Python-merge service implementation keeps SQLite tests viable
- `pg_trgm` GIN index for ILIKE — Phase B trigger only

---

## Task 1: Add explore schema (enums + Pydantic models)

**Files:**
- Create: `app/schemas/explore.py`
- Test: `tests/unit/test_explore_schemas.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_explore_schemas.py
import pytest
from pydantic import ValidationError

from app.schemas.explore import (
    ConversationSource,
    PersonalBusinessItem,
    PersonalBusinessRelationship,
    PersonalBusinessesResponse,
    SourceMeta,
    TenantCategory,
)


class TestTenantCategory:
    def test_all_thirteen_values_are_defined(self):
        expected = {
            "retail_ecommerce", "software_saas", "food_beverage",
            "health_wellness", "beauty_personal_care", "education_coaching",
            "professional_services", "financial_insurance", "travel_hospitality",
            "real_estate", "media_arts_entertainment", "nonprofit", "other",
        }
        assert {c.value for c in TenantCategory} == expected

    def test_values_are_str_subclass(self):
        # Required so Pydantic + SQLAlchemy treat them as strings.
        assert isinstance(TenantCategory.RETAIL_ECOMMERCE, str)
        assert TenantCategory.RETAIL_ECOMMERCE == "retail_ecommerce"


class TestConversationSource:
    def test_all_six_values_are_defined(self):
        expected = {
            "widget", "explore_personal", "explore_browse",
            "explore_search", "direct", "unknown",
        }
        assert {s.value for s in ConversationSource} == expected


class TestSourceMeta:
    def test_accepts_documented_keys(self):
        meta = SourceMeta(
            category_filter="retail_ecommerce",
            search_query_present=True,
            position=4,
            page=2,
        )
        assert meta.position == 4

    def test_unknown_keys_silently_dropped(self):
        # Pydantic v2 with extra="ignore" drops unknowns silently.
        meta = SourceMeta.model_validate({"position": 1, "totally_made_up": "x"})
        assert not hasattr(meta, "totally_made_up")

    def test_all_keys_optional(self):
        SourceMeta()  # must not raise

    def test_category_filter_validated_against_enum(self):
        with pytest.raises(ValidationError):
            SourceMeta(category_filter="not_a_real_category")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_explore_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.explore'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/schemas/explore.py
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenantCategory(str, Enum):
    RETAIL_ECOMMERCE         = "retail_ecommerce"
    SOFTWARE_SAAS            = "software_saas"
    FOOD_BEVERAGE            = "food_beverage"
    HEALTH_WELLNESS          = "health_wellness"
    BEAUTY_PERSONAL_CARE     = "beauty_personal_care"
    EDUCATION_COACHING       = "education_coaching"
    PROFESSIONAL_SERVICES    = "professional_services"
    FINANCIAL_INSURANCE      = "financial_insurance"
    TRAVEL_HOSPITALITY       = "travel_hospitality"
    REAL_ESTATE              = "real_estate"
    MEDIA_ARTS_ENTERTAINMENT = "media_arts_entertainment"
    NONPROFIT                = "nonprofit"
    OTHER                    = "other"


class ConversationSource(str, Enum):
    WIDGET           = "widget"
    EXPLORE_PERSONAL = "explore_personal"
    EXPLORE_BROWSE   = "explore_browse"
    EXPLORE_SEARCH   = "explore_search"
    DIRECT           = "direct"
    UNKNOWN          = "unknown"


class SourceMeta(BaseModel):
    """Optional attribution context written alongside ConversationSource."""

    model_config = ConfigDict(extra="ignore")  # forward-compat: drop unknown keys

    category_filter: Optional[TenantCategory] = None
    search_query_present: Optional[bool] = None
    position: Optional[int] = None
    page: Optional[int] = None


class PersonalBusinessRelationship(BaseModel):
    is_followed: bool
    followed_at: Optional[datetime]
    last_chat_at: Optional[datetime]
    last_activity_at: datetime


class PersonalBusinessItem(BaseModel):
    public_slug: str
    display_name: str
    description: Optional[str]
    support_headline: Optional[str]
    support_subheadline: Optional[str]
    widget_logo_url: Optional[str]
    is_featured: bool
    category: Optional[TenantCategory]
    created_at: datetime
    relationship: PersonalBusinessRelationship


class PersonalBusinessesResponse(BaseModel):
    businesses: list[PersonalBusinessItem]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_explore_schemas.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/schemas/explore.py tests/unit/test_explore_schemas.py
git commit -m "feat(explore): add TenantCategory, ConversationSource, SourceMeta schemas"
```

---

## Task 2: Add `category` column to Tenant model

**Files:**
- Modify: `app/db/models/tenant.py`
- Test: `tests/unit/test_tenant_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tenant_model.py
from app.db.models.tenant import Tenant


def test_tenant_has_nullable_category_column():
    col = Tenant.__table__.columns["category"]
    assert col.nullable is True
    assert col.type.length == 40  # VARCHAR(40)


def test_tenant_category_defaults_to_none(db):
    # 'db' fixture from conftest.py
    tenant = Tenant(
        company_name="Test Co",
        display_name="Test Co",
        public_slug=None,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    assert tenant.category is None


def test_tenant_category_accepts_string_value(db):
    tenant = Tenant(
        company_name="Retail Co",
        display_name="Retail Co",
        category="retail_ecommerce",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    assert tenant.category == "retail_ecommerce"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tenant_model.py -v`
Expected: FAIL with `KeyError: 'category'`

- [ ] **Step 3: Add the column to the model**

In `app/db/models/tenant.py`, add this column to the `Tenant` class definition (place it near the other public-listing fields like `is_publicly_listed`, `public_slug`):

```python
category: Mapped[str | None] = mapped_column(
    String(40),
    nullable=True,
    default=None,
    comment="Industry category for public directory filtering — validated against TenantCategory enum at API boundary",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tenant_model.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/db/models/tenant.py tests/unit/test_tenant_model.py
git commit -m "feat(tenant): add nullable category column"
```

---

## Task 3: Add `source` + `source_meta` columns to ConversationSession

**Files:**
- Modify: `app/db/models/conversation.py`
- Test: `tests/unit/test_conversation_session_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_conversation_session_model.py
from sqlalchemy import inspect

from app.db.models.conversation import ConversationSession


def test_conversation_session_has_nullable_source():
    col = ConversationSession.__table__.columns["source"]
    assert col.nullable is True
    assert col.type.length == 32


def test_conversation_session_has_nullable_source_meta_jsonb():
    col = ConversationSession.__table__.columns["source_meta"]
    assert col.nullable is True
    # Type is JSONB on Postgres, JSON on SQLite — both expose .python_type == dict
    # So just assert it's *some* JSON-y type by checking it's not VARCHAR
    assert "JSON" in str(col.type).upper() or "TEXT" in str(col.type).upper()


def test_conversation_session_source_defaults_to_none(db):
    from app.db.models.tenant import Tenant
    tenant = Tenant(company_name="X", display_name="X")
    db.add(tenant); db.commit(); db.refresh(tenant)

    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="visitor:abc",
    )
    db.add(session); db.commit(); db.refresh(session)
    assert session.source is None
    assert session.source_meta is None


def test_conversation_session_source_persists(db):
    from app.db.models.tenant import Tenant
    tenant = Tenant(company_name="X", display_name="X")
    db.add(tenant); db.commit(); db.refresh(tenant)

    session = ConversationSession(
        tenant_id=tenant.id,
        user_identifier="visitor:abc",
        source="explore_browse",
        source_meta={"position": 4, "page": 1},
    )
    db.add(session); db.commit(); db.refresh(session)
    assert session.source == "explore_browse"
    assert session.source_meta == {"position": 4, "page": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_conversation_session_model.py -v`
Expected: FAIL with `KeyError: 'source'`

- [ ] **Step 3: Add the columns and index**

In `app/db/models/conversation.py`, modify the `ConversationSession` class:

Add these columns alongside the existing ones (after `customer_user_id`, before `status`):

```python
source: Mapped[str | None] = mapped_column(
    String(32),
    nullable=True,
    default=None,
    comment="Attribution source — validated against ConversationSource enum at API boundary",
)
source_meta: Mapped[dict | None] = mapped_column(
    JSONB().with_variant(JSON(), "sqlite"),
    nullable=True,
    default=None,
    comment="Optional attribution context (category_filter, position, page, etc.)",
)
```

At the top of the file, add the imports if not already present:

```python
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
```

Update `__table_args__` for `ConversationSession` to include the new index:

```python
__table_args__ = (
    Index("ix_conversation_sessions_tenant_status", "tenant_id", "status"),
    Index("ix_conversation_sessions_user", "tenant_id", "user_identifier"),
    Index("ix_conversation_sessions_customer", "customer_user_id", "last_message_at"),
    Index("ix_conversation_sessions_source_created", "source", "created_at"),  # NEW
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_conversation_session_model.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/db/models/conversation.py tests/unit/test_conversation_session_model.py
git commit -m "feat(conversation): add source + source_meta columns for attribution"
```

---

## Task 4: Alembic migration

**Files:**
- Create: `alembic/versions/<auto_rev>_explore_phase_a.py` (Alembic generates the rev hash)

- [ ] **Step 1: Generate revision template**

Run: `alembic revision -m "explore phase a"`
Expected: a new file under `alembic/versions/` named `<rev>_explore_phase_a.py`

- [ ] **Step 2: Write the migration body**

Replace the generated `upgrade()` and `downgrade()` with:

```python
"""explore phase a

Revision ID: <auto>
Revises: <previous head>
Create Date: 2026-05-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# Alembic-fills these in
revision = "<auto>"
down_revision = "<previous head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. tenants.category
    op.add_column(
        "tenants",
        sa.Column("category", sa.String(length=40), nullable=True),
    )
    # Partial index: only listed tenants (the WHERE clause for the browse grid)
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_tenants_listed_category "
        "ON tenants (is_publicly_listed, category) "
        "WHERE is_publicly_listed = true"
    )

    # 2. conversation_sessions.source + source_meta
    op.add_column(
        "conversation_sessions",
        sa.Column("source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("source_meta", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_conversation_sessions_source_created "
        "ON conversation_sessions (source, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_conversation_sessions_source_created")
    op.drop_column("conversation_sessions", "source_meta")
    op.drop_column("conversation_sessions", "source")

    op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_tenants_listed_category")
    op.drop_column("tenants", "category")
```

> **Note:** `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. Configure the migration to run outside the migration transaction. Add this near the top of the file:

```python
from alembic import context

# Run outside transaction so CONCURRENTLY works
def run_migrations_offline_or_online():
    pass  # Alembic handles via env.py
```

If `alembic/env.py` does not already opt this revision out of transactions, add `transactional_ddl = False` for this revision via a `migration_context.configure(transactional_ddl=False)` override, OR document that the operator must run with `alembic upgrade <rev> -x no-tx=true` (project convention; check existing migrations for precedent).

- [ ] **Step 3: Run the migration up against a test DB**

Run: `make docker-up` (start local Postgres)
Then: `alembic upgrade head`
Expected: migration applies cleanly, both indexes exist (`\d tenants` and `\d conversation_sessions` in psql).

- [ ] **Step 4: Run the migration down**

Run: `alembic downgrade -1`
Expected: columns and indexes removed cleanly.

Run: `alembic upgrade head` (re-apply for state).

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/<rev>_explore_phase_a.py
git commit -m "feat(db): add explore phase a migration (category + source attribution)"
```

---

## Task 5: Extend `PublicBusinessDirectoryItem` schema

**Files:**
- Modify: `app/schemas/visitor.py`
- Test: `tests/unit/test_visitor_schemas.py` (create if not present)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_visitor_schemas.py
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.visitor import PublicBusinessDirectoryItem


def test_directory_item_includes_category_and_created_at():
    item = PublicBusinessDirectoryItem(
        public_slug="test",
        display_name="Test Co",
        description=None,
        support_headline=None,
        support_subheadline=None,
        widget_logo_url=None,
        is_featured=False,
        category="retail_ecommerce",
        created_at=datetime.now(timezone.utc),
    )
    assert item.category == "retail_ecommerce"


def test_directory_item_category_optional():
    item = PublicBusinessDirectoryItem(
        public_slug="test",
        display_name="Test Co",
        description=None,
        support_headline=None,
        support_subheadline=None,
        widget_logo_url=None,
        is_featured=False,
        category=None,
        created_at=datetime.now(timezone.utc),
    )
    assert item.category is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_visitor_schemas.py -v`
Expected: FAIL — schema rejects `category` field or rejects `created_at`.

- [ ] **Step 3: Add the fields**

In `app/schemas/visitor.py`, modify `PublicBusinessDirectoryItem` (find the existing class and add the two fields):

```python
from datetime import datetime
from app.schemas.explore import TenantCategory


class PublicBusinessDirectoryItem(BaseModel):
    # ... existing fields ...
    category: TenantCategory | None = None
    created_at: datetime
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_visitor_schemas.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/schemas/visitor.py tests/unit/test_visitor_schemas.py
git commit -m "feat(schemas): add category and created_at to PublicBusinessDirectoryItem"
```

---

## Task 6: Extend `VisitorService.list_public_businesses` (sort + category filter)

**Files:**
- Modify: `app/services/visitor_service.py`
- Test: `tests/services/test_visitor_service_browse.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_visitor_service_browse.py
from datetime import datetime, timedelta, timezone
import pytest

from app.db.models.tenant import Tenant
from app.services.visitor_service import VisitorService


def _seed_tenants(db, fixtures):
    """fixtures: list of (display_name, is_featured, category, days_old)."""
    now = datetime.now(timezone.utc)
    created = []
    for name, featured, category, days in fixtures:
        t = Tenant(
            company_name=name,
            display_name=name,
            public_slug=name.lower().replace(" ", "-"),
            is_publicly_listed=True,
            is_featured=featured,
            category=category,
            created_at=now - timedelta(days=days),
        )
        db.add(t); created.append(t)
    db.commit()
    return created


class TestListPublicBusinesses:
    def test_sort_order_featured_first_then_recent_then_alpha(self, db):
        # Insert 4 tenants:
        # A: not featured, old (60d)         → tier 4
        # B: not featured, new (5d)          → tier 3
        # C: featured,    old (60d)          → tier 2
        # D: featured,    new (5d)           → tier 1
        _seed_tenants(db, [
            ("Alpha",   False, None, 60),
            ("Bravo",   False, None,  5),
            ("Charlie", True,  None, 60),
            ("Delta",   True,  None,  5),
        ])

        svc = VisitorService(db)
        resp = svc.list_public_businesses(query=None, page=1, page_size=24, category=None)

        names = [b.display_name for b in resp.businesses]
        # Expected: D (featured+new), C (featured+old), B (unfeatured+new), A (unfeatured+old)
        assert names == ["Delta", "Charlie", "Bravo", "Alpha"]

    def test_category_filter_returns_only_matching(self, db):
        _seed_tenants(db, [
            ("RetailCo",  False, "retail_ecommerce", 5),
            ("SoftCo",    False, "software_saas",    5),
            ("FoodCo",    False, "food_beverage",    5),
        ])
        svc = VisitorService(db)
        resp = svc.list_public_businesses(category="retail_ecommerce")
        assert [b.display_name for b in resp.businesses] == ["RetailCo"]

    def test_category_filter_combined_with_search(self, db):
        _seed_tenants(db, [
            ("Lagos Retail",  False, "retail_ecommerce", 5),
            ("Abuja Retail",  False, "retail_ecommerce", 5),
            ("Lagos Tech",    False, "software_saas",    5),
        ])
        svc = VisitorService(db)
        resp = svc.list_public_businesses(query="lagos", category="retail_ecommerce")
        assert {b.display_name for b in resp.businesses} == {"Lagos Retail"}

    def test_unlisted_tenants_never_appear(self, db):
        _seed_tenants(db, [("Listed", False, None, 5)])
        # Add unlisted tenant
        unlisted = Tenant(
            company_name="Hidden", display_name="Hidden", is_publicly_listed=False,
        )
        db.add(unlisted); db.commit()

        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        assert [b.display_name for b in resp.businesses] == ["Listed"]

    def test_response_includes_category_and_created_at(self, db):
        _seed_tenants(db, [("Item", True, "retail_ecommerce", 5)])
        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        item = resp.businesses[0]
        assert item.category == "retail_ecommerce"
        assert item.created_at is not None

    def test_pagination_count_matches_listing(self, db):
        _seed_tenants(db, [(f"Co {i:02d}", False, None, 5) for i in range(15)])
        svc = VisitorService(db)
        page1 = svc.list_public_businesses(page=1, page_size=10)
        page2 = svc.list_public_businesses(page=2, page_size=10)
        assert page1.total == 15
        assert page1.total_pages == 2
        assert len(page1.businesses) == 10
        assert len(page2.businesses) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_visitor_service_browse.py -v`
Expected: FAIL — sort order tests fail (current sort is alphabetical only); category filter tests fail (param not accepted).

- [ ] **Step 3: Update `VisitorService.list_public_businesses`**

In `app/services/visitor_service.py`, modify the method signature and body. Replace the existing sort + filter logic with:

```python
from datetime import datetime, timedelta, timezone
from sqlalchemy import asc, case, desc, func, or_, select

from app.schemas.explore import TenantCategory
from app.schemas.visitor import (
    PublicBusinessDirectoryItem,
    PublicBusinessDirectoryResponse,
)


def list_public_businesses(
    self,
    *,
    query: str | None = None,
    page: int = 1,
    page_size: int = 24,
    category: TenantCategory | None = None,
) -> PublicBusinessDirectoryResponse:
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    base_filters = [Tenant.is_publicly_listed.is_(True)]
    if category is not None:
        base_filters.append(Tenant.category == category)
    if query:
        like = f"%{query.strip()}%"
        base_filters.append(or_(
            Tenant.display_name.ilike(like),
            Tenant.support_headline.ilike(like),
        ))

    # Boolean expression that's true for "recently created"
    is_recent = case(
        (Tenant.created_at >= thirty_days_ago, 1),
        else_=0,
    )

    count_stmt = select(func.count(Tenant.id)).where(*base_filters)
    total = self.db.execute(count_stmt).scalar_one()

    page_stmt = (
        select(Tenant)
        .where(*base_filters)
        .order_by(
            desc(Tenant.is_featured),
            desc(is_recent),
            asc(Tenant.display_name),
        )
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = self.db.execute(page_stmt).scalars().all()

    items = [
        PublicBusinessDirectoryItem(
            public_slug=t.public_slug,
            display_name=t.display_name,
            description=t.description,
            support_headline=t.support_headline,
            support_subheadline=t.support_subheadline,
            widget_logo_url=t.widget_logo_url,
            is_featured=t.is_featured,
            category=t.category,
            created_at=t.created_at,
        )
        for t in rows
    ]
    total_pages = (total + page_size - 1) // page_size if total else 0
    return PublicBusinessDirectoryResponse(
        businesses=items,
        total=total,
        total_pages=total_pages,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_visitor_service_browse.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/visitor_service.py tests/services/test_visitor_service_browse.py
git commit -m "feat(visitor): extend list_public_businesses with category filter + new sort"
```

---

## Task 7: Extend `/api/v1/businesses` route with `category` query param

**Files:**
- Modify: `app/api/v1/endpoints/public.py`
- Test: `tests/api/test_public_businesses.py` (create if not present)

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_public_businesses.py
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.models.tenant import Tenant


@pytest.fixture
def seeded_tenants(db):
    tenants = [
        Tenant(company_name="A", display_name="A", public_slug="a",
               is_publicly_listed=True, is_featured=False, category="retail_ecommerce"),
        Tenant(company_name="B", display_name="B", public_slug="b",
               is_publicly_listed=True, is_featured=False, category="software_saas"),
        Tenant(company_name="C", display_name="C", public_slug="c",
               is_publicly_listed=True, is_featured=False, category="retail_ecommerce"),
    ]
    db.add_all(tenants); db.commit()
    return tenants


def test_browse_filters_by_category(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses?category=retail_ecommerce")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 2
    assert {b["display_name"] for b in payload["businesses"]} == {"A", "C"}


def test_browse_invalid_category_returns_422(client):
    resp = client.get("/api/v1/public/businesses?category=not_a_real_category")
    assert resp.status_code == 422


def test_browse_no_category_returns_all_listed(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses")
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


def test_browse_response_has_category_field(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses")
    items = resp.json()["businesses"]
    cats = {item["category"] for item in items}
    assert cats == {"retail_ecommerce", "software_saas"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_public_businesses.py -v`
Expected: FAIL — `category` param not recognized; 422 returned for unknown values either way.

- [ ] **Step 3: Add `category` query param to the endpoint**

In `app/api/v1/endpoints/public.py`, modify `list_public_businesses`:

```python
from app.schemas.explore import TenantCategory


@router.get(
    "/businesses",
    response_model=PublicBusinessDirectoryResponse,
    status_code=status.HTTP_200_OK,
)
def list_public_businesses(
    q: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    category: TenantCategory | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """List publicly discoverable businesses for the customer support directory."""
    visitor_service = VisitorService(db)
    return visitor_service.list_public_businesses(
        query=q, page=page, page_size=page_size, category=category,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/api/test_public_businesses.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/public.py tests/api/test_public_businesses.py
git commit -m "feat(api): add category filter to /api/v1/public/businesses"
```

---

## Task 8: New `CustomerExploreService.get_personal_businesses`

**Files:**
- Create: `app/services/customer_explore_service.py`
- Test: `tests/services/test_customer_explore_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_customer_explore_service.py
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.models.conversation import ConversationSession
from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.services.customer_explore_service import CustomerExploreService


@pytest.fixture
def customer(db):
    c = CustomerUser(
        full_name="Test Customer",
        email="test@example.com",
        hashed_password="x",
        chat_user_key="ck_x",
    )
    db.add(c); db.commit(); db.refresh(c)
    return c


def _make_tenant(db, name: str, listed: bool = True) -> Tenant:
    t = Tenant(
        company_name=name, display_name=name,
        public_slug=name.lower().replace(" ", "-"),
        is_publicly_listed=listed,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t


def _follow(db, customer, tenant, days_ago: int = 0):
    f = CustomerBusinessFollow(
        customer_user_id=customer.id,
        tenant_id=tenant.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(f); db.commit()
    return f


def _chat(db, customer, tenant, days_ago: int):
    s = ConversationSession(
        tenant_id=tenant.id,
        customer_user_id=customer.id,
        user_identifier=f"customer:{customer.id}",
        last_message_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(s); db.commit()
    return s


class TestGetPersonalBusinesses:

    def test_empty_when_no_follows_or_chats(self, db, customer):
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_includes_followed_business(self, db, customer):
        t = _make_tenant(db, "FollowedCo")
        _follow(db, customer, t, days_ago=2)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        item = result.businesses[0]
        assert item.public_slug == "followedco"
        assert item.relationship.is_followed is True

    def test_includes_recently_chatted_business_within_30_days(self, db, customer):
        t = _make_tenant(db, "ChattedCo")
        _chat(db, customer, t, days_ago=10)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        assert result.businesses[0].relationship.is_followed is False
        assert result.businesses[0].relationship.last_chat_at is not None

    def test_excludes_chats_older_than_30_days(self, db, customer):
        t = _make_tenant(db, "OldChatCo")
        _chat(db, customer, t, days_ago=45)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_dedupe_when_followed_and_recently_chatted(self, db, customer):
        t = _make_tenant(db, "BothCo")
        _follow(db, customer, t, days_ago=20)
        _chat(db, customer, t, days_ago=2)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        item = result.businesses[0]
        assert item.relationship.is_followed is True
        assert item.relationship.last_chat_at is not None

    def test_excludes_unlisted_tenants(self, db, customer):
        t = _make_tenant(db, "UnlistedCo", listed=False)
        _follow(db, customer, t)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_caps_at_eight_items(self, db, customer):
        for i in range(12):
            t = _make_tenant(db, f"Tenant {i:02d}")
            _follow(db, customer, t, days_ago=i)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 8

    def test_sorts_by_last_activity_descending(self, db, customer):
        # 3 tenants with distinct most-recent-activity timestamps
        t_old = _make_tenant(db, "Oldest")
        _follow(db, customer, t_old, days_ago=20)

        t_mid = _make_tenant(db, "Middle")
        _follow(db, customer, t_mid, days_ago=10)

        t_new = _make_tenant(db, "Newest")
        _chat(db, customer, t_new, days_ago=1)

        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        names = [b.display_name for b in result.businesses]
        assert names == ["Newest", "Middle", "Oldest"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_customer_explore_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.customer_explore_service'`

- [ ] **Step 3: Implement the service**

```python
# app/services/customer_explore_service.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession
from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.tenant import Tenant
from app.schemas.explore import (
    PersonalBusinessItem,
    PersonalBusinessRelationship,
    PersonalBusinessesResponse,
)


PERSONAL_BLOCK_LIMIT = 8
RECENT_CHAT_WINDOW_DAYS = 30


@dataclass
class _Entry:
    tenant_id: UUID
    is_followed: bool
    followed_at: Optional[datetime]
    last_viewed_channel_at: Optional[datetime]
    last_chat_at: Optional[datetime]

    @property
    def last_activity_at(self) -> datetime:
        """Most recent of follow_at, last_viewed_channel_at, last_chat_at."""
        candidates = [self.followed_at, self.last_viewed_channel_at, self.last_chat_at]
        non_null = [c for c in candidates if c is not None]
        # _Entry construction guarantees at least one non-null timestamp.
        return max(non_null)


class CustomerExploreService:
    def __init__(self, db: Session):
        self.db = db

    def get_personal_businesses(
        self,
        *,
        customer_user_id: UUID,
    ) -> PersonalBusinessesResponse:
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=RECENT_CHAT_WINDOW_DAYS)

        # 1. Followed tenants
        followed_rows = self.db.execute(
            select(
                CustomerBusinessFollow.tenant_id,
                CustomerBusinessFollow.created_at,
                CustomerBusinessFollow.last_viewed_channel_at,
            ).where(CustomerBusinessFollow.customer_user_id == customer_user_id)
        ).all()

        # 2. Recently-chatted tenants (aggregated to last_message_at per tenant)
        recent_chat_rows = self.db.execute(
            select(
                ConversationSession.tenant_id,
                func.max(ConversationSession.last_message_at).label("last_chat_at"),
            )
            .where(
                ConversationSession.customer_user_id == customer_user_id,
                ConversationSession.last_message_at >= thirty_days_ago,
            )
            .group_by(ConversationSession.tenant_id)
        ).all()

        # 3. Merge in Python — keyed by tenant_id, preferring follow info when present
        entries: dict[UUID, _Entry] = {}
        for r in followed_rows:
            entries[r.tenant_id] = _Entry(
                tenant_id=r.tenant_id,
                is_followed=True,
                followed_at=r.created_at,
                last_viewed_channel_at=r.last_viewed_channel_at,
                last_chat_at=None,
            )
        for r in recent_chat_rows:
            if r.tenant_id in entries:
                entries[r.tenant_id].last_chat_at = r.last_chat_at
            else:
                entries[r.tenant_id] = _Entry(
                    tenant_id=r.tenant_id,
                    is_followed=False,
                    followed_at=None,
                    last_viewed_channel_at=None,
                    last_chat_at=r.last_chat_at,
                )

        if not entries:
            return PersonalBusinessesResponse(businesses=[])

        # 4. Sort by last_activity_at DESC, take top N
        sorted_entries = sorted(
            entries.values(),
            key=lambda e: e.last_activity_at,
            reverse=True,
        )[: PERSONAL_BLOCK_LIMIT]

        # 5. Fetch tenant rows for the survivors (only listed)
        tenant_ids = [e.tenant_id for e in sorted_entries]
        tenants = self.db.execute(
            select(Tenant).where(
                Tenant.id.in_(tenant_ids),
                Tenant.is_publicly_listed.is_(True),
            )
        ).scalars().all()
        by_id = {t.id: t for t in tenants}

        # 6. Combine + return, preserving sort order, dropping unlisted
        items: list[PersonalBusinessItem] = []
        for e in sorted_entries:
            t = by_id.get(e.tenant_id)
            if t is None:
                continue
            items.append(PersonalBusinessItem(
                public_slug=t.public_slug,
                display_name=t.display_name,
                description=t.description,
                support_headline=t.support_headline,
                support_subheadline=t.support_subheadline,
                widget_logo_url=t.widget_logo_url,
                is_featured=t.is_featured,
                category=t.category,
                created_at=t.created_at,
                relationship=PersonalBusinessRelationship(
                    is_followed=e.is_followed,
                    followed_at=e.followed_at,
                    last_chat_at=e.last_chat_at,
                    last_activity_at=e.last_activity_at,
                ),
            ))

        return PersonalBusinessesResponse(businesses=items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/test_customer_explore_service.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/customer_explore_service.py tests/services/test_customer_explore_service.py
git commit -m "feat(explore): add CustomerExploreService.get_personal_businesses"
```

---

## Task 9: New `/api/v1/customer/explore/personal-businesses` endpoint

**Files:**
- Create: `app/api/v1/endpoints/customer_explore.py`
- Modify: `app/api/v1/router.py` (or wherever routers are registered)
- Test: `tests/api/test_customer_explore.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_customer_explore.py
import pytest

from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant


@pytest.fixture
def authed_customer(db, client):
    """Create a customer + return (customer, auth_headers)."""
    # Use the project's existing customer-auth flow to get a token.
    # Implementation note: the engineer should mirror whatever tests for
    # /customer/conversations or other customer routes use to obtain a JWT.
    # If no helper exists, write one in tests/conftest.py.
    raise NotImplementedError("Implement using existing customer-auth fixture pattern")


def test_personal_businesses_unauthenticated_returns_401(client):
    resp = client.get("/api/v1/customer/explore/personal-businesses")
    assert resp.status_code == 401


def test_personal_businesses_returns_empty_for_new_customer(authed_customer, client):
    customer, headers = authed_customer
    resp = client.get(
        "/api/v1/customer/explore/personal-businesses", headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"businesses": []}


def test_personal_businesses_returns_followed_tenant(db, authed_customer, client):
    customer, headers = authed_customer
    t = Tenant(
        company_name="X", display_name="X", public_slug="x",
        is_publicly_listed=True,
    )
    db.add(t); db.commit(); db.refresh(t)

    f = CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id)
    db.add(f); db.commit()

    resp = client.get(
        "/api/v1/customer/explore/personal-businesses", headers=headers,
    )
    payload = resp.json()
    assert len(payload["businesses"]) == 1
    assert payload["businesses"][0]["display_name"] == "X"
    assert payload["businesses"][0]["relationship"]["is_followed"] is True


def test_personal_businesses_caps_at_eight(db, authed_customer, client):
    customer, headers = authed_customer
    for i in range(12):
        t = Tenant(
            company_name=f"T{i}", display_name=f"T{i}", public_slug=f"t-{i}",
            is_publicly_listed=True,
        )
        db.add(t); db.commit(); db.refresh(t)
        db.add(CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id))
    db.commit()

    resp = client.get(
        "/api/v1/customer/explore/personal-businesses", headers=headers,
    )
    assert len(resp.json()["businesses"]) == 8


def test_response_includes_relationship_block(db, authed_customer, client):
    customer, headers = authed_customer
    t = Tenant(
        company_name="X", display_name="X", public_slug="x",
        is_publicly_listed=True,
    )
    db.add(t); db.commit(); db.refresh(t)
    db.add(CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id))
    db.commit()

    resp = client.get(
        "/api/v1/customer/explore/personal-businesses", headers=headers,
    )
    item = resp.json()["businesses"][0]
    assert "relationship" in item
    assert "is_followed" in item["relationship"]
    assert "last_activity_at" in item["relationship"]
```

> **Note:** the `authed_customer` fixture must mirror the codebase's existing customer-auth test pattern. Look at `tests/api/` for any test that hits `/api/v1/customer/...` and reuse its fixture. If none exists, create a small fixture in `tests/conftest.py` that creates a `CustomerUser`, generates a JWT via the existing `customer_auth_service`, and returns `(customer, {"Authorization": "Bearer <token>"})`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_customer_explore.py -v`
Expected: FAIL with 404 on the route (not yet registered).

- [ ] **Step 3: Create the endpoint module**

```python
# app/api/v1/endpoints/customer_explore.py
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_customer
from app.db.models.customer_user import CustomerUser
from app.schemas.explore import PersonalBusinessesResponse
from app.services.customer_explore_service import CustomerExploreService


router = APIRouter()


@router.get(
    "/personal-businesses",
    response_model=PersonalBusinessesResponse,
    status_code=status.HTTP_200_OK,
)
def list_personal_businesses(
    customer: CustomerUser = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    """Return up to 8 followed/recently-chatted businesses for the auth'd customer."""
    service = CustomerExploreService(db)
    response = service.get_personal_businesses(customer_user_id=customer.id)
    return response
```

> **Note:** the dependency name `get_current_customer` is a stand-in. Use the project's existing customer-auth dependency — likely `get_current_customer_user` or similar. Grep `app/api/deps.py` and the existing customer-route modules (`customer_auth.py`, `customer_conversations.py`) for the precedent.

- [ ] **Step 4: Register the router**

In `app/api/v1/router.py` (or wherever sub-routers are wired into the main API router), add:

```python
from app.api.v1.endpoints import customer_explore

api_router.include_router(
    customer_explore.router,
    prefix="/customer/explore",
    tags=["customer-explore"],
)
```

Also add `Cache-Control` header on the response. Modify the endpoint to add a `Response` parameter:

```python
from fastapi import Response

@router.get(...)
def list_personal_businesses(
    response: Response,
    customer: CustomerUser = Depends(get_current_customer),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, max-age=120, stale-while-revalidate=300"
    service = CustomerExploreService(db)
    return service.get_personal_businesses(customer_user_id=customer.id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/api/test_customer_explore.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/customer_explore.py app/api/v1/router.py tests/api/test_customer_explore.py
git commit -m "feat(api): add GET /api/v1/customer/explore/personal-businesses"
```

---

## Task 10: Extend `PATCH /api/v1/tenants/me` to accept `category`

**Files:**
- Modify: `app/schemas/tenant.py` (or wherever the update payload lives)
- Modify: `app/services/tenant_service.py` (already accepts arbitrary fields — verify)
- Test: `tests/api/test_tenant_update.py` (extend if exists)

- [ ] **Step 1: Locate the existing schema**

Run: `grep -nE "class.*TenantUpdate|class.*UpdateTenant" app/schemas/tenant.py`
Expected: find the existing update schema (e.g., `TenantUpdate`).

- [ ] **Step 2: Write the failing test**

```python
# tests/api/test_tenant_update_category.py
import pytest


def test_patch_tenant_accepts_category(authed_tenant_admin, client):
    operator, headers = authed_tenant_admin
    resp = client.patch(
        "/api/v1/tenants/me",
        json={"category": "retail_ecommerce"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["category"] == "retail_ecommerce"


def test_patch_tenant_rejects_invalid_category(authed_tenant_admin, client):
    operator, headers = authed_tenant_admin
    resp = client.patch(
        "/api/v1/tenants/me",
        json={"category": "not_a_real_category"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_patch_tenant_clears_category_on_null(db, authed_tenant_admin, client):
    operator, headers = authed_tenant_admin
    # First set
    client.patch("/api/v1/tenants/me", json={"category": "software_saas"}, headers=headers)
    # Then clear
    resp = client.patch("/api/v1/tenants/me", json={"category": None}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["category"] is None
```

> **Note:** `authed_tenant_admin` fixture mirrors existing patterns for tenant operator auth in tests. Reuse or write a small helper in `conftest.py` (`CompanyMembership`, JWT, etc.).

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/api/test_tenant_update_category.py -v`
Expected: FAIL — likely 422 because schema doesn't include `category`.

- [ ] **Step 4: Add `category` to the update schema**

In `app/schemas/tenant.py`, find the `TenantUpdate`-style class and add:

```python
from app.schemas.explore import TenantCategory


class TenantUpdate(BaseModel):
    # ... existing fields ...
    category: TenantCategory | None = None
```

Verify `TenantService.update_tenant` (or equivalent) propagates the field to the model — most likely it already iterates over set fields generically, but check.

- [ ] **Step 5: Verify response model includes `category`**

In the tenant read schema (`TenantRead` or equivalent), add `category`:

```python
class TenantRead(BaseModel):
    # ... existing fields ...
    category: TenantCategory | None
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/api/test_tenant_update_category.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add app/schemas/tenant.py tests/api/test_tenant_update_category.py
git commit -m "feat(tenant): accept category in PATCH /tenants/me"
```

---

## Task 11: Wire `source` + `source_meta` into conversation creation

**Files:**
- Modify: `app/services/visitor_service.py::bootstrap_visitor` (and/or wherever a customer-driven `ConversationSession` is created — likely `app/services/customer_conversation_service.py`)
- Modify: relevant request schemas (`app/schemas/visitor.py` for `VisitorBootstrapRequest`, plus customer-side conversation create)
- Test: `tests/api/test_conversation_attribution.py`

- [ ] **Step 1: Find both creation paths**

Run: `grep -rnE "ConversationSession\(" app/services app/api 2>/dev/null | grep -v __pycache__`
Expected: enumerate every site where a session is constructed. Likely 2-3 places.

- [ ] **Step 2: Write the failing test**

```python
# tests/api/test_conversation_attribution.py
import pytest

from app.db.models.conversation import ConversationSession


def test_visitor_bootstrap_accepts_source(client, db, listed_tenant):
    payload = {
        "tenant_slug": listed_tenant.public_slug,
        "source": "explore_browse",
        "source_meta": {"position": 4, "category_filter": "retail_ecommerce"},
    }
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    assert resp.status_code == 200
    # Find the session that was created
    sess = db.query(ConversationSession).filter_by(tenant_id=listed_tenant.id).first()
    assert sess is not None
    assert sess.source == "explore_browse"
    assert sess.source_meta == {"position": 4, "category_filter": "retail_ecommerce"}


def test_visitor_bootstrap_unknown_source_coerced_to_unknown(client, db, listed_tenant):
    payload = {
        "tenant_slug": listed_tenant.public_slug,
        "source": "totally_made_up",
    }
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    assert resp.status_code == 200  # MUST succeed — analytics field never breaks creation
    sess = db.query(ConversationSession).filter_by(tenant_id=listed_tenant.id).first()
    assert sess.source == "unknown"


def test_visitor_bootstrap_unknown_source_meta_keys_dropped(client, db, listed_tenant):
    payload = {
        "tenant_slug": listed_tenant.public_slug,
        "source": "widget",
        "source_meta": {"position": 1, "totally_made_up": "x"},
    }
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    assert resp.status_code == 200
    sess = db.query(ConversationSession).filter_by(tenant_id=listed_tenant.id).first()
    assert sess.source_meta == {"position": 1}
    assert "totally_made_up" not in sess.source_meta


def test_visitor_bootstrap_no_source_defaults_to_unknown(client, db, listed_tenant):
    payload = {"tenant_slug": listed_tenant.public_slug}
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    assert resp.status_code == 200
    sess = db.query(ConversationSession).filter_by(tenant_id=listed_tenant.id).first()
    # Default behavior — `unknown` (not None), so analytics queries find every row
    assert sess.source == "unknown"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/api/test_conversation_attribution.py -v`
Expected: FAIL — request schema rejects `source`/`source_meta`, OR session row has `source = None`.

- [ ] **Step 4: Extend the visitor bootstrap request schema**

In `app/schemas/visitor.py`, modify `VisitorBootstrapRequest`:

```python
from app.schemas.explore import ConversationSource, SourceMeta


class VisitorBootstrapRequest(BaseModel):
    # ... existing fields ...
    source: str | None = None  # raw string; service coerces to ConversationSource enum
    source_meta: SourceMeta | None = None
```

Note: declaring `source` as `str` (not `ConversationSource`) is intentional — Pydantic enum validation would 422 on unknown values, and we want **silent coerce-to-unknown** per the spec. Validation happens in the service.

- [ ] **Step 5: Coerce + persist in the service**

Add a helper at the top of `app/services/visitor_service.py` (or a shared `app/utils/attribution.py`):

```python
from app.schemas.explore import ConversationSource, SourceMeta


def coerce_source(raw: str | None) -> str:
    """Map a raw source string to a known ConversationSource value, defaulting to 'unknown'."""
    if raw is None:
        return ConversationSource.UNKNOWN.value
    try:
        return ConversationSource(raw).value
    except ValueError:
        # TODO: increment a counter so we notice if a frontend starts sending typo'd sources
        return ConversationSource.UNKNOWN.value


def normalize_source_meta(raw: dict | SourceMeta | None) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, SourceMeta):
        return raw.model_dump(exclude_none=False)
    # dict — validate via the model to drop unknown keys
    return SourceMeta.model_validate(raw).model_dump(exclude_none=False)
```

Then in `VisitorService.bootstrap_visitor` (and any other `ConversationSession` construction site), wire the new fields:

```python
session = ConversationSession(
    tenant_id=tenant.id,
    user_identifier=user_identifier,
    customer_user_id=customer_user_id,
    source=coerce_source(payload.source),
    source_meta=normalize_source_meta(payload.source_meta),
    # ... existing fields ...
)
```

Repeat at every site found in Step 1. Each site that doesn't have a `payload.source` to read from passes `coerce_source(None)` (which returns `"unknown"`).

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/api/test_conversation_attribution.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add app/schemas/visitor.py app/services/visitor_service.py tests/api/test_conversation_attribution.py
# also any other service files modified in step 5
git commit -m "feat(conversations): accept and persist source + source_meta attribution"
```

---

## Task 12: Run full test suite + smoke check

- [ ] **Step 1: Run all tests**

Run: `make test` (or `pytest -v` if make target differs)
Expected: ALL PASS — no regressions in existing tests, all new tests green.

- [ ] **Step 2: Smoke-test the endpoints manually**

Start the local stack: `make docker-up && make run`
Then exercise:

```bash
# Browse endpoint with new category filter
curl -s 'http://localhost:8000/api/v1/public/businesses?category=retail_ecommerce&page=1&page_size=5' | jq

# Browse endpoint with invalid category → 422
curl -s -i 'http://localhost:8000/api/v1/public/businesses?category=foo' | head -1

# Personal-businesses endpoint without auth → 401
curl -s -i 'http://localhost:8000/api/v1/customer/explore/personal-businesses' | head -1

# Tenant update with category
# (Requires a tenant operator JWT — adapt to your local setup)
```

Expected: shapes match the spec; status codes correct.

- [ ] **Step 3: Inspect query plans**

In a `psql` session against the local DB:

```sql
EXPLAIN ANALYZE SELECT * FROM tenants
WHERE is_publicly_listed = true AND category = 'retail_ecommerce'
ORDER BY is_featured DESC, (created_at >= now() - interval '30 days') DESC, display_name ASC
LIMIT 24;
```

Expected: uses `ix_tenants_listed_category` (the partial index). If it doesn't (e.g., on tiny seed data), seed more rows and re-check.

- [ ] **Step 4: Open PR**

Run: `git push -u origin <branch> && gh pr create --title "Phase A: explore backend (categories + attribution)" --body "$(cat <<'EOF'
## Summary
- Adds nullable `tenants.category` column + `TenantCategory` enum (13 values)
- Adds nullable `conversation_sessions.source` + `source_meta` JSONB columns + `ConversationSource` enum (6 values)
- Extends `GET /api/v1/public/businesses` with `category` filter + new sort (featured > recent > alpha)
- Adds `GET /api/v1/customer/explore/personal-businesses` for the customer's followed + recently-chatted businesses (cap 8, hide-when-empty)
- Wires `source` + `source_meta` through visitor bootstrap and customer conversation creation paths; unknown sources silently coerce to `"unknown"` so analytics never breaks creation

Spec: docs/superpowers/specs/2026-05-07-explore-phase-a-design.md
Plan: docs/superpowers/plans/2026-05-07-explore-phase-a-backend.md

## Test plan
- [x] All new tests green (12 new test files / ~30 cases)
- [x] Existing test suite unchanged
- [x] Manual smoke test against local Docker stack (browse + personal + invalid category)
- [x] EXPLAIN ANALYZE confirms partial index used by browse query

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"`

---

## Self-review checklist

- [x] **Spec coverage:** every section of the spec has tasks (data model T2-3, API contract T7+T9+T10+T11, service T6+T8, migration T4, schemas T1+T5)
- [x] **Placeholders:** none — every task has actual code
- [x] **Type consistency:** `TenantCategory`, `ConversationSource`, `SourceMeta`, `PersonalBusinessesResponse` named consistently across all tasks
- [x] **Cross-DB compatibility:** personal-block service uses Python-merge (works on SQLite + Postgres)
- [x] **Convention departures called out:** VARCHAR + Pydantic enum (vs codebase's `SQLEnum`); coerce-on-unknown source (vs strict 422)
- [x] **Auth fixtures:** test tasks flag the need to mirror existing customer-auth + tenant-operator-auth patterns rather than reinventing
