"""Tests for the promotional grant lifecycle ARQ worker."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4


from app.db.models.promotional_grant import PromotionalGrant
from app.db.models.tenant import Tenant, TenantCapabilities


class _NoCloseSession:
    """Thin proxy around a SQLAlchemy Session that swallows close() calls."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


def _make_active_grant(db_session, *, days_until_end: int):
    """Create Tenant + TenantCapabilities + active PromotionalGrant returning the grant."""
    tenant = Tenant(
        name=f"t-{uuid4().hex[:8]}",
        display_name="Lifecycle Test Co",
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

    grant = PromotionalGrant(
        tenant_id=tenant.id,
        campaign_code="TEST_LIFECYCLE",
        campaign_name="Lifecycle Test Campaign",
        granted_tier="professional",
        duration_days=90,
        grant_starts_at=datetime.now(UTC) - timedelta(days=90 - days_until_end),
        grant_ends_at=datetime.now(UTC) + timedelta(days=days_until_end),
        previous_tier="trial",
        previous_status="trial_expired",
        status="active",
        source="admin_single",
        reason="lifecycle worker test",
    )
    db_session.add(grant)
    db_session.commit()
    db_session.refresh(grant)
    return grant


async def test_worker_sends_reminders_at_t14(db_session, monkeypatch):
    """Worker sends a reminder when grant ends in exactly 14 days."""
    from app.services.email_notification_service import EmailNotificationService
    from app.workers.document_processor import process_promotional_grant_lifecycle

    grant = _make_active_grant(db_session, days_until_end=14)

    sent = []

    async def fake_reminder(self, *, grant_id, days_remaining):
        sent.append((grant_id, days_remaining))
        return True

    async def fake_expired(self, *, grant_id):
        return True

    monkeypatch.setattr(EmailNotificationService, "send_promo_expiration_reminder", fake_reminder)
    monkeypatch.setattr(EmailNotificationService, "send_promo_expired_email", fake_expired)

    # Patch SessionLocal to use the test db_session
    import app.workers.document_processor as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    result = await process_promotional_grant_lifecycle({})

    assert (grant.id, 14) in sent
    assert result["reminders_sent"] >= 1


async def test_worker_skips_reminders_outside_window(db_session, monkeypatch):
    """Worker does NOT send reminders for grants expiring on a non-reminder day (10 days)."""
    from app.services.email_notification_service import EmailNotificationService
    from app.workers.document_processor import process_promotional_grant_lifecycle

    grant = _make_active_grant(db_session, days_until_end=10)

    sent = []

    async def fake_reminder(self, *, grant_id, days_remaining):
        sent.append((grant_id, days_remaining))
        return True

    async def fake_expired(self, *, grant_id):
        return True

    monkeypatch.setattr(EmailNotificationService, "send_promo_expiration_reminder", fake_reminder)
    monkeypatch.setattr(EmailNotificationService, "send_promo_expired_email", fake_expired)

    import app.workers.document_processor as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    await process_promotional_grant_lifecycle({})

    sent_ids = [g_id for (g_id, _) in sent]
    assert grant.id not in sent_ids


async def test_worker_expires_past_due_grants(db_session, monkeypatch):
    """Worker expires grants past their end date and reverts tenant to trial/trial_expired."""
    from app.services.email_notification_service import EmailNotificationService
    from app.workers.document_processor import process_promotional_grant_lifecycle

    # Create a grant that ended in the past
    grant = _make_active_grant(db_session, days_until_end=-1)

    monkeypatch.setattr(
        EmailNotificationService,
        "send_promo_expiration_reminder",
        lambda self, **kw: True,
    )
    monkeypatch.setattr(
        EmailNotificationService,
        "send_promo_expired_email",
        lambda self, **kw: True,
    )

    import app.workers.document_processor as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _NoCloseSession(db_session))

    result = await process_promotional_grant_lifecycle({})

    db_session.refresh(grant)
    assert grant.status == "expired"

    from app.db.models.tenant import Tenant

    tenant = db_session.get(Tenant, grant.tenant_id)
    assert tenant.subscription_tier == "trial"
    assert tenant.subscription_status == "trial_expired"

    assert result["expired"] >= 1
