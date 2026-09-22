"""Tests for the account-deletion scheduled purge ARQ worker.

Covers the four scenarios from task-5-brief.md: a due grace request gets
purged (right purge method per scope, then status -> purged); a not-yet-due
grace request is left untouched; an already-purged request is skipped; and a
per-item failure doesn't abort the rest of the batch.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import DeletionStatus, User, UserRole


class _NoCloseSession:
    """Thin proxy around a SQLAlchemy Session that swallows close() calls."""

    def __init__(self, session):
        self._session = session

    def close(self):
        pass  # do not close the shared test session

    def __getattr__(self, name):
        return getattr(self._session, name)


def _make_tenant(db_session) -> Tenant:
    unique = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
        status=TenantStatus.ACTIVE,
        is_publicly_listed=True,
    )
    db_session.add(tenant)
    db_session.flush()
    return tenant


def _make_user(db_session, tenant: Tenant, *, role: UserRole = UserRole.SUPPORT_AGENT) -> User:
    unique = uuid4().hex[:8]
    user = User(
        email=f"user_{unique}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"Test User {unique}",
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        deletion_status=DeletionStatus.ACTIVE,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)
    return user


def _make_request(
    db_session,
    user: User,
    *,
    scope: DeletionScope = DeletionScope.SELF_USER,
    status: DeletionStatus = DeletionStatus.GRACE,
    scheduled_purge_at: datetime | None,
) -> DeletionRequest:
    request = DeletionRequest(
        user_id=user.id,
        tenant_id=user.tenant_id,
        scope=scope,
        status=status,
        requested_via=DeletionRequestedVia.AUTHED,
        cancel_token_hash=uuid4().hex,
        scheduled_purge_at=scheduled_purge_at,
    )
    db_session.add(request)
    db_session.flush()
    return request


def _patch_session_local(monkeypatch, db_session):
    import app.workers.account_deletion_purge as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _NoCloseSession(db_session))


async def test_due_self_user_request_is_purged(db_session, monkeypatch):
    """A due (grace, scheduled_purge_at <= now) self_user request gets purged."""
    from app.workers.account_deletion_purge import purge_due_deletion_requests

    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant)
    request = _make_request(
        db_session,
        user,
        scope=DeletionScope.SELF_USER,
        scheduled_purge_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    _patch_session_local(monkeypatch, db_session)

    result = await purge_due_deletion_requests({})

    db_session.refresh(user)
    db_session.refresh(request)
    assert result["success"] is True
    assert result["purged_count"] == 1
    assert user.deletion_status == DeletionStatus.PURGED
    assert user.full_name == "Deleted user"
    assert request.status == DeletionStatus.PURGED


async def test_due_tenant_request_is_purged(db_session, monkeypatch):
    """A due (grace, scheduled_purge_at <= now) tenant request purges the whole tenant."""
    from app.workers.account_deletion_purge import purge_due_deletion_requests

    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    request = _make_request(
        db_session,
        admin,
        scope=DeletionScope.TENANT,
        scheduled_purge_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    _patch_session_local(monkeypatch, db_session)

    result = await purge_due_deletion_requests({})

    db_session.refresh(admin)
    db_session.refresh(request)
    db_session.refresh(tenant)
    assert result["success"] is True
    assert result["purged_count"] == 1
    assert admin.deletion_status == DeletionStatus.PURGED
    assert tenant.status == TenantStatus.INACTIVE
    assert request.status == DeletionStatus.PURGED


async def test_not_yet_due_grace_request_is_untouched(db_session, monkeypatch):
    """A grace request whose scheduled_purge_at is still in the future is left alone."""
    from app.workers.account_deletion_purge import purge_due_deletion_requests

    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant)
    request = _make_request(
        db_session,
        user,
        scope=DeletionScope.SELF_USER,
        scheduled_purge_at=datetime.now(UTC) + timedelta(days=5),
    )

    _patch_session_local(monkeypatch, db_session)

    result = await purge_due_deletion_requests({})

    db_session.refresh(user)
    db_session.refresh(request)
    assert result["purged_count"] == 0
    assert user.deletion_status == DeletionStatus.ACTIVE
    assert request.status == DeletionStatus.GRACE


async def test_already_purged_request_is_skipped(db_session, monkeypatch):
    """An already-purged request is not re-processed, even if scheduled_purge_at is due."""
    from app.workers.account_deletion_purge import purge_due_deletion_requests

    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant)
    user.deletion_status = DeletionStatus.PURGED
    db_session.flush()
    request = _make_request(
        db_session,
        user,
        scope=DeletionScope.SELF_USER,
        status=DeletionStatus.PURGED,
        scheduled_purge_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    _patch_session_local(monkeypatch, db_session)

    calls = []
    import app.workers.account_deletion_purge as worker_mod
    from app.services.account_purge_service import AccountPurgeService

    original_purge_user = AccountPurgeService.purge_user

    def spy_purge_user(self, user_id):
        calls.append(user_id)
        return original_purge_user(self, user_id)

    monkeypatch.setattr(worker_mod.AccountPurgeService, "purge_user", spy_purge_user)

    result = await purge_due_deletion_requests({})

    db_session.refresh(request)
    assert result["purged_count"] == 0
    assert calls == []
    assert request.status == DeletionStatus.PURGED


async def test_one_failure_does_not_abort_the_batch(db_session, monkeypatch):
    """A per-item failure is logged and skipped; other due requests still get purged."""
    from app.workers.account_deletion_purge import purge_due_deletion_requests

    tenant = _make_tenant(db_session)
    failing_user = _make_user(db_session, tenant)
    failing_request = _make_request(
        db_session,
        failing_user,
        scope=DeletionScope.SELF_USER,
        scheduled_purge_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    healthy_user = _make_user(db_session, tenant)
    healthy_request = _make_request(
        db_session,
        healthy_user,
        scope=DeletionScope.SELF_USER,
        scheduled_purge_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.commit()

    _patch_session_local(monkeypatch, db_session)

    import app.workers.account_deletion_purge as worker_mod
    from app.services.account_purge_service import AccountPurgeService

    original_purge_user = AccountPurgeService.purge_user

    def flaky_purge_user(self, user_id):
        if user_id == failing_user.id:
            raise RuntimeError("boom")
        return original_purge_user(self, user_id)

    monkeypatch.setattr(worker_mod.AccountPurgeService, "purge_user", flaky_purge_user)

    result = await purge_due_deletion_requests({})

    db_session.refresh(failing_request)
    db_session.refresh(healthy_user)
    db_session.refresh(healthy_request)
    assert result["success"] is True
    assert result["purged_count"] == 1
    assert result["failed_count"] == 1
    assert failing_request.status == DeletionStatus.GRACE
    assert healthy_user.deletion_status == DeletionStatus.PURGED
    assert healthy_request.status == DeletionStatus.PURGED
