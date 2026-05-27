"""Test-only fixture endpoints for E2E setup.

These endpoints are env-gated and respond 404 in staging/production. They
exist solely to give Playwright a clean, deterministic database state for
the promotional-grants test suite.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_current_super_admin, get_db
from app.core.config import settings
from app.db.models.tenant import Tenant, TenantCapabilities, TenantStatus
from app.schemas.platform_admin import PlatformAdminResponse

router = APIRouter()


def require_test_env() -> None:
    if settings.ENVIRONMENT in ("staging", "production"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


class CreateChurnedTenantRequest(BaseModel):
    suffix: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z0-9]+$")
    days_ago: int = Field(default=30, ge=1, le=365)


class CreateChurnedTenantResponse(BaseModel):
    tenant_id: str
    name: str


@router.post(
    "/reset-promotional-grants",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_test_env)],
)
def reset_promotional_grants(
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
) -> None:
    """Wipe promotional grants and disable the auto-signup singleton.

    Truncates the grants table and resets the auto-signup config row to its
    seeded (disabled) state. Tenants are NOT touched — only grant-related
    state is reset.
    """
    db.execute(text("DELETE FROM promotional_grants"))
    # Remove churned test tenants seeded by create-churned-tenant; the
    # tenant_capabilities FK cascades on delete.
    db.execute(text("DELETE FROM tenants WHERE name LIKE 'churned\\_%' ESCAPE '\\'"))
    db.execute(
        text(
            """
            UPDATE auto_signup_grant_config
            SET enabled = FALSE,
                campaign_code = NULL,
                campaign_name = NULL,
                granted_tier = NULL,
                duration_days = NULL,
                valid_until = NULL,
                updated_by_admin_id = NULL,
                updated_by_admin_name = NULL,
                updated_at = NOW()
            """
        )
    )
    db.commit()


@router.post(
    "/create-churned-tenant",
    response_model=CreateChurnedTenantResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_test_env)],
)
def create_churned_tenant(
    payload: CreateChurnedTenantRequest,
    db: Session = Depends(get_db),
    current_admin: PlatformAdminResponse = Depends(get_current_super_admin),
) -> CreateChurnedTenantResponse:
    """Create a tenant in canceled state with subscription_ends_at in the past.

    Used by grants-wizard / grants-revoke / grants-campaign-detail specs to
    seed cohort members for the "churned" filter. The tenant is fully
    independent (no admin user, no AI config) — it only needs to satisfy the
    cohort-build query on Tenant.subscription_status + subscription_ends_at.
    """
    now = datetime.now(UTC)
    name = f"churned_{payload.suffix}"

    churned_at = now - timedelta(days=payload.days_ago)
    tenant = Tenant(
        name=name,
        display_name=f"Churned Tenant {payload.suffix}",
        status=TenantStatus.INACTIVE,
        api_key_hash="fixture_no_auth",
        subscription_tier="trial",
        subscription_status="trial_expired",
        trial_ends_at=churned_at,
        subscription_ends_at=churned_at,
    )
    db.add(tenant)
    db.flush()

    db.add(TenantCapabilities(tenant_id=tenant.id))
    db.commit()
    db.refresh(tenant)

    return CreateChurnedTenantResponse(tenant_id=str(tenant.id), name=tenant.name)
