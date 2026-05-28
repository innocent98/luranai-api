"""Smoke tests for the three promotional-grant email notification methods."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant, TenantCapabilities
from app.db.models.user import User, UserRole
from app.services.email_notification_service import EmailNotificationService


def _make_tenant_with_admin(db_session):
    """Create a tenant + active TENANT_ADMIN user and return (tenant, admin)."""
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Promo Test Co",
        api_key_hash="dummy",
        subscription_tier="professional",
        subscription_status="active",
    )
    db_session.add(tenant)
    db_session.flush()
    db_session.refresh(tenant)

    caps = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(caps)
    db_session.flush()

    admin = User(
        tenant_id=tenant.id,
        email=f"admin-{uuid4().hex[:6]}@example.com",
        full_name="Test Admin",
        role=UserRole.TENANT_ADMIN,
        is_active=True,
        hashed_password="x",
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    db_session.refresh(tenant)
    return tenant, admin


def _make_grant(db_session, tenant_id, *, status="active", days_until_end=7):
    grant = PromotionalGrant(
        tenant_id=tenant_id,
        campaign_code="LAUNCH_2026",
        campaign_name="Launch 2026",
        granted_tier="professional",
        duration_days=90,
        grant_starts_at=datetime.now(UTC) - timedelta(days=90 - days_until_end),
        grant_ends_at=datetime.now(UTC) + timedelta(days=days_until_end),
        previous_tier="trial",
        previous_status="trial_expired",
        status=status,
        source="admin_bulk",
        reason="test",
    )
    db_session.add(grant)
    db_session.commit()
    db_session.refresh(grant)
    return grant


@pytest.mark.asyncio
async def test_send_promo_expiration_reminder_returns_true_on_success(db_session, monkeypatch):
    tenant, _admin = _make_tenant_with_admin(db_session)
    grant = _make_grant(db_session, tenant.id, days_until_end=7)

    monkeypatch.setattr(
        "app.services.email_service.EmailService._send_email",
        lambda self, **kw: True,
    )

    service = EmailNotificationService(db_session)
    result = await service.send_promo_expiration_reminder(grant_id=grant.id, days_remaining=7)
    assert result is True


@pytest.mark.asyncio
async def test_send_promo_expiration_reminder_inactive_grant_returns_false(db_session, monkeypatch):
    tenant, _admin = _make_tenant_with_admin(db_session)
    grant = _make_grant(db_session, tenant.id, status="expired")

    service = EmailNotificationService(db_session)
    result = await service.send_promo_expiration_reminder(grant_id=grant.id, days_remaining=7)
    assert result is False


@pytest.mark.asyncio
async def test_send_promo_expired_email_returns_true_on_success(db_session, monkeypatch):
    tenant, _admin = _make_tenant_with_admin(db_session)
    # send_promo_expired_email does NOT check grant.status — any grant qualifies
    grant = _make_grant(db_session, tenant.id, status="expired", days_until_end=-1)

    monkeypatch.setattr(
        "app.services.email_service.EmailService._send_email",
        lambda self, **kw: True,
    )

    service = EmailNotificationService(db_session)
    result = await service.send_promo_expired_email(grant_id=grant.id)
    assert result is True


@pytest.mark.asyncio
async def test_send_promo_granted_email_returns_true_on_success(db_session, monkeypatch):
    tenant, _admin = _make_tenant_with_admin(db_session)
    grant = _make_grant(db_session, tenant.id)

    monkeypatch.setattr(
        "app.services.email_service.EmailService._send_email",
        lambda self, **kw: True,
    )

    service = EmailNotificationService(db_session)
    result = await service.send_promo_granted_email(grant_id=grant.id)
    assert result is True


@pytest.mark.asyncio
async def test_send_promo_granted_email_inactive_grant_returns_false(db_session, monkeypatch):
    tenant, _admin = _make_tenant_with_admin(db_session)
    grant = _make_grant(db_session, tenant.id, status="expired")

    service = EmailNotificationService(db_session)
    result = await service.send_promo_granted_email(grant_id=grant.id)
    assert result is False
