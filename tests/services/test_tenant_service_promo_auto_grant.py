"""Tests: auto-signup grant is created (or not) when a new tenant is created."""

from uuid import uuid4

from app.db.models.promotional_grant import PromotionalGrant
from app.schemas.tenant import TenantCreate
from app.services.auto_signup_grant_config_service import AutoSignupGrantConfigService
from app.services.tenant_service import TenantService


def _build_tenant_create() -> TenantCreate:
    """Build a minimal valid TenantCreate."""
    return TenantCreate(
        name=f"t{uuid4().hex[:8]}",
        display_name="Test Tenant",
    )


def test_create_tenant_with_auto_signup_disabled_no_grant(db_session):
    """When auto-signup is disabled (default), no grant should be created."""
    service = TenantService(db_session)
    tenant_data = _build_tenant_create()
    result = service.create_tenant(tenant_data)
    tenant_id = result.id if hasattr(result, "id") else result["id"]

    grants = (
        db_session.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant_id)
        .all()
    )
    assert len(grants) == 0


def test_create_tenant_with_auto_signup_enabled_creates_grant(db_session, super_admin_user):
    """When auto-signup config is active, a grant should be created automatically."""
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
    tenant_data = _build_tenant_create()
    result = service.create_tenant(tenant_data)
    tenant_id = result.id if hasattr(result, "id") else result["id"]

    grants = (
        db_session.query(PromotionalGrant)
        .filter(PromotionalGrant.tenant_id == tenant_id)
        .all()
    )
    assert len(grants) == 1
    assert grants[0].source == "auto_signup"
    assert grants[0].granted_tier == "professional"
