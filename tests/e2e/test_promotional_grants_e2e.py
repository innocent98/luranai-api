"""End-to-end lifecycle test: grant → reminder → expiry (Task 19)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db.models.tenant import Tenant, TenantCapabilities
from app.services.promotional_grant_service import PromotionalGrantService


class _NoCloseSession:
    """Thin proxy around a SQLAlchemy Session that swallows close() calls."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.mark.asyncio
async def test_full_lifecycle_grant_to_expiry(db_session, monkeypatch, super_admin_user):
    """
    1. Create churned tenant + capabilities.
    2. Bulk-grant via service (mirrors the API path).
    3. Move grant_ends_at to T-1, run worker, assert reminder triggered.
    4. Move grant_ends_at past T+0, run worker, assert tenant reverted.
    """
    from app.services.email_notification_service import EmailNotificationService
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
    db_session.flush()
    db_session.refresh(tenant)

    capabilities = TenantCapabilities(tenant_id=tenant.id)
    db_session.add(capabilities)
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
        reason="e2e test setup with sufficient length",
    )
    db_session.refresh(tenant)
    assert tenant.subscription_tier == "professional"
    assert tenant.subscription_status == "active"

    sent_reminders: list[tuple] = []

    async def fake_reminder(self, *, grant_id, days_remaining):
        sent_reminders.append((grant_id, days_remaining))
        return True

    async def fake_expired(self, *, grant_id):
        return True

    monkeypatch.setattr(EmailNotificationService, "send_promo_expiration_reminder", fake_reminder)
    monkeypatch.setattr(EmailNotificationService, "send_promo_expired_email", fake_expired)

    # Patch SessionLocal to use the test db_session
    import app.workers.document_processor as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    # Move grant_ends_at to be exactly 1 day from now (in T-1 reminder window)
    grant.grant_ends_at = datetime.now(UTC) + timedelta(days=1)
    db_session.commit()

    await process_promotional_grant_lifecycle({})
    assert (
        grant.id,
        1,
    ) in sent_reminders, f"Expected (grant.id, 1) in reminders, got {sent_reminders}"

    # Now move grant_ends_at to the past
    db_session.refresh(grant)
    grant.grant_ends_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    await process_promotional_grant_lifecycle({})

    db_session.refresh(grant)
    db_session.refresh(tenant)
    assert grant.status == "expired", f"Expected expired, got {grant.status}"
    assert tenant.subscription_tier == "trial"
    assert tenant.subscription_status == "trial_expired"
