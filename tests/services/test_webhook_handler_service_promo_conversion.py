"""Tests for promotional grant conversion detection in webhook handler."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4


from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.promotional_grant_service import PromotionalGrantService


def _make_tenant_with_active_grant(db_session, super_admin_user):
    """Create a churned tenant + an active promotional grant on it."""
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Test",
        api_key_hash="dummy",
        subscription_tier="trial",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.refresh(tenant)

    capabilities = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(capabilities)
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
        reason="webhook conversion test setup",
    )
    return tenant, grant


def test_payment_success_marks_active_grant_converted(db_session, super_admin_user):
    """mark_converted_for_tenant sets status=converted and records converted_at."""
    tenant, grant = _make_tenant_with_active_grant(db_session, super_admin_user)

    promo = PromotionalGrantService(db_session)
    converted = promo.mark_converted_for_tenant(
        tenant_id=tenant.id,
        paid_tier="professional",
        paid_billing_cycle="monthly",
    )

    db_session.refresh(grant)
    assert converted is not None
    assert grant.status == "converted"
    assert grant.converted_at is not None


def test_payment_success_no_grant_is_noop(db_session, super_admin_user):
    """mark_converted_for_tenant returns None when tenant has no active grant."""
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="NoGrant",
        api_key_hash="dummy2",
        subscription_tier="trial",
        subscription_status="trial_expired",
        trial_ends_at=datetime.now(UTC) - timedelta(days=5),
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.refresh(tenant)
    capabilities = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(capabilities)
    db_session.commit()

    promo = PromotionalGrantService(db_session)
    result = promo.mark_converted_for_tenant(
        tenant_id=tenant.id,
        paid_tier="lite",
        paid_billing_cycle="monthly",
    )
    assert result is None


def test_webhook_handler_calls_mark_converted(db_session, super_admin_user):
    """Verify _handle_charge_success wires promo conversion after upgrade."""
    # This test verifies the hook is wired by inspecting the source of
    # _handle_charge_success for the conversion call.
    import inspect
    from app.services.webhook_handler_service import WebhookHandlerService

    source = inspect.getsource(WebhookHandlerService._handle_charge_success)
    assert "mark_converted_for_tenant" in source, (
        "_handle_charge_success must call mark_converted_for_tenant "
        "after upgrading the tenant subscription."
    )
    assert (
        "PromotionalGrantService" in source
    ), "_handle_charge_success must import/use PromotionalGrantService."
