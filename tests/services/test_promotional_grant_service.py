from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.subscription import SubscriptionHistory
from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.promotional_grant_service import (
    PromotionalGrantService,
    PromoGrantNotFoundError,
    PromoGrantStateError,
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
        reason="setup for revoke test",
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
        reason="setup for revoke test",
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
            reason="not found in db",
            admin_id=super_admin_user.id,
            admin_name=super_admin_user.email,
        )


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
    _grant = service.create_grant(
        tenant_id=tenant.id,
        campaign_code="WINBACK_TEST",
        campaign_name=None,
        granted_tier="professional",
        duration_days=90,
        source="admin_single",
        granted_by_admin_id=super_admin_user.id,
        granted_by_admin_name=super_admin_user.email,
        reason="setup for idempotency test",
    )
    _grant.grant_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    first = service.expire_grants_due()
    second = service.expire_grants_due()
    assert len(first) == 1
    assert second == []


def test_mark_converted_blocks_future_revert(db_session, super_admin_user):
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
        reason="setup for conversion test",
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
        reason="bulk run 1 setup",
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
        reason="bulk run 2 repeat",
        force_replace_active=False,
    )
    assert res2["granted"] == 0
    assert res2["skipped_already_granted"] == 2
