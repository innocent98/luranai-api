from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.promotional_grant_service import (
    PromotionalGrantService,
    PromoGrantValidationError,
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
    db_session.flush()
    db_session.refresh(tenant)

    capabilities = TenantCapabilities(
        tenant_id=tenant.id,
    )
    db_session.add(capabilities)
    db_session.commit()
    db_session.refresh(tenant)
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
            reason="duplicate grant attempt",
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
    db_session.flush()
    db_session.refresh(tenant)

    capabilities = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(capabilities)
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
