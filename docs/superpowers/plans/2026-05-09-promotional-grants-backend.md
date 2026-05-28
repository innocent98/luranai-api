# Promotional Grants — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `promotional_grants` backend system on the `luran` FastAPI repo, enabling time-bounded subscription-tier grants for re-engagement campaigns and extended onboarding, per `docs/superpowers/specs/2026-05-09-promotional-grants-design.md`.

**Architecture:** New `promotional_grants` and `auto_signup_grant_config` tables; a `PromotionalGrantService` that owns lifecycle mutations and shares the existing capability-recompute path; an ARQ worker that runs reminders and expiry reverts daily; hooks in the tenant signup path and the Paystack webhook for auto-grants and conversion detection.

**Tech Stack:** FastAPI, SQLAlchemy 2.x (sync session pattern matching existing services), Pydantic v2, Alembic, ARQ (Redis), Jinja2 (HTML email templates), pytest, pytest-freezegun.

**Out of scope for this plan:** dashboard UI (separate plan against `luran-dashboard`). Operator runbook docs (separate plan against `luran-docs`).

---

## File Structure

**New files:**
- `app/db/models/promotional_grant.py` — SQLAlchemy models for `PromotionalGrant` and `AutoSignupGrantConfig`.
- `app/schemas/promotional_grant.py` — Pydantic request/response schemas.
- `app/services/promotional_grant_service.py` — Core business logic: create, revoke, expire, mark-converted, cohort filter, preview token.
- `app/services/auto_signup_grant_config_service.py` — Singleton CRUD for the auto-signup config.
- `app/api/v1/endpoints/platform/promotional_grants.py` — All admin endpoints.
- `app/templates/emails/promo_granted.html` — Email sent when admin-bulk grant created.
- `app/templates/emails/promo_expiring.html` — Reminder email at T-14, T-7, T-1.
- `app/templates/emails/promo_expired.html` — Email sent at expiry revert.
- `alembic/versions/<rev>_add_promotional_grants.py` — Migration.
- `tests/services/test_promotional_grant_service.py`
- `tests/services/test_auto_signup_grant_config_service.py`
- `tests/api/v1/platform/test_promotional_grants.py`
- `tests/workers/test_promotional_grant_lifecycle.py`
- `tests/e2e/test_promotional_grants_e2e.py`

**Modified files:**
- `app/db/models/__init__.py` — Export new models.
- `app/api/v1/api.py` — Register the new router.
- `app/services/email_notification_service.py` — Add three send methods + notification types.
- `app/services/tenant_service.py` — Hook auto-signup grant into `create_tenant`.
- `app/services/webhook_handler_service.py` — Add conversion detection in payment success handler.
- `app/workers/document_processor.py` — Add `process_promotional_grant_lifecycle` worker function and cron registration.

---

## Phase 0 — Pre-flight

### Task 0: Verify clean baseline

**Files:** none (read-only)

- [ ] **Step 1: Run the full backend test suite and record green count**

Run: `pytest tests/ -q`
Expected: All tests pass (or known-failing tests listed; record the count). If failing tests exist outside the area we're changing, note them in the plan execution log so we can compare deltas.

- [ ] **Step 2: Verify ruff and black are clean**

Run: `ruff check app/ tests/ && black --check app/ tests/`
Expected: No output (clean) — these are the project's lint/format gates.

- [ ] **Step 3: Confirm latest migration head matches DB**

Run: `alembic current && alembic heads`
Expected: `current` matches `heads`. If they don't match, run `alembic upgrade head` before continuing.

---

## Phase 1 — Data layer

### Task 1: Alembic migration

**Files:**
- Create: `alembic/versions/<rev>_add_promotional_grants.py`

- [ ] **Step 1: Generate the migration scaffold**

Run: `alembic revision -m "add promotional grants and auto signup config"`
Expected: A new file is created under `alembic/versions/`. Note the revision ID it generates.

- [ ] **Step 2: Write the migration body**

Replace the body of the generated file with:

```python
"""add promotional grants and auto signup config

Revision ID: <auto>
Revises: <previous_head>
Create Date: 2026-05-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "<auto>"
down_revision = "<previous_head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotional_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_code", sa.String(64), nullable=False),
        sa.Column("campaign_name", sa.String(160), nullable=True),
        sa.Column("granted_tier", sa.String(50), nullable=False),
        sa.Column("duration_days", sa.Integer, nullable=False),
        sa.Column("grant_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_tier", sa.String(50), nullable=False),
        sa.Column("previous_status", sa.String(50), nullable=False),
        sa.Column("previous_billing_cycle", sa.String(20), nullable=True),
        sa.Column("previous_trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_subscription_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text, nullable=True),
        sa.Column("granted_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_admins.id"), nullable=True),
        sa.Column("granted_by_admin_name", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_index(
        "promo_grants_one_active_per_tenant",
        "promotional_grants",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "promo_grants_idempotency",
        "promotional_grants",
        ["tenant_id", "campaign_code"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'converted')"),
    )
    op.create_index(
        "promo_grants_expiry_scan",
        "promotional_grants",
        ["grant_ends_at", "status"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("promo_grants_campaign_lookup", "promotional_grants", ["campaign_code", "status"])
    op.create_index("promo_grants_tenant_lookup", "promotional_grants", ["tenant_id", sa.text("created_at DESC")])

    op.create_table(
        "auto_signup_grant_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("campaign_code", sa.String(64), nullable=True),
        sa.Column("campaign_name", sa.String(160), nullable=True),
        sa.Column("granted_tier", sa.String(50), nullable=True),
        sa.Column("duration_days", sa.Integer, nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_admins.id"), nullable=True),
        sa.Column("updated_by_admin_name", sa.String(255), nullable=True),
    )

    op.execute(
        "CREATE UNIQUE INDEX auto_signup_grant_config_singleton "
        "ON auto_signup_grant_config((true))"
    )

    # Seed singleton row
    op.execute(
        "INSERT INTO auto_signup_grant_config (enabled) VALUES (false)"
    )


def downgrade() -> None:
    op.drop_index("auto_signup_grant_config_singleton", table_name="auto_signup_grant_config")
    op.drop_table("auto_signup_grant_config")
    op.drop_index("promo_grants_tenant_lookup", table_name="promotional_grants")
    op.drop_index("promo_grants_campaign_lookup", table_name="promotional_grants")
    op.drop_index("promo_grants_expiry_scan", table_name="promotional_grants")
    op.drop_index("promo_grants_idempotency", table_name="promotional_grants")
    op.drop_index("promo_grants_one_active_per_tenant", table_name="promotional_grants")
    op.drop_table("promotional_grants")
```

Replace `<auto>` and `<previous_head>` with the actual revision IDs Alembic generated.

- [ ] **Step 3: Apply the migration to local DB and verify**

Run: `alembic upgrade head`
Expected: Migration succeeds. Then run `psql $DATABASE_URL -c "\d promotional_grants" -c "\d auto_signup_grant_config"` and confirm both tables exist with the indexes listed.

- [ ] **Step 4: Verify the singleton row was seeded**

Run: `psql $DATABASE_URL -c "SELECT enabled, campaign_code FROM auto_signup_grant_config"`
Expected: One row, `enabled = false`, `campaign_code = NULL`.

- [ ] **Step 5: Test downgrade and re-upgrade**

Run: `alembic downgrade -1 && alembic upgrade head`
Expected: Downgrade drops both tables, upgrade recreates them. No errors.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/<rev>_add_promotional_grants.py
git commit -m "feat(db): add promotional_grants and auto_signup_grant_config tables"
```

---

### Task 2: SQLAlchemy models

**Files:**
- Create: `app/db/models/promotional_grant.py`
- Modify: `app/db/models/__init__.py`

- [ ] **Step 1: Write the model file**

Create `app/db/models/promotional_grant.py`:

```python
"""Promotional grant database models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class PromotionalGrant(Base, UUIDMixin, TimestampMixin):
    """Time-bounded subscription tier elevation for marketing campaigns and onboarding."""

    __tablename__ = "promotional_grants"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    campaign_code: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    granted_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grant_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    previous_tier: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_billing_cycle: Mapped[str | None] = mapped_column(String(20), nullable=True)
    previous_trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_subscription_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    granted_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id"), nullable=True
    )
    granted_by_admin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PromotionalGrant(id={self.id}, tenant_id={self.tenant_id}, "
            f"campaign={self.campaign_code}, tier={self.granted_tier}, status={self.status})>"
        )


class AutoSignupGrantConfig(Base, UUIDMixin):
    """Singleton config controlling auto-grant on signup. Exactly one row exists."""

    __tablename__ = "auto_signup_grant_config"

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    campaign_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    granted_tier: Mapped[str | None] = mapped_column(String(50), nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_by_admin_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("platform_admins.id"), nullable=True
    )
    updated_by_admin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<AutoSignupGrantConfig(enabled={self.enabled}, campaign={self.campaign_code})>"
```

- [ ] **Step 2: Export models in package init**

In `app/db/models/__init__.py`, add (matching the file's existing export style):

```python
from app.db.models.promotional_grant import AutoSignupGrantConfig, PromotionalGrant
```

And include `"PromotionalGrant"` and `"AutoSignupGrantConfig"` in the `__all__` list (if the file defines one).

- [ ] **Step 3: Sanity-check imports**

Run: `python -c "from app.db.models import PromotionalGrant, AutoSignupGrantConfig; print(PromotionalGrant.__tablename__, AutoSignupGrantConfig.__tablename__)"`
Expected: `promotional_grants auto_signup_grant_config`

- [ ] **Step 4: Run full test suite to ensure nothing else broke**

Run: `pytest tests/ -q`
Expected: Same green count as Task 0 baseline (no regressions from model registration).

- [ ] **Step 5: Commit**

```bash
git add app/db/models/promotional_grant.py app/db/models/__init__.py
git commit -m "feat(db): add PromotionalGrant and AutoSignupGrantConfig models"
```

---

### Task 3: Pydantic schemas

**Files:**
- Create: `app/schemas/promotional_grant.py`

- [ ] **Step 1: Write the schema file**

Create `app/schemas/promotional_grant.py`:

```python
"""Schemas for promotional grant management."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

GrantedTier = Literal["lite", "growth", "professional"]
GrantStatus = Literal["active", "expired", "revoked", "converted"]
GrantSource = Literal["admin_bulk", "admin_single", "auto_signup"]


# ---------- Filters & preview ----------

class CohortFilter(BaseModel):
    subscription_statuses: list[str] = Field(
        default_factory=lambda: ["trial_expired", "canceled"],
        max_length=10,
    )
    churned_within_days: int | None = Field(default=90, ge=1, le=3650)


class PreviewRequest(BaseModel):
    filters: CohortFilter
    exclude_already_granted: bool = True


class PreviewTenantSummary(BaseModel):
    tenant_id: UUID
    name: str
    display_name: str
    subscription_status: str
    churned_at: datetime | None = None


class PreviewResponse(BaseModel):
    matched_count: int
    sample_tenants: list[PreviewTenantSummary]
    preview_token: str


# ---------- Bulk create ----------

CAMPAIGN_CODE_REGEX = r"^[A-Z][A-Z0-9_]{2,63}$"


class BulkGrantRequest(BaseModel):
    preview_token: str = Field(..., min_length=10)
    campaign_code: str = Field(..., pattern=CAMPAIGN_CODE_REGEX)
    campaign_name: str | None = Field(default=None, max_length=160)
    granted_tier: GrantedTier
    duration_days: int = Field(..., ge=14, le=180)
    force_replace_active: bool = False
    reason: str = Field(..., min_length=10, max_length=500)


class BulkGrantResponse(BaseModel):
    campaign_code: str
    granted: int
    skipped_already_granted: int
    failed: int
    grant_ids_sample: list[UUID]


# ---------- Single grant + responses ----------

class PromotionalGrantResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    campaign_code: str
    campaign_name: str | None
    granted_tier: GrantedTier
    duration_days: int
    grant_starts_at: datetime
    grant_ends_at: datetime
    status: GrantStatus
    source: GrantSource
    previous_tier: str
    previous_status: str
    expired_at: datetime | None
    revoked_at: datetime | None
    converted_at: datetime | None
    revoke_reason: str | None
    granted_by_admin_name: str | None
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RevokeGrantRequest(BaseModel):
    reason: str = Field(..., min_length=10, max_length=500)


class RevokeGrantResponse(BaseModel):
    grant_id: UUID
    status: GrantStatus
    tenant_id: UUID
    reverted_to_tier: str
    reverted_to_status: str


# ---------- Campaign summary ----------

class CampaignStatsBySource(BaseModel):
    admin_bulk: int = 0
    admin_single: int = 0
    auto_signup: int = 0


class CampaignStats(BaseModel):
    total: int
    active: int
    converted: int
    expired: int
    revoked: int
    by_source: CampaignStatsBySource


class CampaignSummary(BaseModel):
    campaign_code: str
    campaign_name: str | None
    granted_tier: GrantedTier
    duration_days: int
    first_grant_at: datetime
    stats: CampaignStats


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignSummary]


# ---------- Auto-signup config ----------

class AutoSignupConfigResponse(BaseModel):
    enabled: bool
    campaign_code: str | None
    campaign_name: str | None
    granted_tier: GrantedTier | None
    duration_days: int | None
    valid_until: datetime | None
    updated_at: datetime
    updated_by_admin_name: str | None


class AutoSignupConfigUpdateRequest(BaseModel):
    enabled: bool
    campaign_code: str | None = Field(default=None, pattern=CAMPAIGN_CODE_REGEX)
    campaign_name: str | None = Field(default=None, max_length=160)
    granted_tier: GrantedTier | None = None
    duration_days: int | None = Field(default=None, ge=14, le=180)
    valid_until: datetime | None = None

    @field_validator("granted_tier", "duration_days", "campaign_code", mode="before")
    @classmethod
    def required_when_enabled(cls, v, info):
        # Only enforce in model_validator below where we have full context.
        return v


class AutoSignupConfigUpdateResponse(AutoSignupConfigResponse):
    pass
```

- [ ] **Step 2: Verify schemas import cleanly**

Run: `python -c "from app.schemas.promotional_grant import BulkGrantRequest, AutoSignupConfigUpdateRequest; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/schemas/promotional_grant.py
git commit -m "feat(api): add promotional grant schemas"
```

---

## Phase 2 — Core service

### Task 4: AutoSignupGrantConfigService (singleton CRUD)

**Files:**
- Create: `app/services/auto_signup_grant_config_service.py`
- Test: `tests/services/test_auto_signup_grant_config_service.py`

- [ ] **Step 1: Write failing tests**

Create `tests/services/test_auto_signup_grant_config_service.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.services.auto_signup_grant_config_service import (
    AutoSignupGrantConfigService,
    AutoSignupConfigValidationError,
)


def test_get_returns_singleton_default(db_session):
    service = AutoSignupGrantConfigService(db_session)
    config = service.get()
    assert config.enabled is False
    assert config.campaign_code is None


def test_update_enables_with_full_config(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    valid_until = datetime.now(UTC) + timedelta(days=90)
    updated = service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=valid_until,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert updated.enabled is True
    assert updated.campaign_code == "LAUNCH_2026"
    assert updated.granted_tier == "professional"
    assert updated.duration_days == 90
    assert updated.updated_by_admin_id == super_admin_user.id


def test_update_rejects_enabled_without_required_fields(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    with pytest.raises(AutoSignupConfigValidationError):
        service.update(
            enabled=True,
            campaign_code=None,
            campaign_name=None,
            granted_tier=None,
            duration_days=None,
            valid_until=None,
            admin_id=super_admin_user.id,
            admin_name=super_admin_user.email,
        )


def test_update_disabling_clears_optional_fields_preserved(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    # enable first
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    # disable — campaign_code stays so we know what was last active
    disabled = service.update(
        enabled=False,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert disabled.enabled is False
    assert disabled.campaign_code == "LAUNCH_2026"


def test_is_active_handles_valid_until(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    past = datetime.now(UTC) - timedelta(days=1)
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=past,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert service.is_active() is False  # enabled but valid_until passed


def test_is_active_no_valid_until_means_indefinite(db_session, super_admin_user):
    service = AutoSignupGrantConfigService(db_session)
    service.update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Public Launch 2026",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    assert service.is_active() is True
```

Note: this assumes a `db_session` and `super_admin_user` pytest fixture exist. Check `tests/conftest.py`. If they don't, add them following the existing test patterns in `tests/services/test_subscription_validation_service.py`.

- [ ] **Step 2: Run the tests, expect failure**

Run: `pytest tests/services/test_auto_signup_grant_config_service.py -v`
Expected: ImportError (module not yet created).

- [ ] **Step 3: Implement the service**

Create `app/services/auto_signup_grant_config_service.py`:

```python
"""Singleton service managing the auto-signup grant configuration."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.promotional_grant import AutoSignupGrantConfig


class AutoSignupConfigValidationError(ValueError):
    """Raised when the config update is internally inconsistent."""


class AutoSignupGrantConfigService:
    """CRUD on the singleton auto_signup_grant_config row."""

    def __init__(self, db: Session):
        self.db = db

    def get(self) -> AutoSignupGrantConfig:
        """Return the singleton row. Migration seeds it; we should always find one."""
        row = self.db.query(AutoSignupGrantConfig).first()
        if row is None:
            raise RuntimeError(
                "auto_signup_grant_config singleton not seeded. "
                "Run alembic upgrade head."
            )
        return row

    def is_active(self) -> bool:
        """True iff config.enabled is true AND valid_until has not passed."""
        row = self.get()
        if not row.enabled:
            return False
        if row.valid_until is None:
            return True
        return datetime.now(UTC) < row.valid_until

    def update(
        self,
        *,
        enabled: bool,
        campaign_code: str | None,
        campaign_name: str | None,
        granted_tier: str | None,
        duration_days: int | None,
        valid_until: datetime | None,
        admin_id: UUID | None,
        admin_name: str | None,
    ) -> AutoSignupGrantConfig:
        if enabled:
            missing = [
                name for name, val in (
                    ("campaign_code", campaign_code),
                    ("granted_tier", granted_tier),
                    ("duration_days", duration_days),
                ) if val is None
            ]
            if missing:
                raise AutoSignupConfigValidationError(
                    f"Cannot enable auto-signup with missing fields: {', '.join(missing)}"
                )

        row = self.get()
        row.enabled = enabled
        row.campaign_code = campaign_code
        row.campaign_name = campaign_name
        row.granted_tier = granted_tier
        row.duration_days = duration_days
        row.valid_until = valid_until
        row.updated_at = datetime.now(UTC)
        row.updated_by_admin_id = admin_id
        row.updated_by_admin_name = admin_name
        self.db.commit()
        self.db.refresh(row)
        return row
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/services/test_auto_signup_grant_config_service.py -v`
Expected: All 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/auto_signup_grant_config_service.py tests/services/test_auto_signup_grant_config_service.py
git commit -m "feat(promo): AutoSignupGrantConfigService"
```

---

### Task 5: PromotionalGrantService skeleton + create_grant (single)

**Files:**
- Create: `app/services/promotional_grant_service.py`
- Test: `tests/services/test_promotional_grant_service.py`

- [ ] **Step 1: Write failing tests for create_grant**

Create `tests/services/test_promotional_grant_service.py`:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.promotional_grant_service import (
    PromotionalGrantService,
    PromoGrantValidationError,
    PromoGrantNotFoundError,
)


def _make_tenant(db_session, *, status="trial_expired", tier="trial"):
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Test Tenant",
        api_key_hash="dummy",
        subscription_tier=tier,
        subscription_status=status,
        trial_ends_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(tenant)
    db_session.commit()
    return tenant


def test_create_grant_admin_single_elevates_tenant(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)

    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name="Test Win-back",
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="manual single grant for QA",
    )

    db_session.refresh(tenant)
    assert tenant.subscription_tier == "professional"
    assert tenant.subscription_status == "active"
    assert grant.status == "active"
    assert grant.previous_tier == "trial"
    assert grant.previous_status == "trial_expired"
    assert grant.grant_ends_at - grant.grant_starts_at == timedelta(days=90)


def test_create_grant_writes_subscription_history(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="lite",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="manual single grant for QA",
    )
    history = (
        db_session.query(SubscriptionHistory)
        .filter(SubscriptionHistory.tenant_id == tenant.id)
        .order_by(SubscriptionHistory.created_at.desc())
        .first()
    )
    assert history is not None
    assert history.action == "promo_granted"
    assert history.new_tier == "lite"
    assert history.event_metadata.get("grant_id") == str(grant.id)
    assert history.event_metadata.get("campaign_code") == "WINBACK_TEST"


def test_create_grant_rejects_enterprise_tier(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    with pytest.raises(PromoGrantValidationError):
        service.create_grant(
            tenant_id=tenant.id,
            campaign_code="WINBACK_TEST",
            campaign_name=None,
            granted_tier="enterprise",
            duration_days=90,
            source="admin_single",
            granted_by_admin_id=super_admin_user.id,
            granted_by_admin_name=super_admin_user.email,
            reason="should fail",
        )


def test_create_grant_rejects_duration_out_of_range(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    with pytest.raises(PromoGrantValidationError):
        service.create_grant(
            tenant_id=tenant.id,
            campaign_code="WINBACK_TEST",
            campaign_name=None,
            granted_tier="professional",
            duration_days=10,  # below 14
            source="admin_single",
            granted_by_admin_id=super_admin_user.id,
            granted_by_admin_name=super_admin_user.email,
            reason="should fail",
        )


def test_create_grant_idempotency_blocks_duplicate_per_campaign(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="first grant",
    )
    with pytest.raises(IntegrityError):
        service.create_grant(
            tenant_id=tenant.id,
            campaign_code="WINBACK_TEST",
            campaign_name=None,
            granted_tier="professional",
            duration_days=90,
            source="admin_single",
            granted_by_admin_id=super_admin_user.id,
            granted_by_admin_name=super_admin_user.email,
            reason="duplicate",
        )


def test_create_grant_one_active_per_tenant_invariant(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="first grant",
    )
    # Different campaign, same tenant — partial unique on (tenant_id) WHERE active blocks it
    with pytest.raises(IntegrityError):
        service.create_grant(
            tenant_id=tenant.id,
            campaign_code="DIFFERENT",
            campaign_name=None,
            granted_tier="professional",
            duration_days=90,
            source="admin_single",
            granted_by_admin_id=super_admin_user.id,
            granted_by_admin_name=super_admin_user.email,
            reason="should fail — already has active",
        )


def test_create_grant_auto_signup_uses_synthetic_snapshot(db_session):
    # Tenant created moments ago, status='trialing'
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Fresh Signup",
        api_key_hash="dummy",
        subscription_tier="trial",
        subscription_status="trialing",
        trial_ends_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(tenant)
    db_session.commit()

    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="LAUNCH_2026",
        campaign_name="Launch",
        granted_tier="professional",
        duration_days=90,
        source="auto_signup",
        granted_by_admin_id=None,
        granted_by_admin_name="system",
        reason="auto-grant on signup",
    )
    assert grant.previous_tier == "trial"
    assert grant.previous_status == "trial_expired"
    # Synthetic: previous_trial_ends_at is set near "now" so revert hits the wall immediately
    assert grant.previous_trial_ends_at is not None
    assert abs((datetime.now(UTC) - grant.previous_trial_ends_at).total_seconds()) < 5
```

- [ ] **Step 2: Run tests, expect import failure**

Run: `pytest tests/services/test_promotional_grant_service.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement service skeleton + create_grant**

Create `app/services/promotional_grant_service.py`:

```python
"""Promotional grant service: lifecycle for time-bounded subscription elevations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant
from app.services.platform_subscription_service import PlatformSubscriptionService

ALLOWED_TIERS = {"lite", "growth", "professional"}
ALLOWED_SOURCES = {"admin_bulk", "admin_single", "auto_signup"}
DURATION_MIN_DAYS = 14
DURATION_MAX_DAYS = 180


class PromoGrantValidationError(ValueError):
    """Validation failure on input parameters (not DB-level constraints)."""


class PromoGrantNotFoundError(LookupError):
    """Grant lookup failed."""


class PromoGrantStateError(RuntimeError):
    """Grant is in a state incompatible with the requested operation."""


class PromotionalGrantService:
    def __init__(self, db: Session):
        self.db = db
        # Reuse the existing capability-recompute path; this is the single source
        # of truth for "tier → capabilities" already used by override.
        self._platform_subs = PlatformSubscriptionService(db)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_grant(
        self,
        *,
        tenant_id: UUID,
        campaign_code: str,
        campaign_name: str | None,
        granted_tier: str,
        duration_days: int,
        source: str,
        granted_by_admin_id: UUID | None,
        granted_by_admin_name: str | None,
        reason: str,
    ) -> PromotionalGrant:
        if granted_tier not in ALLOWED_TIERS:
            raise PromoGrantValidationError(
                f"granted_tier must be one of {sorted(ALLOWED_TIERS)}; got {granted_tier!r}"
            )
        if source not in ALLOWED_SOURCES:
            raise PromoGrantValidationError(
                f"source must be one of {sorted(ALLOWED_SOURCES)}; got {source!r}"
            )
        if duration_days < DURATION_MIN_DAYS or duration_days > DURATION_MAX_DAYS:
            raise PromoGrantValidationError(
                f"duration_days must be in [{DURATION_MIN_DAYS}, {DURATION_MAX_DAYS}]; "
                f"got {duration_days}"
            )
        if not reason or len(reason) < 10:
            raise PromoGrantValidationError("reason must be at least 10 characters")

        tenant = self.db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant is None:
            raise PromoGrantNotFoundError(f"Tenant {tenant_id} not found")

        now = datetime.now(UTC)
        starts_at = now
        ends_at = now + timedelta(days=duration_days)

        # Snapshot the pre-grant state.
        if source == "auto_signup":
            # Synthetic snapshot: revert puts them straight at trial_expired.
            previous_tier = "trial"
            previous_status = "trial_expired"
            previous_billing_cycle = None
            previous_trial_ends_at = now
            previous_subscription_ends_at = None
        else:
            previous_tier = tenant.subscription_tier
            previous_status = tenant.subscription_status
            previous_billing_cycle = tenant.billing_cycle
            previous_trial_ends_at = tenant.trial_ends_at
            previous_subscription_ends_at = tenant.subscription_ends_at

        grant = PromotionalGrant(
            tenant_id=tenant_id,
            campaign_code=campaign_code,
            campaign_name=campaign_name,
            granted_tier=granted_tier,
            duration_days=duration_days,
            grant_starts_at=starts_at,
            grant_ends_at=ends_at,
            previous_tier=previous_tier,
            previous_status=previous_status,
            previous_billing_cycle=previous_billing_cycle,
            previous_trial_ends_at=previous_trial_ends_at,
            previous_subscription_ends_at=previous_subscription_ends_at,
            status="active",
            source=source,
            granted_by_admin_id=granted_by_admin_id,
            granted_by_admin_name=granted_by_admin_name,
            reason=reason,
        )
        self.db.add(grant)
        self.db.flush()  # surfaces partial-unique-index violations as IntegrityError before mutating tenant

        # Apply elevation
        tenant.subscription_tier = granted_tier
        tenant.subscription_status = "active"
        tenant.billing_cycle = None  # promotional grants are not billed
        tenant.subscription_starts_at = starts_at
        tenant.subscription_ends_at = ends_at
        tenant.trial_ends_at = None  # don't double-count trial during a grant

        # Recompute capabilities (reset overrides; grants apply tier defaults)
        self._platform_subs._update_capabilities_with_overrides(
            tenant_id, granted_tier, respect_overrides=False
        )

        self.db.add(SubscriptionHistory(
            tenant_id=tenant_id,
            action="promo_granted",
            previous_tier=previous_tier,
            new_tier=granted_tier,
            previous_status=previous_status,
            new_status="active",
            changed_by_type="platform_admin" if source != "auto_signup" else "system",
            changed_by_id=granted_by_admin_id,
            changed_by_name=granted_by_admin_name,
            reason=reason,
            event_metadata={
                "grant_id": str(grant.id),
                "campaign_code": campaign_code,
                "source": source,
                "duration_days": duration_days,
            },
        ))

        self.db.commit()
        self.db.refresh(grant)
        return grant
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "create"`
Expected: All 7 create-related tests pass. The IntegrityError tests rely on Postgres partial unique indexes — if your test DB is SQLite, those tests will fail; ensure tests use Postgres (the project's test conftest should already configure this).

- [ ] **Step 5: Commit**

```bash
git add app/services/promotional_grant_service.py tests/services/test_promotional_grant_service.py
git commit -m "feat(promo): PromotionalGrantService.create_grant"
```

---

### Task 6: PromotionalGrantService — revoke_grant

**Files:**
- Modify: `app/services/promotional_grant_service.py`
- Modify: `tests/services/test_promotional_grant_service.py`

- [ ] **Step 1: Append failing tests for revoke**

Append to `tests/services/test_promotional_grant_service.py`:

```python
def test_revoke_grant_reverts_state_and_writes_history(db_session, super_admin_user):
    tenant = _make_tenant(db_session, status="trial_expired", tier="trial")
    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="setup",
    )

    result = service.revoke_grant(
        grant_id=grant.id,
        reason="customer requested cancellation",
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )

    db_session.refresh(tenant)
    db_session.refresh(grant)
    assert grant.status == "revoked"
    assert grant.revoked_at is not None
    assert grant.revoke_reason == "customer requested cancellation"
    assert tenant.subscription_tier == "trial"
    assert tenant.subscription_status == "trial_expired"
    assert result["reverted_to_tier"] == "trial"
    assert result["reverted_to_status"] == "trial_expired"

    history = (
        db_session.query(SubscriptionHistory)
        .filter(
            SubscriptionHistory.tenant_id == tenant.id,
            SubscriptionHistory.action == "promo_revoked",
        )
        .first()
    )
    assert history is not None


def test_revoke_grant_already_inactive_raises(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="setup",
    )
    service.revoke_grant(
        grant_id=grant.id,
        reason="first revoke",
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    with pytest.raises(PromoGrantStateError):
        service.revoke_grant(
            grant_id=grant.id,
            reason="second revoke should fail",
            admin_id=super_admin_user.id,
            admin_name=super_admin_user.email,
        )


def test_revoke_grant_not_found(db_session, super_admin_user):
    service = PromotionalGrantService(db_session)
    with pytest.raises(PromoGrantNotFoundError):
        service.revoke_grant(
            grant_id=uuid4(),
            reason="not found",
            admin_id=super_admin_user.id,
            admin_name=super_admin_user.email,
        )
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/services/test_promotional_grant_service.py::test_revoke_grant_reverts_state_and_writes_history -v`
Expected: FAIL — `revoke_grant` not defined.

- [ ] **Step 3: Implement revoke_grant + the shared revert helper**

Append to `app/services/promotional_grant_service.py` (inside the class):

```python
    # ------------------------------------------------------------------
    # Revert (shared between expire and revoke)
    # ------------------------------------------------------------------

    def _revert_tenant_to_snapshot(self, grant: PromotionalGrant) -> Tenant:
        """Apply the snapshot fields back to the tenant. Caller commits."""
        tenant = self.db.query(Tenant).filter(Tenant.id == grant.tenant_id).first()
        if tenant is None:
            raise PromoGrantStateError(f"Tenant {grant.tenant_id} for grant {grant.id} not found")
        tenant.subscription_tier = grant.previous_tier
        tenant.subscription_status = grant.previous_status
        tenant.billing_cycle = grant.previous_billing_cycle
        tenant.trial_ends_at = grant.previous_trial_ends_at
        tenant.subscription_ends_at = grant.previous_subscription_ends_at
        # Recompute capabilities based on the restored tier
        self._platform_subs._update_capabilities_with_overrides(
            tenant.id, grant.previous_tier, respect_overrides=False
        )
        return tenant

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke_grant(
        self,
        *,
        grant_id: UUID,
        reason: str,
        admin_id: UUID | None,
        admin_name: str | None,
    ) -> dict:
        if not reason or len(reason) < 10:
            raise PromoGrantValidationError("reason must be at least 10 characters")

        grant = self.db.query(PromotionalGrant).filter(PromotionalGrant.id == grant_id).first()
        if grant is None:
            raise PromoGrantNotFoundError(f"Grant {grant_id} not found")
        if grant.status != "active":
            raise PromoGrantStateError(
                f"Grant {grant_id} is {grant.status!r}; only active grants can be revoked"
            )

        tenant = self._revert_tenant_to_snapshot(grant)

        grant.status = "revoked"
        grant.revoked_at = datetime.now(UTC)
        grant.revoke_reason = reason

        self.db.add(SubscriptionHistory(
            tenant_id=grant.tenant_id,
            action="promo_revoked",
            previous_tier=grant.granted_tier,
            new_tier=grant.previous_tier,
            previous_status="active",
            new_status=grant.previous_status,
            changed_by_type="platform_admin",
            changed_by_id=admin_id,
            changed_by_name=admin_name,
            reason=reason,
            event_metadata={
                "grant_id": str(grant.id),
                "campaign_code": grant.campaign_code,
            },
        ))

        self.db.commit()

        return {
            "grant_id": grant.id,
            "status": grant.status,
            "tenant_id": tenant.id,
            "reverted_to_tier": grant.previous_tier,
            "reverted_to_status": grant.previous_status,
        }
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "revoke"`
Expected: All 3 revoke tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/promotional_grant_service.py tests/services/test_promotional_grant_service.py
git commit -m "feat(promo): PromotionalGrantService.revoke_grant"
```

---

### Task 7: PromotionalGrantService — expire_grants and mark_converted

**Files:**
- Modify: `app/services/promotional_grant_service.py`
- Modify: `tests/services/test_promotional_grant_service.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/services/test_promotional_grant_service.py`:

```python
def test_expire_grants_reverts_only_past_active_grants(db_session, super_admin_user):
    # Active future-ending grant — should NOT expire
    t1 = _make_tenant(db_session, status="trial_expired", tier="trial")
    # Active past-ending grant — SHOULD expire
    t2 = _make_tenant(db_session, status="trial_expired", tier="trial")

    service = PromotionalGrantService(db_session)
    g1 = service.create_grant(
        tenant_id=t1.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="future grant",
    )
    g2 = service.create_grant(
        tenant_id=t2.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="will be expired",
    )
    # Force g2 to be past-ending
    g2.grant_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    expired_ids = service.expire_grants_due()

    db_session.refresh(g1)
    db_session.refresh(g2)
    db_session.refresh(t2)
    assert expired_ids == [g2.id]
    assert g1.status == "active"
    assert g2.status == "expired"
    assert g2.expired_at is not None
    assert t2.subscription_tier == "trial"
    assert t2.subscription_status == "trial_expired"


def test_expire_grants_idempotent(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="setup",
    )
    grant.grant_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    first = service.expire_grants_due()
    second = service.expire_grants_due()
    assert len(first) == 1
    assert second == []


def test_mark_converted_blocks_future_revert(db_session, super_admin_user):
    tenant = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="setup",
    )

    # Simulate Paystack webhook: tenant pays
    tenant.subscription_tier = "professional"
    tenant.subscription_status = "active"
    tenant.billing_cycle = "monthly"
    db_session.commit()

    converted = service.mark_converted_for_tenant(
        tenant_id=tenant.id, paid_tier="professional", paid_billing_cycle="monthly"
    )
    assert converted is not None
    assert converted.status == "converted"
    assert converted.converted_at is not None

    # Force end_at to past, expire pass should NOT revert
    converted.grant_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    expired_ids = service.expire_grants_due()
    assert expired_ids == []
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "expire or convert"`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Implement expire_grants_due and mark_converted_for_tenant**

Append to the class in `app/services/promotional_grant_service.py`:

```python
    # ------------------------------------------------------------------
    # Expire (worker pass)
    # ------------------------------------------------------------------

    def find_grants_due_for_expiry(self) -> list[PromotionalGrant]:
        """Return all active grants whose end-time has passed."""
        now = datetime.now(UTC)
        return (
            self.db.query(PromotionalGrant)
            .filter(
                PromotionalGrant.status == "active",
                PromotionalGrant.grant_ends_at < now,
            )
            .all()
        )

    def expire_grants_due(self) -> list[UUID]:
        """Process all grants that have hit their end_at. Idempotent.

        Returns the list of grant IDs expired in this pass (so the caller
        — typically the worker — can dispatch a per-grant 'expired' email).
        """
        due = self.find_grants_due_for_expiry()
        expired_ids: list[UUID] = []
        for grant in due:
            try:
                self._revert_tenant_to_snapshot(grant)
                grant.status = "expired"
                grant.expired_at = datetime.now(UTC)
                self.db.add(SubscriptionHistory(
                    tenant_id=grant.tenant_id,
                    action="promo_expired",
                    previous_tier=grant.granted_tier,
                    new_tier=grant.previous_tier,
                    previous_status="active",
                    new_status=grant.previous_status,
                    changed_by_type="system",
                    changed_by_name="promo_lifecycle_worker",
                    reason=f"Grant ended after {grant.duration_days} days",
                    event_metadata={
                        "grant_id": str(grant.id),
                        "campaign_code": grant.campaign_code,
                    },
                ))
                self.db.commit()
                expired_ids.append(grant.id)
            except Exception:
                self.db.rollback()
                # logged at the worker layer; continue to next grant
                raise
        return expired_ids

    # ------------------------------------------------------------------
    # Convert (Paystack webhook integration point)
    # ------------------------------------------------------------------

    def mark_converted_for_tenant(
        self,
        *,
        tenant_id: UUID,
        paid_tier: str,
        paid_billing_cycle: str | None,
    ) -> PromotionalGrant | None:
        """Mark the active grant for tenant_id as converted. No-op if no active grant.

        Caller (the Paystack webhook) is expected to have already mutated the
        tenant's subscription to the paid state.
        """
        active = (
            self.db.query(PromotionalGrant)
            .filter(
                PromotionalGrant.tenant_id == tenant_id,
                PromotionalGrant.status == "active",
            )
            .first()
        )
        if active is None:
            return None
        active.status = "converted"
        active.converted_at = datetime.now(UTC)
        self.db.add(SubscriptionHistory(
            tenant_id=tenant_id,
            action="promo_converted",
            previous_tier=active.granted_tier,
            new_tier=paid_tier,
            previous_status="active",
            new_status="active",
            changed_by_type="webhook",
            changed_by_name="paystack",
            reason=f"Tenant converted to paid {paid_tier} during {active.campaign_code}",
            event_metadata={
                "grant_id": str(active.id),
                "campaign_code": active.campaign_code,
                "paid_billing_cycle": paid_billing_cycle,
            },
        ))
        self.db.commit()
        return active
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "expire or convert"`
Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/promotional_grant_service.py tests/services/test_promotional_grant_service.py
git commit -m "feat(promo): expire_grants_due and mark_converted_for_tenant"
```

---

### Task 8: PromotionalGrantService — cohort filter, preview token, bulk_create

**Files:**
- Modify: `app/services/promotional_grant_service.py`
- Modify: `tests/services/test_promotional_grant_service.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/services/test_promotional_grant_service.py`:

```python
def test_build_cohort_query_filters_status_and_age(db_session):
    # 1 trial_expired recently, 1 trial_expired old, 1 canceled recently, 1 trialing
    now = datetime.now(UTC)
    recent_expired = _make_tenant(db_session, status="trial_expired", tier="trial")
    recent_expired.trial_ends_at = now - timedelta(days=10)
    old_expired = _make_tenant(db_session, status="trial_expired", tier="trial")
    old_expired.trial_ends_at = now - timedelta(days=400)
    canceled = _make_tenant(db_session, status="canceled", tier="lite")
    canceled.subscription_ends_at = now - timedelta(days=20)
    active = _make_tenant(db_session, status="trialing", tier="trial")
    db_session.commit()

    service = PromotionalGrantService(db_session)
    matched = service.build_cohort(
        subscription_statuses=["trial_expired", "canceled"],
        churned_within_days=90,
        exclude_already_granted=True,
    )
    matched_ids = {t.id for t in matched}
    assert recent_expired.id in matched_ids
    assert canceled.id in matched_ids
    assert old_expired.id not in matched_ids
    assert active.id not in matched_ids


def test_preview_token_round_trip(db_session):
    service = PromotionalGrantService(db_session)
    tenant_ids = [uuid4() for _ in range(5)]
    token = service.encode_preview_token(tenant_ids)
    decoded = service.decode_preview_token(token)
    assert set(decoded) == set(tenant_ids)


def test_preview_token_rejects_tampered_payload(db_session):
    service = PromotionalGrantService(db_session)
    tenant_ids = [uuid4() for _ in range(2)]
    token = service.encode_preview_token(tenant_ids)
    tampered = token[:-2] + "xx"
    with pytest.raises(PromoGrantValidationError):
        service.decode_preview_token(tampered)


def test_preview_token_expires(db_session, monkeypatch):
    service = PromotionalGrantService(db_session)
    tenant_ids = [uuid4()]
    token = service.encode_preview_token(tenant_ids, ttl_seconds=1)
    import time
    time.sleep(2)
    with pytest.raises(PromoGrantValidationError):
        service.decode_preview_token(token)


def test_bulk_create_grants_idempotent_skip_already_granted(db_session, super_admin_user):
    t1 = _make_tenant(db_session)
    t2 = _make_tenant(db_session)
    service = PromotionalGrantService(db_session)

    # First bulk
    res1 = service.bulk_create_grants(
        tenant_ids=[t1.id, t2.id],
        campaign_code="WINBACK_TEST",
        campaign_name="Test",
        granted_tier="professional",
        duration_days=90,
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="bulk run 1",
        force_replace_active=False,
    )
    assert res1["granted"] == 2
    assert res1["skipped_already_granted"] == 0

    # Second bulk with same campaign — both skipped
    res2 = service.bulk_create_grants(
        tenant_ids=[t1.id, t2.id],
        campaign_code="WINBACK_TEST",
        campaign_name="Test",
        granted_tier="professional",
        duration_days=90,
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="bulk run 2",
        force_replace_active=False,
    )
    assert res2["granted"] == 0
    assert res2["skipped_already_granted"] == 2
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "cohort or preview or bulk"`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Implement cohort + preview token + bulk_create**

Append to the class in `app/services/promotional_grant_service.py`:

```python
    # ------------------------------------------------------------------
    # Cohort selection
    # ------------------------------------------------------------------

    def build_cohort(
        self,
        *,
        subscription_statuses: list[str],
        churned_within_days: int | None,
        exclude_already_granted: bool,
    ) -> list[Tenant]:
        from sqlalchemy import or_

        if not subscription_statuses:
            return []

        query = self.db.query(Tenant).filter(Tenant.subscription_status.in_(subscription_statuses))

        if churned_within_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=churned_within_days)
            # 'churned at' is approximated by trial_ends_at for trial_expired,
            # subscription_ends_at for canceled / past_due
            query = query.filter(
                or_(
                    Tenant.trial_ends_at >= cutoff,
                    Tenant.subscription_ends_at >= cutoff,
                )
            )

        if exclude_already_granted:
            granted_subq = (
                self.db.query(PromotionalGrant.tenant_id)
                .filter(PromotionalGrant.status == "active")
                .subquery()
            )
            query = query.filter(~Tenant.id.in_(self.db.query(granted_subq.c.tenant_id)))

        return query.all()

    # ------------------------------------------------------------------
    # Preview token (signed, TTL'd cohort handle)
    # ------------------------------------------------------------------

    def encode_preview_token(
        self, tenant_ids: list[UUID], *, ttl_seconds: int = 1800
    ) -> str:
        """Sign a list of tenant IDs with SECRET_KEY + a TTL.

        Returns a URL-safe token.
        """
        from itsdangerous import TimestampSigner

        from app.core.config import settings

        signer = TimestampSigner(settings.SECRET_KEY, salt="promo-preview")
        payload = ",".join(str(tid) for tid in tenant_ids)
        # encode TTL in the token via TimestampSigner; verification side enforces it.
        return signer.sign(payload).decode("utf-8")

    def decode_preview_token(self, token: str, *, ttl_seconds: int = 1800) -> list[UUID]:
        from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

        from app.core.config import settings

        signer = TimestampSigner(settings.SECRET_KEY, salt="promo-preview")
        try:
            raw = signer.unsign(token, max_age=ttl_seconds).decode("utf-8")
        except SignatureExpired as e:
            raise PromoGrantValidationError("preview_token expired") from e
        except BadSignature as e:
            raise PromoGrantValidationError("preview_token invalid") from e
        return [UUID(s) for s in raw.split(",") if s]

    # ------------------------------------------------------------------
    # Bulk create
    # ------------------------------------------------------------------

    def bulk_create_grants(
        self,
        *,
        tenant_ids: list[UUID],
        campaign_code: str,
        campaign_name: str | None,
        granted_tier: str,
        duration_days: int,
        granted_by_admin_id: UUID,
        granted_by_admin_name: str,
        reason: str,
        force_replace_active: bool,
    ) -> dict:
        from sqlalchemy.exc import IntegrityError

        granted = 0
        skipped = 0
        failed = 0
        sample_ids: list[UUID] = []

        for tenant_id in tenant_ids:
            try:
                if force_replace_active:
                    # Revoke any existing active grant first (different campaign)
                    existing = (
                        self.db.query(PromotionalGrant)
                        .filter(
                            PromotionalGrant.tenant_id == tenant_id,
                            PromotionalGrant.status == "active",
                        )
                        .first()
                    )
                    if existing is not None:
                        self.revoke_grant(
                            grant_id=existing.id,
                            reason=f"Replaced by bulk grant from campaign {campaign_code}",
                            admin_id=granted_by_admin_id,
                            admin_name=granted_by_admin_name,
                        )
                grant = self.create_grant(
                    tenant_id=tenant_id,
                    campaign_code=campaign_code,
                    campaign_name=campaign_name,
                    granted_tier=granted_tier,
                    duration_days=duration_days,
                    source="admin_bulk",
                    granted_by_admin_id=granted_by_admin_id,
                    granted_by_admin_name=granted_by_admin_name,
                    reason=reason,
                )
                granted += 1
                if len(sample_ids) < 10:
                    sample_ids.append(grant.id)
            except IntegrityError:
                self.db.rollback()
                skipped += 1
            except (PromoGrantValidationError, PromoGrantNotFoundError):
                self.db.rollback()
                failed += 1

        return {
            "granted": granted,
            "skipped_already_granted": skipped,
            "failed": failed,
            "grant_ids_sample": sample_ids,
        }
```

Note on `itsdangerous`: this is a transitive dep of Flask but commonly available. If not in `requirements.txt`, add it: `pip install itsdangerous && echo "itsdangerous" >> requirements.txt`. The project may already use it for other signed tokens — `grep itsdangerous requirements.txt` to check before adding.

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/services/test_promotional_grant_service.py -v -k "cohort or preview or bulk"`
Expected: All 5 tests pass.

- [ ] **Step 5: Run the full service test suite to ensure no regressions**

Run: `pytest tests/services/test_promotional_grant_service.py tests/services/test_auto_signup_grant_config_service.py -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/promotional_grant_service.py tests/services/test_promotional_grant_service.py
git commit -m "feat(promo): cohort filter, signed preview token, bulk_create_grants"
```

---

## Phase 3 — API endpoints

### Task 9: Endpoint module skeleton + POST /preview

**Files:**
- Create: `app/api/v1/endpoints/platform/promotional_grants.py`
- Modify: `app/api/v1/api.py`
- Test: `tests/api/v1/platform/test_promotional_grants.py`

- [ ] **Step 1: Write failing tests for /preview**

Create `tests/api/v1/platform/test_promotional_grants.py`:

```python
import pytest
from fastapi import status


def test_preview_returns_matched_count_and_token(client, super_admin_token, churned_tenants_factory):
    churned_tenants_factory(count=3, status="trial_expired", churned_days_ago=30)
    response = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={
            "filters": {
                "subscription_statuses": ["trial_expired"],
                "churned_within_days": 90,
            },
            "exclude_already_granted": True,
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["matched_count"] == 3
    assert "preview_token" in body
    assert len(body["sample_tenants"]) == 3


def test_preview_requires_super_admin(client, regular_user_token):
    response = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {regular_user_token}"},
    )
    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
```

If `churned_tenants_factory` doesn't exist, add it to `tests/conftest.py` with a basic implementation that creates N tenants with the given status and `trial_ends_at = now - days_ago`.

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v`
Expected: FAIL (404 or import error).

- [ ] **Step 3: Implement endpoint module + register**

Create `app/api/v1/endpoints/platform/promotional_grants.py`:

```python
"""Platform admin endpoints for promotional grants."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.schemas.platform_admin import PlatformAdminResponse
from app.schemas.promotional_grant import (
    BulkGrantRequest,
    BulkGrantResponse,
    PreviewRequest,
    PreviewResponse,
    PreviewTenantSummary,
)
from app.services.promotional_grant_service import (
    PromotionalGrantService,
    PromoGrantValidationError,
    PromoGrantStateError,
    PromoGrantNotFoundError,
)
from app.utils.http_rate_limiter import platform_admin_rate_limit

router = APIRouter()

PREVIEW_SAMPLE_LIMIT = 10
BULK_MAX_PER_REQUEST = 5000


@router.post(
    "/preview",
    response_model=PreviewResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def preview_cohort(
    request: PreviewRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    matched = service.build_cohort(
        subscription_statuses=request.filters.subscription_statuses,
        churned_within_days=request.filters.churned_within_days,
        exclude_already_granted=request.exclude_already_granted,
    )
    sample = [
        PreviewTenantSummary(
            tenant_id=t.id,
            name=t.name,
            display_name=t.display_name,
            subscription_status=t.subscription_status,
            churned_at=t.trial_ends_at or t.subscription_ends_at,
        )
        for t in matched[:PREVIEW_SAMPLE_LIMIT]
    ]
    token = service.encode_preview_token([t.id for t in matched])
    return PreviewResponse(
        matched_count=len(matched),
        sample_tenants=sample,
        preview_token=token,
    )
```

In `app/api/v1/api.py`, add the import and registration alongside the existing platform routers:

```python
from app.api.v1.endpoints.platform import promotional_grants as platform_promotional_grants

api_router.include_router(
    platform_promotional_grants.router,
    prefix="/platform/promotional-grants",
    tags=["platform: promotional grants"],
)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v`
Expected: Both preview tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/platform/promotional_grants.py app/api/v1/api.py tests/api/v1/platform/test_promotional_grants.py
git commit -m "feat(api): POST /platform/promotional-grants/preview"
```

---

### Task 10: POST / (bulk create)

**Files:**
- Modify: `app/api/v1/endpoints/platform/promotional_grants.py`
- Modify: `tests/api/v1/platform/test_promotional_grants.py`

- [ ] **Step 1: Append failing tests**

```python
def test_bulk_create_happy_path(client, super_admin_token, db_session, churned_tenants_factory):
    tenants = churned_tenants_factory(count=2, status="trial_expired", churned_days_ago=30)
    preview = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()

    response = client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": "API Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "API integration test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["granted"] == 2
    assert body["skipped_already_granted"] == 0
    assert body["campaign_code"] == "WINBACK_API_TEST"


def test_bulk_create_rejects_invalid_preview_token(client, super_admin_token):
    response = client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": "obviously-fake-token",
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": "API Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "should fail",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 400


def test_bulk_create_rejects_enterprise_tier(client, super_admin_token, churned_tenants_factory):
    churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    preview = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    response = client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": None,
            "granted_tier": "enterprise",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "should fail validation",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 422  # Pydantic Literal violation
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v -k bulk`
Expected: FAIL.

- [ ] **Step 3: Implement bulk endpoint**

Append to `app/api/v1/endpoints/platform/promotional_grants.py`:

```python
@router.post(
    "/",
    response_model=BulkGrantResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def bulk_create_grants(
    request: BulkGrantRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    try:
        tenant_ids = service.decode_preview_token(request.preview_token)
    except PromoGrantValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    if len(tenant_ids) > BULK_MAX_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bulk operations are capped at {BULK_MAX_PER_REQUEST} tenants per request",
        )

    result = service.bulk_create_grants(
        tenant_ids=tenant_ids,
        campaign_code=request.campaign_code,
        campaign_name=request.campaign_name,
        granted_tier=request.granted_tier,
        duration_days=request.duration_days,
        granted_by_admin_id=current_admin.id,
        granted_by_admin_name=current_admin.email,
        reason=request.reason,
        force_replace_active=request.force_replace_active,
    )
    return BulkGrantResponse(
        campaign_code=request.campaign_code,
        granted=result["granted"],
        skipped_already_granted=result["skipped_already_granted"],
        failed=result["failed"],
        grant_ids_sample=result["grant_ids_sample"],
    )
```

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v -k bulk`
Expected: All 3 bulk tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/platform/promotional_grants.py tests/api/v1/platform/test_promotional_grants.py
git commit -m "feat(api): POST /platform/promotional-grants/ (bulk create)"
```

---

### Task 11: POST /{grant_id}/revoke + GET /tenant/{tenant_id}

**Files:**
- Modify: `app/api/v1/endpoints/platform/promotional_grants.py`
- Modify: `tests/api/v1/platform/test_promotional_grants.py`

- [ ] **Step 1: Append failing tests**

```python
def test_revoke_endpoint_reverts_tenant(client, super_admin_token, db_session, churned_tenants_factory):
    tenants = churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    preview = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    bulk = client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_REVOKE",
            "campaign_name": "Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "setup for revoke test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    grant_id = bulk["grant_ids_sample"][0]

    response = client.post(
        f"/api/v1/platform/promotional-grants/{grant_id}/revoke",
        json={"reason": "QA — testing revoke endpoint"},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


def test_list_grants_for_tenant(client, super_admin_token, churned_tenants_factory):
    tenants = churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    tenant_id = tenants[0].id
    response = client.get(
        f"/api/v1/platform/promotional-grants/tenant/{tenant_id}",
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

- [ ] **Step 2: Run, expect FAIL**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v -k "revoke or tenant"`
Expected: 404 / handler not implemented.

- [ ] **Step 3: Implement both endpoints**

Append to `app/api/v1/endpoints/platform/promotional_grants.py`:

```python
from app.db.models.promotional_grant import PromotionalGrant
from app.schemas.promotional_grant import (
    PromotionalGrantResponse,
    RevokeGrantRequest,
    RevokeGrantResponse,
)


@router.post(
    "/{grant_id}/revoke",
    response_model=RevokeGrantResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def revoke_grant(
    grant_id: UUID,
    request: RevokeGrantRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    try:
        result = service.revoke_grant(
            grant_id=grant_id,
            reason=request.reason,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
        )
        return RevokeGrantResponse(**result)
    except PromoGrantNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except PromoGrantStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except PromoGrantValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.get(
    "/tenant/{tenant_id}",
    response_model=list[PromotionalGrantResponse],
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_grants_for_tenant(
    tenant_id: UUID,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    grants = (
        db.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant_id)
        .order_by(PromotionalGrant.created_at.desc())
        .all()
    )
    return [PromotionalGrantResponse.model_validate(g) for g in grants]
```

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v`
Expected: All API tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/platform/promotional_grants.py tests/api/v1/platform/test_promotional_grants.py
git commit -m "feat(api): POST /{grant_id}/revoke and GET /tenant/{tenant_id}"
```

---

### Task 12: GET /campaigns + GET /campaigns/{code}

**Files:**
- Modify: `app/api/v1/endpoints/platform/promotional_grants.py`
- Modify: `app/services/promotional_grant_service.py` (aggregate query)
- Modify: `tests/api/v1/platform/test_promotional_grants.py`

- [ ] **Step 1: Append failing test**

```python
def test_list_campaigns_aggregates_stats(client, super_admin_token, db_session, churned_tenants_factory):
    tenants = churned_tenants_factory(count=2, status="trial_expired", churned_days_ago=30)
    preview = client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_STATS",
            "campaign_name": "Stats Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "setup for stats test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    response = client.get(
        "/api/v1/platform/promotional-grants/campaigns",
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "campaigns" in body
    target = next((c for c in body["campaigns"] if c["campaign_code"] == "WINBACK_STATS"), None)
    assert target is not None
    assert target["stats"]["total"] == 2
    assert target["stats"]["active"] == 2
    assert target["stats"]["by_source"]["admin_bulk"] == 2
```

- [ ] **Step 2: Run, expect FAIL**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v -k campaigns`
Expected: 404.

- [ ] **Step 3: Implement aggregate service method**

Append to `app/services/promotional_grant_service.py`:

```python
    # ------------------------------------------------------------------
    # Campaign aggregates
    # ------------------------------------------------------------------

    def list_campaigns_with_stats(self) -> list[dict]:
        from sqlalchemy import func

        rows = (
            self.db.query(
                PromotionalGrant.campaign_code,
                func.max(PromotionalGrant.campaign_name).label("campaign_name"),
                func.max(PromotionalGrant.granted_tier).label("granted_tier"),
                func.max(PromotionalGrant.duration_days).label("duration_days"),
                func.min(PromotionalGrant.created_at).label("first_grant_at"),
                func.count().label("total"),
                func.sum((PromotionalGrant.status == "active").cast_to_int_if_needed()).label("active") if False else func.count(PromotionalGrant.id),
            )
            .group_by(PromotionalGrant.campaign_code)
            .all()
        )

        # Re-fetch per-campaign breakdowns explicitly — clearer than nested CASE expressions
        campaigns = []
        codes = [r.campaign_code for r in rows]
        for code in codes:
            grants = (
                self.db.query(PromotionalGrant)
                .filter(PromotionalGrant.campaign_code == code)
                .all()
            )
            stats = {
                "total": len(grants),
                "active": sum(1 for g in grants if g.status == "active"),
                "converted": sum(1 for g in grants if g.status == "converted"),
                "expired": sum(1 for g in grants if g.status == "expired"),
                "revoked": sum(1 for g in grants if g.status == "revoked"),
                "by_source": {
                    "admin_bulk": sum(1 for g in grants if g.source == "admin_bulk"),
                    "admin_single": sum(1 for g in grants if g.source == "admin_single"),
                    "auto_signup": sum(1 for g in grants if g.source == "auto_signup"),
                },
            }
            head = grants[0]
            campaigns.append({
                "campaign_code": code,
                "campaign_name": head.campaign_name,
                "granted_tier": head.granted_tier,
                "duration_days": head.duration_days,
                "first_grant_at": min(g.created_at for g in grants),
                "stats": stats,
            })
        campaigns.sort(key=lambda c: c["first_grant_at"], reverse=True)
        return campaigns
```

(Note: the aggregation could be a single SQL query with CASE/SUM expressions for performance. v1 prioritizes correctness over speed; campaigns table will be small. Optimize if it becomes an issue.)

- [ ] **Step 4: Implement endpoints**

Append to `app/api/v1/endpoints/platform/promotional_grants.py`:

```python
from app.schemas.promotional_grant import (
    CampaignListResponse,
    CampaignSummary,
    CampaignStats,
    CampaignStatsBySource,
)


@router.get(
    "/campaigns",
    response_model=CampaignListResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_campaigns(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = PromotionalGrantService(db)
    raw = service.list_campaigns_with_stats()
    campaigns = [
        CampaignSummary(
            campaign_code=c["campaign_code"],
            campaign_name=c["campaign_name"],
            granted_tier=c["granted_tier"],
            duration_days=c["duration_days"],
            first_grant_at=c["first_grant_at"],
            stats=CampaignStats(
                total=c["stats"]["total"],
                active=c["stats"]["active"],
                converted=c["stats"]["converted"],
                expired=c["stats"]["expired"],
                revoked=c["stats"]["revoked"],
                by_source=CampaignStatsBySource(**c["stats"]["by_source"]),
            ),
        )
        for c in raw
    ]
    return CampaignListResponse(campaigns=campaigns)


@router.get(
    "/campaigns/{campaign_code}",
    response_model=list[PromotionalGrantResponse],
    dependencies=[Depends(platform_admin_rate_limit)],
)
def list_grants_for_campaign(
    campaign_code: str,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    grants = (
        db.query(PromotionalGrant)
        .filter(PromotionalGrant.campaign_code == campaign_code)
        .order_by(PromotionalGrant.created_at.desc())
        .all()
    )
    return [PromotionalGrantResponse.model_validate(g) for g in grants]
```

- [ ] **Step 5: Run, expect pass**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v`
Expected: All API tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/endpoints/platform/promotional_grants.py app/services/promotional_grant_service.py tests/api/v1/platform/test_promotional_grants.py
git commit -m "feat(api): GET /campaigns and GET /campaigns/{code}"
```

---

### Task 13: GET / PUT /config/auto-signup

**Files:**
- Modify: `app/api/v1/endpoints/platform/promotional_grants.py`
- Modify: `tests/api/v1/platform/test_promotional_grants.py`

- [ ] **Step 1: Append failing tests**

```python
def test_get_auto_signup_config_default_disabled(client, super_admin_token):
    response = client.get(
        "/api/v1/platform/promotional-grants/config/auto-signup",
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False


def test_put_auto_signup_config_enables_with_full_config(client, super_admin_token):
    response = client.put(
        "/api/v1/platform/promotional-grants/config/auto-signup",
        json={
            "enabled": True,
            "campaign_code": "LAUNCH_2026",
            "campaign_name": "Public Launch",
            "granted_tier": "professional",
            "duration_days": 90,
            "valid_until": "2026-08-09T00:00:00+00:00",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["campaign_code"] == "LAUNCH_2026"


def test_put_auto_signup_config_rejects_enable_without_required_fields(client, super_admin_token):
    response = client.put(
        "/api/v1/platform/promotional-grants/config/auto-signup",
        json={
            "enabled": True,
            "campaign_code": None,
            "campaign_name": None,
            "granted_tier": None,
            "duration_days": None,
            "valid_until": None,
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 400
```

- [ ] **Step 2: Run, expect FAIL**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v -k auto_signup`
Expected: 404.

- [ ] **Step 3: Implement endpoints**

Append to `app/api/v1/endpoints/platform/promotional_grants.py`:

```python
from app.schemas.promotional_grant import (
    AutoSignupConfigResponse,
    AutoSignupConfigUpdateRequest,
    AutoSignupConfigUpdateResponse,
)
from app.services.auto_signup_grant_config_service import (
    AutoSignupGrantConfigService,
    AutoSignupConfigValidationError,
)


@router.get(
    "/config/auto-signup",
    response_model=AutoSignupConfigResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def get_auto_signup_config(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    config = AutoSignupGrantConfigService(db).get()
    return AutoSignupConfigResponse.model_validate(config, from_attributes=True)


@router.put(
    "/config/auto-signup",
    response_model=AutoSignupConfigUpdateResponse,
    dependencies=[Depends(platform_admin_rate_limit)],
)
def update_auto_signup_config(
    request: AutoSignupConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
):
    service = AutoSignupGrantConfigService(db)
    try:
        updated = service.update(
            enabled=request.enabled,
            campaign_code=request.campaign_code,
            campaign_name=request.campaign_name,
            granted_tier=request.granted_tier,
            duration_days=request.duration_days,
            valid_until=request.valid_until,
            admin_id=current_admin.id,
            admin_name=current_admin.email,
        )
    except AutoSignupConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return AutoSignupConfigUpdateResponse.model_validate(updated, from_attributes=True)
```

Add `from_attributes` to the schemas if not already set: in `app/schemas/promotional_grant.py`, add `model_config = {"from_attributes": True}` to `AutoSignupConfigResponse`.

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/api/v1/platform/test_promotional_grants.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/platform/promotional_grants.py app/schemas/promotional_grant.py tests/api/v1/platform/test_promotional_grants.py
git commit -m "feat(api): GET/PUT /config/auto-signup"
```

---

## Phase 4 — Email templates and notifications

### Task 14: Email templates

**Files:**
- Create: `app/templates/emails/promo_granted.html`
- Create: `app/templates/emails/promo_expiring.html`
- Create: `app/templates/emails/promo_expired.html`

- [ ] **Step 1: Read the existing trial_expiring template for the established style**

Run: `cat app/templates/emails/trial_expiring.html | head -50`
Expected: HTML structure inheriting from `base.html`. Match this style.

- [ ] **Step 2: Create promo_granted.html**

Create `app/templates/emails/promo_granted.html`:

```html
{% extends "base.html" %}

{% block content %}
<h1>Welcome back to Luran, {{ tenant_display_name }} 👋</h1>

<p>We've added <strong>{{ duration_days }} days of {{ granted_tier_display }}</strong> access to your account, no card required.</p>

<p>This is part of our <strong>{{ campaign_name }}</strong> campaign. We've made significant improvements since you last tried Luran, and we'd love for you to see what's changed.</p>

<p>Your access ends on <strong>{{ grant_ends_at_display }}</strong>. If you decide Luran is right for your team, you can subscribe at any time and your data and integrations stay exactly as they are.</p>

<p style="margin: 32px 0;">
  <a href="{{ dashboard_url }}" style="background: #312e81; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none;">
    Open Luran
  </a>
</p>

<p style="color: #666; font-size: 14px;">Questions? Just reply to this email.</p>
{% endblock %}
```

- [ ] **Step 3: Create promo_expiring.html**

Create `app/templates/emails/promo_expiring.html`:

```html
{% extends "base.html" %}

{% block content %}
<h1>Your {{ granted_tier_display }} access ends in {{ days_remaining }} day{{ 's' if days_remaining != 1 else '' }}</h1>

<p>Hi {{ tenant_display_name }},</p>

<p>Your free {{ granted_tier_display }} access from the <strong>{{ campaign_name }}</strong> campaign expires on <strong>{{ grant_ends_at_display }}</strong>.</p>

<p>To keep your data, integrations, and team intact, subscribe before then:</p>

<p style="margin: 32px 0;">
  <a href="{{ subscribe_url }}" style="background: #312e81; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none;">
    Subscribe to {{ granted_tier_display }}
  </a>
</p>

<p style="color: #666; font-size: 14px;">If you decide not to subscribe, your data is preserved — you'll just need an active subscription to access it again.</p>
{% endblock %}
```

- [ ] **Step 4: Create promo_expired.html**

Create `app/templates/emails/promo_expired.html`:

```html
{% extends "base.html" %}

{% block content %}
<h1>Your {{ granted_tier_display }} access has ended</h1>

<p>Hi {{ tenant_display_name }},</p>

<p>The free {{ granted_tier_display }} access from the <strong>{{ campaign_name }}</strong> campaign has ended.</p>

<p>Your data, integrations, and configuration are all preserved. To restore full access, subscribe to any tier:</p>

<p style="margin: 32px 0;">
  <a href="{{ subscribe_url }}" style="background: #312e81; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none;">
    View plans and subscribe
  </a>
</p>

<p style="color: #666; font-size: 14px;">Thanks for trying Luran. We'd love to hear what worked and what didn't — just reply to this email.</p>
{% endblock %}
```

- [ ] **Step 5: Render-test the templates**

Run:
```bash
python -c "
from app.services.email_service import EmailService
svc = EmailService()
print(svc.render_template('promo_granted.html', tenant_display_name='Test', granted_tier_display='Professional', duration_days=90, campaign_name='Win-Back', grant_ends_at_display='August 9, 2026', dashboard_url='https://app.luran.io')[:200])
print('---')
print(svc.render_template('promo_expiring.html', tenant_display_name='Test', granted_tier_display='Professional', days_remaining=7, campaign_name='Win-Back', grant_ends_at_display='August 9, 2026', subscribe_url='https://app.luran.io/billing')[:200])
"
```
Expected: Two HTML snippets render without errors.

- [ ] **Step 6: Commit**

```bash
git add app/templates/emails/promo_granted.html app/templates/emails/promo_expiring.html app/templates/emails/promo_expired.html
git commit -m "feat(email): promo grant lifecycle email templates"
```

---

### Task 15: EmailNotificationService send methods

**Files:**
- Modify: `app/services/email_notification_service.py`
- Test: extend `tests/services/` with a small notification test

- [ ] **Step 1: Read the existing send_trial_expiration_reminder for the pattern**

Run: `sed -n '270,390p' app/services/email_notification_service.py`
Expected: a method that builds notification_type, calls `_check_if_already_sent`, renders template, sends, records.

- [ ] **Step 2: Write failing test for send_promo_expiration_reminder**

Add `tests/services/test_email_notification_promo.py`:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant
from app.services.email_notification_service import EmailNotificationService


@pytest.mark.asyncio
async def test_send_promo_expiration_reminder_writes_notification(db_session, monkeypatch):
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Test",
        api_key_hash="dummy",
        subscription_tier="professional",
        subscription_status="active",
    )
    db_session.add(tenant)
    db_session.commit()
    grant = PromotionalGrant(
        tenant_id=tenant.id,
        campaign_code="LAUNCH_2026",
        campaign_name="Launch",
        granted_tier="professional",
        duration_days=90,
        grant_starts_at=datetime.now(UTC) - timedelta(days=83),
        grant_ends_at=datetime.now(UTC) + timedelta(days=7),
        previous_tier="trial",
        previous_status="trial_expired",
        status="active",
        source="auto_signup",
        reason="auto_signup",
    )
    db_session.add(grant)
    db_session.commit()

    sent_calls = []

    async def fake_send(self, **kwargs):
        sent_calls.append(kwargs)
        return True

    # Monkeypatch the actual SMTP/sendmail layer; depends on existing email_service contract.
    # If `send_templated_email` is on EmailService, patch that instead.
    monkeypatch.setattr(
        "app.services.email_service.EmailService.send_templated_email",
        lambda self, **kw: True,
    )

    service = EmailNotificationService(db_session)
    success = await service.send_promo_expiration_reminder(
        grant_id=grant.id, days_remaining=7
    )
    assert success is True
```

- [ ] **Step 3: Implement send methods using the existing async patterns**

The existing `email_notification_service.py` is fully async. Use these established helpers (already on `self`):
- `await self._execute(query)` — run an async SQLAlchemy query.
- `await self._get_tenant_admin(tenant_id)` — returns the primary admin user (`.email`, `.full_name`); used by `send_trial_expiration_reminder` at line 314.
- `await self._check_if_already_sent(tenant_id, notification_type, hours=N)` — dedup guard; used at line 293.
- `await self._record_notification(tenant_id, notification_type, recipient_email, subject, template_data, status, error_message=None)` — audit record; used at line 295 and 360.
- `self.email_service.jinja_env.get_template(name)` — load template (sync; returns Jinja `Template`).
- `self.email_service._send_email(to_email, subject, html_content)` — sends via SMTP (sync).

Append to `app/services/email_notification_service.py`:

```python
    async def send_promo_expiration_reminder(
        self, grant_id: uuid.UUID, days_remaining: int
    ) -> bool:
        """Send a 'promo expiring in N days' email. Idempotent within 20h."""
        from app.db.models.promotional_grant import PromotionalGrant

        try:
            notification_type = f"promo_expiring_{days_remaining}d"

            grant_query = select(PromotionalGrant).where(PromotionalGrant.id == grant_id)
            grant_result = await self._execute(grant_query)
            grant = grant_result.scalars().first()
            if grant is None or grant.status != "active":
                return False

            if await self._check_if_already_sent(grant.tenant_id, notification_type, hours=20):
                logger.info(
                    f"Promo expiring email already sent for tenant {grant.tenant_id} "
                    f"window {days_remaining}d"
                )
                return True

            tenant_query = select(Tenant).where(Tenant.id == grant.tenant_id)
            tenant_result = await self._execute(tenant_query)
            tenant = tenant_result.scalars().first()
            if tenant is None:
                return False

            admin = await self._get_tenant_admin(grant.tenant_id)
            if not admin:
                logger.warning(f"No admin found for tenant {grant.tenant_id}; skipping reminder")
                return False

            template_data = {
                "admin_name": admin.full_name,
                "tenant_display_name": tenant.display_name,
                "granted_tier_display": grant.granted_tier.title(),
                "days_remaining": days_remaining,
                "campaign_name": grant.campaign_name or grant.campaign_code,
                "grant_ends_at_display": grant.grant_ends_at.strftime("%B %d, %Y"),
                "subscribe_url": f"{settings.FRONTEND_URL}/dashboard/billing?tier={grant.granted_tier}",
                "current_year": datetime.now().year,
            }
            subject = (
                f"Your {grant.granted_tier.title()} access ends in {days_remaining} "
                f"day{'s' if days_remaining != 1 else ''} - {tenant.display_name}"
            )

            try:
                template = self.email_service.jinja_env.get_template("promo_expiring.html")
                html_content = template.render(**template_data)
                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )
                await self._record_notification(
                    tenant_id=grant.tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )
                return success
            except Exception as e:
                logger.error(f"Failed to send promo expiring reminder for grant {grant_id}: {e}")
                await self._record_notification(
                    tenant_id=grant.tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email if admin else "",
                    subject=subject,
                    template_data=template_data,
                    status="failed",
                    error_message=str(e),
                )
                return False
        except Exception as e:
            logger.error(f"Error in send_promo_expiration_reminder: {e}")
            return False

    async def send_promo_expired_email(self, grant_id: uuid.UUID) -> bool:
        """Send 'your promo has ended' email after revert. Idempotent within 20h."""
        from app.db.models.promotional_grant import PromotionalGrant

        try:
            notification_type = "promo_expired"

            grant_query = select(PromotionalGrant).where(PromotionalGrant.id == grant_id)
            grant = (await self._execute(grant_query)).scalars().first()
            if grant is None:
                return False

            if await self._check_if_already_sent(grant.tenant_id, notification_type, hours=20):
                return True

            tenant = (
                await self._execute(select(Tenant).where(Tenant.id == grant.tenant_id))
            ).scalars().first()
            if tenant is None:
                return False
            admin = await self._get_tenant_admin(grant.tenant_id)
            if not admin:
                return False

            template_data = {
                "admin_name": admin.full_name,
                "tenant_display_name": tenant.display_name,
                "granted_tier_display": grant.granted_tier.title(),
                "campaign_name": grant.campaign_name or grant.campaign_code,
                "subscribe_url": f"{settings.FRONTEND_URL}/dashboard/billing",
                "current_year": datetime.now().year,
            }
            subject = f"Your {grant.granted_tier.title()} access has ended - {tenant.display_name}"

            try:
                template = self.email_service.jinja_env.get_template("promo_expired.html")
                html_content = template.render(**template_data)
                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )
                await self._record_notification(
                    tenant_id=grant.tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )
                return success
            except Exception as e:
                logger.error(f"Failed to send promo expired email for grant {grant_id}: {e}")
                return False
        except Exception as e:
            logger.error(f"Error in send_promo_expired_email: {e}")
            return False

    async def send_promo_granted_email(self, grant_id: uuid.UUID) -> bool:
        """Send 'welcome back, here's N days of Pro' email for admin_bulk grants.

        Auto-signup grants do not call this — the standard tenant welcome email
        covers their case. Idempotent over a 7-day window so re-running a
        campaign on the same tenant doesn't double-send.
        """
        from app.db.models.promotional_grant import PromotionalGrant

        try:
            notification_type = "promo_granted"

            grant_query = select(PromotionalGrant).where(PromotionalGrant.id == grant_id)
            grant = (await self._execute(grant_query)).scalars().first()
            if grant is None or grant.status != "active":
                return False

            if await self._check_if_already_sent(grant.tenant_id, notification_type, hours=24 * 7):
                return True

            tenant = (
                await self._execute(select(Tenant).where(Tenant.id == grant.tenant_id))
            ).scalars().first()
            if tenant is None:
                return False
            admin = await self._get_tenant_admin(grant.tenant_id)
            if not admin:
                return False

            template_data = {
                "admin_name": admin.full_name,
                "tenant_display_name": tenant.display_name,
                "granted_tier_display": grant.granted_tier.title(),
                "duration_days": grant.duration_days,
                "campaign_name": grant.campaign_name or grant.campaign_code,
                "grant_ends_at_display": grant.grant_ends_at.strftime("%B %d, %Y"),
                "dashboard_url": settings.FRONTEND_URL,
                "current_year": datetime.now().year,
            }
            subject = (
                f"We've added {grant.duration_days} days of "
                f"{grant.granted_tier.title()} to your account - {tenant.display_name}"
            )

            try:
                template = self.email_service.jinja_env.get_template("promo_granted.html")
                html_content = template.render(**template_data)
                success = self.email_service._send_email(
                    to_email=admin.email, subject=subject, html_content=html_content
                )
                await self._record_notification(
                    tenant_id=grant.tenant_id,
                    notification_type=notification_type,
                    recipient_email=admin.email,
                    subject=subject,
                    template_data=template_data,
                    status="sent" if success else "failed",
                    error_message=None if success else "SMTP send failed",
                )
                return success
            except Exception as e:
                logger.error(f"Failed to send promo granted email for grant {grant_id}: {e}")
                return False
        except Exception as e:
            logger.error(f"Error in send_promo_granted_email: {e}")
            return False
```

The new methods reuse `_get_tenant_admin`, `_check_if_already_sent`, and `_record_notification` exactly as `send_trial_expiration_reminder` does — no new helpers needed.

- [ ] **Step 4: Run test, expect pass**

Run: `pytest tests/services/test_email_notification_promo.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/email_notification_service.py tests/services/test_email_notification_promo.py
git commit -m "feat(email): promo expiration reminder, granted, and expired notifications"
```

---

## Phase 5 — Worker

### Task 16: Worker function — reminder pass + expiry pass

**Files:**
- Modify: `app/workers/document_processor.py`
- Test: `tests/workers/test_promotional_grant_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Create `tests/workers/test_promotional_grant_lifecycle.py`:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from freezegun import freeze_time

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant
from app.services.promotional_grant_service import PromotionalGrantService


def _make_active_grant(db_session, *, days_until_end):
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Test",
        api_key_hash="dummy",
        subscription_tier="professional",
        subscription_status="active",
    )
    db_session.add(tenant)
    db_session.commit()
    grant = PromotionalGrant(
        tenant_id=tenant.id,
        campaign_code="LAUNCH_TEST",
        campaign_name="Launch Test",
        granted_tier="professional",
        duration_days=90,
        grant_starts_at=datetime.now(UTC) - timedelta(days=90 - days_until_end),
        grant_ends_at=datetime.now(UTC) + timedelta(days=days_until_end),
        previous_tier="trial",
        previous_status="trial_expired",
        status="active",
        source="admin_bulk",
        reason="test grant",
    )
    db_session.add(grant)
    db_session.commit()
    return tenant, grant


@pytest.mark.asyncio
async def test_worker_sends_reminders_at_t14(db_session, monkeypatch):
    from app.workers.document_processor import process_promotional_grant_lifecycle

    tenant, grant = _make_active_grant(db_session, days_until_end=14)
    sent = []

    async def fake_send(self, *, grant_id, days_remaining):
        sent.append((grant_id, days_remaining))
        return True

    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expiration_reminder",
        fake_send,
    )

    await process_promotional_grant_lifecycle({})
    assert (grant.id, 14) in sent


@pytest.mark.asyncio
async def test_worker_skips_reminders_outside_window(db_session, monkeypatch):
    from app.workers.document_processor import process_promotional_grant_lifecycle

    _, grant = _make_active_grant(db_session, days_until_end=10)  # not 14, 7, or 1
    sent = []
    async def fake_send(self, *, grant_id, days_remaining):
        sent.append((grant_id, days_remaining))
        return True
    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expiration_reminder",
        fake_send,
    )
    await process_promotional_grant_lifecycle({})
    assert grant.id not in [s[0] for s in sent]


@pytest.mark.asyncio
async def test_worker_expires_past_due_grants(db_session, monkeypatch):
    from app.workers.document_processor import process_promotional_grant_lifecycle

    tenant, grant = _make_active_grant(db_session, days_until_end=-1)  # past due
    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expiration_reminder",
        lambda self, **kw: True,
    )
    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expired_email",
        lambda self, **kw: True,
    )

    await process_promotional_grant_lifecycle({})
    db_session.refresh(grant)
    db_session.refresh(tenant)
    assert grant.status == "expired"
    assert tenant.subscription_tier == "trial"
    assert tenant.subscription_status == "trial_expired"
```

If `tests/workers/` directory doesn't exist, create it with an `__init__.py` and `conftest.py` mirroring tests/services.

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/workers/test_promotional_grant_lifecycle.py -v`
Expected: ImportError on `process_promotional_grant_lifecycle`.

- [ ] **Step 3: Implement worker function**

Append to `app/workers/document_processor.py` (alongside `check_trial_expiration_3days`):

```python
async def process_promotional_grant_lifecycle(ctx: dict) -> dict:
    """Daily worker: send reminders and process grant expiries.

    Scheduled: Daily at 09:00 UTC (alongside trial expiration reminders).
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import and_, select

    from app.db.models.promotional_grant import PromotionalGrant
    from app.services.email_notification_service import EmailNotificationService
    from app.services.promotional_grant_service import PromotionalGrantService

    db = SessionLocal()
    reminders_sent = 0
    expired = 0
    errors = 0
    try:
        email_service = EmailNotificationService(db)
        promo_service = PromotionalGrantService(db)

        # ---- Pass 1: reminders at T-14, T-7, T-1 ----
        for days_remaining in [14, 7, 1]:
            target_date = datetime.now(UTC) + timedelta(days=days_remaining)
            date_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = select(PromotionalGrant).where(
                and_(
                    PromotionalGrant.status == "active",
                    PromotionalGrant.grant_ends_at >= date_start,
                    PromotionalGrant.grant_ends_at <= date_end,
                )
            )
            grants = db.execute(query).scalars().all()
            for grant in grants:
                try:
                    success = await email_service.send_promo_expiration_reminder(
                        grant_id=grant.id, days_remaining=days_remaining
                    )
                    if success:
                        reminders_sent += 1
                except Exception as e:
                    logger.error(f"Promo reminder error for grant {grant.id}: {e}")
                    errors += 1

        # ---- Pass 2: expire past-due grants ----
        # expire_grants_due processes all due grants and returns their IDs.
        # We then dispatch a per-grant 'expired' email for each.
        try:
            expired_ids = promo_service.expire_grants_due()
            expired = len(expired_ids)
        except Exception as e:
            logger.error(f"Promo expiry pass error: {e}")
            errors += 1
            expired_ids = []

        for grant_id in expired_ids:
            try:
                await email_service.send_promo_expired_email(grant_id=grant_id)
            except Exception as e:
                logger.error(f"Promo expired email error for grant {grant_id}: {e}")
                errors += 1

        logger.info(
            "Promo lifecycle complete. reminders={} expired={} errors={}",
            reminders_sent, expired, errors,
        )
    finally:
        db.close()

    return {"reminders_sent": reminders_sent, "expired": expired, "errors": errors}
```

Then in `WorkerSettings.cron_jobs` (around line 1037), add:

```python
        cron(
            process_promotional_grant_lifecycle,
            hour={9},
            minute={5},  # 5 minutes after trial reminders to avoid lock contention
            run_at_startup=False,
        ),
```

Add `process_promotional_grant_lifecycle` to the `functions` list in `WorkerSettings`.

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/workers/test_promotional_grant_lifecycle.py -v`
Expected: All 3 worker tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/workers/document_processor.py tests/workers/test_promotional_grant_lifecycle.py
git commit -m "feat(worker): promotional grant lifecycle (reminders + expiry)"
```

---

## Phase 6 — Hooks into existing flows

### Task 17: Auto-signup hook in tenant_service.create_tenant

**Files:**
- Modify: `app/services/tenant_service.py`
- Test: `tests/services/test_tenant_service_promo_auto_grant.py` (or extend existing)

- [ ] **Step 1: Read the existing create_tenant**

Run: `sed -n '70,180p' app/services/tenant_service.py`
Expected: see how the tenant is created and committed; identify the safe place to add the auto-grant hook (after `db.commit()` of the tenant).

- [ ] **Step 2: Write failing test**

Create `tests/services/test_tenant_service_promo_auto_grant.py`:

```python
from datetime import UTC, datetime, timedelta

from app.db.models.promotional_grant import AutoSignupGrantConfig, PromotionalGrant
from app.services.auto_signup_grant_config_service import AutoSignupGrantConfigService
from app.services.tenant_service import TenantService
from app.schemas.tenant import TenantCreate


def test_create_tenant_with_auto_signup_disabled_no_grant(db_session):
    service = TenantService(db_session)
    tenant_data = TenantCreate(name=f"t1", display_name="T1", api_key="testkey1")  # adjust to actual schema
    tenant = service.create_tenant(tenant_data)
    grants = (
        db_session.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant.id)
        .all()
    )
    assert len(grants) == 0


def test_create_tenant_with_auto_signup_enabled_creates_grant(db_session, super_admin_user):
    AutoSignupGrantConfigService(db_session).update(
        enabled=True,
        campaign_code="LAUNCH_2026",
        campaign_name="Launch",
        granted_tier="professional",
        duration_days=90,
        valid_until=None,
        admin_id=super_admin_user.id,
        admin_name=super_admin_user.email,
    )
    service = TenantService(db_session)
    tenant_data = TenantCreate(name=f"t2", display_name="T2", api_key="testkey2")
    tenant = service.create_tenant(tenant_data)
    grants = (
        db_session.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant.id)
        .all()
    )
    assert len(grants) == 1
    assert grants[0].source == "auto_signup"
    assert grants[0].granted_tier == "professional"
```

Adjust `TenantCreate` field names to match the actual schema definition.

- [ ] **Step 3: Run, expect FAIL**

Run: `pytest tests/services/test_tenant_service_promo_auto_grant.py -v`
Expected: Second test fails (no grant created).

- [ ] **Step 4: Implement hook**

In `app/services/tenant_service.py`, find the end of `create_tenant` (right before the return). Add:

```python
        # Auto-grant on signup, if campaign is active
        from app.services.auto_signup_grant_config_service import AutoSignupGrantConfigService
        from app.services.promotional_grant_service import PromotionalGrantService

        config_svc = AutoSignupGrantConfigService(self.db)
        if config_svc.is_active():
            cfg = config_svc.get()
            try:
                PromotionalGrantService(self.db).create_grant(
                    tenant_id=tenant.id,
                    campaign_code=cfg.campaign_code,
                    campaign_name=cfg.campaign_name,
                    granted_tier=cfg.granted_tier,
                    duration_days=cfg.duration_days,
                    source="auto_signup",
                    granted_by_admin_id=None,
                    granted_by_admin_name="system",
                    reason=f"Auto-grant on signup for campaign {cfg.campaign_code}",
                )
            except Exception as e:
                # Auto-grant failure must never block tenant creation.
                logger.error(f"Auto-grant failed for tenant {tenant.id}: {e}")
```

The `tenant.id` must be available (post-commit). If `create_tenant` returns a `TenantResponse` (not the SQLAlchemy `Tenant`), refactor so the hook runs against the in-memory tenant before transformation, or refetch.

- [ ] **Step 5: Run, expect pass**

Run: `pytest tests/services/test_tenant_service_promo_auto_grant.py -v`
Expected: Both tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/tenant_service.py tests/services/test_tenant_service_promo_auto_grant.py
git commit -m "feat(promo): auto-grant on signup when config is active"
```

---

### Task 18: Conversion detection in webhook_handler_service

**Files:**
- Modify: `app/services/webhook_handler_service.py`
- Test: `tests/services/test_webhook_handler_service_promo_conversion.py`

- [ ] **Step 1: Read the existing payment success handler**

Run: `grep -n "subscription_payment_success\|handle_subscription\|tenant.subscription_tier =" app/services/webhook_handler_service.py | head -20`
Expected: locate the function that runs on successful Paystack payment.

- [ ] **Step 2: Write failing test**

Create `tests/services/test_webhook_handler_service_promo_conversion.py`:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant
from app.services.promotional_grant_service import PromotionalGrantService
from app.services.webhook_handler_service import WebhookHandlerService


def test_payment_success_marks_active_grant_converted(db_session, super_admin_user):
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Test",
        api_key_hash="dummy",
        subscription_tier="trial",
        subscription_status="trial_expired",
    )
    db_session.add(tenant)
    db_session.commit()
    promo = PromotionalGrantService(db_session)
    grant = promo.create_grant(
        tenant_id=tenant.id,
        campaign_code="LAUNCH_TEST",
        campaign_name="Test",
        granted_tier="professional",
        duration_days=90,
        source="auto_signup",
        granted_by_admin_id=None,
        granted_by_admin_name="system",
        reason="test setup",
    )

    # Construct a payload mirroring Paystack subscription.create or charge.success
    payload = {
        "event": "charge.success",
        "data": {
            "metadata": {
                "tenant_id": str(tenant.id),
                "tier": "professional",
                "billing_cycle": "monthly",
            },
            "amount": 1000_00,
            "reference": "test-ref",
            "status": "success",
        },
    }
    handler = WebhookHandlerService(db_session)
    handler.handle_paystack_webhook(payload)  # use whatever the actual entrypoint is

    db_session.refresh(grant)
    assert grant.status == "converted"
    assert grant.converted_at is not None
```

Adjust the payload shape and handler entrypoint to match the actual webhook handler API.

- [ ] **Step 3: Run, expect FAIL**

Run: `pytest tests/services/test_webhook_handler_service_promo_conversion.py -v`
Expected: grant.status remains 'active'.

- [ ] **Step 4: Implement conversion call**

In the Paystack payment-success handler in `app/services/webhook_handler_service.py`, after the existing tenant subscription update succeeds:

```python
        # Convert any active promotional grant
        from app.services.promotional_grant_service import PromotionalGrantService
        try:
            PromotionalGrantService(self.db).mark_converted_for_tenant(
                tenant_id=tenant.id,
                paid_tier=tenant.subscription_tier,
                paid_billing_cycle=tenant.billing_cycle,
            )
        except Exception as e:
            logger.error(f"Promo conversion mark failed for tenant {tenant.id}: {e}")
```

- [ ] **Step 5: Run, expect pass**

Run: `pytest tests/services/test_webhook_handler_service_promo_conversion.py -v`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/webhook_handler_service.py tests/services/test_webhook_handler_service_promo_conversion.py
git commit -m "feat(webhook): mark promo grant converted on Paystack payment success"
```

---

## Phase 7 — End-to-end test + verification

### Task 19: E2E test (full lifecycle)

**Files:**
- Create: `tests/e2e/test_promotional_grants_e2e.py`

- [ ] **Step 1: Write E2E test**

Create `tests/e2e/test_promotional_grants_e2e.py`:

```python
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from freezegun import freeze_time

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant
from app.services.promotional_grant_service import PromotionalGrantService


@pytest.mark.asyncio
async def test_full_lifecycle_grant_to_expiry(db_session, monkeypatch, super_admin_user):
    """
    1. Create churned tenant.
    2. Bulk-grant via service (mirrors the API path).
    3. Fast-forward to T-1, run worker, assert reminder was triggered.
    4. Fast-forward past T+0, run worker, assert tenant reverted.
    """
    from app.workers.document_processor import process_promotional_grant_lifecycle

    tenant = Tenant(
        name=f"e2e-{uuid4().hex[:8]}",
        display_name="E2E Tenant",
        api_key_hash="dummy",
        subscription_tier="trial",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(tenant)
    db_session.commit()

    service = PromotionalGrantService(db_session)
    grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="E2E_TEST",
        campaign_name="E2E Test",
        granted_tier="professional",
        duration_days=90,
        source="admin_bulk",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="e2e test setup",
    )
    db_session.refresh(tenant)
    assert tenant.subscription_tier == "professional"
    assert tenant.subscription_status == "active"

    sent_reminders = []

    async def fake_send(self, *, grant_id, days_remaining):
        sent_reminders.append((grant_id, days_remaining))
        return True

    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expiration_reminder",
        fake_send,
    )
    monkeypatch.setattr(
        "app.services.email_notification_service.EmailNotificationService.send_promo_expired_email",
        lambda self, **kw: True,
    )

    # T-1
    target = grant.grant_ends_at - timedelta(days=1)
    with freeze_time(target):
        await process_promotional_grant_lifecycle({})
    assert (grant.id, 1) in sent_reminders

    # T+1 (past expiry)
    after = grant.grant_ends_at + timedelta(days=1)
    with freeze_time(after):
        await process_promotional_grant_lifecycle({})

    db_session.refresh(grant)
    db_session.refresh(tenant)
    assert grant.status == "expired"
    assert tenant.subscription_tier == "trial"
    assert tenant.subscription_status == "trial_expired"
```

- [ ] **Step 2: Run E2E test**

Run: `pytest tests/e2e/test_promotional_grants_e2e.py -v`
Expected: pass.

- [ ] **Step 3: Run the entire test suite to confirm no regressions**

Run: `pytest tests/ -q`
Expected: All tests pass; the green count delta vs Task 0 baseline equals only the new tests we added (no pre-existing tests went red).

- [ ] **Step 4: Lint**

Run: `ruff check app/ tests/ && black --check app/ tests/`
Expected: clean.

- [ ] **Step 5: Verify migration round-trip on staging-like environment**

Run: `alembic downgrade -1 && alembic upgrade head`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/test_promotional_grants_e2e.py
git commit -m "test(promo): e2e lifecycle from grant to expiry"
```

---

### Task 20: Manual verification + acceptance handoff

**Files:** none (verification only)

- [ ] **Step 1: Spin up the API locally and run a smoke test**

Run: `uvicorn app.main:app --reload --port 8000` (or the project's standard local-run command).

In a second terminal, with a super-admin token:

```bash
# Preview cohort
curl -X POST http://localhost:8000/api/v1/platform/promotional-grants/preview \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filters":{"subscription_statuses":["trial_expired"],"churned_within_days":365}}'
```
Expected: 200 with the 5 trial_expired tenants and a preview_token.

- [ ] **Step 2: Bulk-grant the WINBACK_MAY_2026 campaign**

```bash
curl -X POST http://localhost:8000/api/v1/platform/promotional-grants/ \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "preview_token":"<token from step 1>",
    "campaign_code":"WINBACK_MAY_2026",
    "campaign_name":"May 2026 Win-Back",
    "granted_tier":"professional",
    "duration_days":90,
    "force_replace_active":false,
    "reason":"Q2 win-back per marketing plan"
  }'
```
Expected: 200 with `granted: 5, skipped: 0, failed: 0`.

- [ ] **Step 3: Verify a single tenant in the DB**

```sql
SELECT t.name, t.subscription_tier, t.subscription_status,
       g.campaign_code, g.granted_tier, g.grant_ends_at, g.status
FROM tenants t
JOIN promotional_grants g ON g.tenant_id = t.id
WHERE g.campaign_code = 'WINBACK_MAY_2026'
ORDER BY t.name;
```
Expected: 5 rows, tier=professional, status=active, grant.status=active.

- [ ] **Step 4: Enable auto-signup config**

```bash
curl -X PUT http://localhost:8000/api/v1/platform/promotional-grants/config/auto-signup \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled":true,
    "campaign_code":"LAUNCH_2026",
    "campaign_name":"Launch 2026",
    "granted_tier":"professional",
    "duration_days":90,
    "valid_until":"2026-08-09T00:00:00+00:00"
  }'
```
Expected: 200 with `enabled: true`.

- [ ] **Step 5: Create a test signup**

Use the existing tenant signup flow (whatever the project's pattern is — e.g. `POST /api/v1/auth/register`). Verify the new tenant lands on `professional` immediately, has a `promotional_grants` row with `source='auto_signup'`.

- [ ] **Step 6: Acceptance checklist**

- [ ] All endpoints from the spec §6 are deployed and reachable.
- [ ] Worker is registered in `WorkerSettings.cron_jobs`.
- [ ] Email templates render without exceptions.
- [ ] Auto-signup config is editable via API.
- [ ] Bulk grant of the 5 trial_expired tenants succeeds in production.
- [ ] Auto-signup is enabled in production with the chosen campaign code.
- [ ] Operator runbook entry written (separate plan against luran-docs).
- [ ] Dashboard UI plan started (separate plan against luran-dashboard).

- [ ] **Step 7: Tag the merge**

```bash
git tag -a promo-grants-backend-v1 -m "Promotional grants backend v1 deployed"
```

---

## Verification gates (run before declaring done)

1. `pytest tests/ -q` — all green; deltas vs Task 0 baseline are only the new tests we added.
2. `ruff check app/ tests/ && black --check app/ tests/` — clean.
3. `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` — clean round-trip.
4. Manual API smoke (Task 20 steps 1-5) succeeds against a local instance.
5. Spec §15 acceptance criteria items checked off.
