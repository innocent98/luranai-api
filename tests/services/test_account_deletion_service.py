"""Tests for AccountDeletionService: request / verify / cancel + last-admin guard.

Covers the working-agreement scenarios from task-2-brief.md: the last-admin
guard (block + allow-with-co-admin + support-agent bypass), the public
non-leaking request path, verify (valid/invalid/expired), cancel (grace
restore / already-purged), the duplicate-open-request guard, and session
revocation (both persisted RefreshToken rows and the live ``revoked:{jti}``
Redis mechanism).

No purge logic is exercised here — Task 3 (AccountPurgeService) owns that.
Where a "purged" state is needed, the test constructs it directly.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.api.deps import is_token_revoked
from app.core.security import create_access_token, create_refresh_token
from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
from app.db.models.refresh_token import RefreshToken
from app.db.models.tenant import Tenant
from app.db.models.user import DeletionStatus, User, UserRole
from app.services.account_deletion_service import (
    AccountDeletionService,
    AlreadyPurgedError,
    InvalidOrExpiredTokenError,
    LastAdminError,
    NotTenantAdminError,
)
from app.utils.datetime import utcnow_aware

pytestmark = pytest.mark.usefixtures("db_session")


def _make_tenant(db_session) -> Tenant:
    unique = uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()
    return tenant


def _make_user(
    db_session,
    tenant: Tenant,
    *,
    role: UserRole = UserRole.TENANT_ADMIN,
    deletion_status: DeletionStatus = DeletionStatus.ACTIVE,
) -> User:
    unique = uuid4().hex[:8]
    user = User(
        email=f"user_{unique}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"Test User {unique}",
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        deletion_status=deletion_status,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)
    return user


def _make_refresh_token(db_session, user: User) -> RefreshToken:
    _, token_hash = create_refresh_token()
    record = RefreshToken(
        user_id=user.id,
        user_type="tenant_user",
        token_hash=token_hash,
        expires_at=utcnow_aware() + timedelta(days=7),
        created_at=utcnow_aware(),
    )
    db_session.add(record)
    db_session.flush()
    return record


# ---------------------------------------------------------------------------
# request_self — last-admin guard
# ---------------------------------------------------------------------------


def test_request_self_sole_admin_raises_last_admin_error_no_request_created(db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    service = AccountDeletionService(db_session)

    with pytest.raises(LastAdminError) as exc_info:
        service.request_self(admin)

    assert exc_info.value.tenant_id == tenant.id
    assert exc_info.value.options == ["transfer_ownership", "delete_tenant"]

    assert db_session.query(DeletionRequest).filter_by(user_id=admin.id).count() == 0
    db_session.refresh(admin)
    assert admin.deletion_status == DeletionStatus.ACTIVE


def test_request_self_admin_with_coadmin_creates_grace_request_and_revokes_sessions(db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)  # co-admin
    refresh_record = _make_refresh_token(db_session, admin)

    access_token = create_access_token(subject=str(admin.id))
    from jose import jwt as jose_jwt

    from app.core.config import settings

    jti = jose_jwt.decode(access_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])["jti"]

    service = AccountDeletionService(db_session)

    request = service.request_self(admin, current_jti=jti)

    assert request.scope == DeletionScope.SELF_USER
    assert request.status == DeletionStatus.GRACE
    assert request.requested_via == DeletionRequestedVia.AUTHED
    assert request.scheduled_purge_at is not None
    assert request.cancel_token_hash is not None
    assert request.raw_cancel_token  # transient, attached for the caller to email

    db_session.refresh(admin)
    assert admin.deletion_status == DeletionStatus.GRACE
    assert admin.deletion_requested_at is not None

    # Persisted session (refresh token) revoked.
    db_session.refresh(refresh_record)
    assert refresh_record.revoked_at is not None

    # Live access token's jti blacklisted via the existing revoked:{jti} mechanism.
    assert is_token_revoked(jti) is True


def test_request_self_support_agent_creates_grace_request(db_session):
    tenant = _make_tenant(db_session)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)

    request = service.request_self(agent)

    assert request.status == DeletionStatus.GRACE
    db_session.refresh(agent)
    assert agent.deletion_status == DeletionStatus.GRACE


def test_request_self_is_idempotent_reuses_open_grace_request(db_session):
    tenant = _make_tenant(db_session)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)

    first = service.request_self(agent)
    second = service.request_self(agent)

    assert first.id == second.id
    assert db_session.query(DeletionRequest).filter_by(user_id=agent.id).count() == 1
    # A fresh cancel token is issued on every call, so callers always have
    # something actionable to (re-)email.
    assert second.raw_cancel_token


# ---------------------------------------------------------------------------
# request_public
# ---------------------------------------------------------------------------


def test_request_public_existing_email_creates_pending_verification_and_token(db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)

    raw_token = service.request_public(user.email)

    assert raw_token is not None
    request = db_session.query(DeletionRequest).filter_by(user_id=user.id).one()
    assert request.status == DeletionStatus.PENDING_VERIFICATION
    assert request.requested_via == DeletionRequestedVia.PUBLIC
    assert request.verification_token_hash is not None
    # Account not yet disabled — public path disables at verify(), not request.
    db_session.refresh(user)
    assert user.deletion_status == DeletionStatus.ACTIVE


def test_request_public_nonexistent_email_creates_no_request_and_returns_none(db_session):
    service = AccountDeletionService(db_session)

    result = service.request_public(f"nobody-{uuid4().hex[:8]}@example.com")

    assert result is None
    assert db_session.query(DeletionRequest).count() == 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_valid_token_moves_to_grace_and_disables_account(db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)
    raw_token = service.request_public(user.email)

    request = service.verify(raw_token)

    assert request.status == DeletionStatus.GRACE
    assert request.scheduled_purge_at is not None
    assert request.verification_token_hash is None  # single-use
    assert request.raw_cancel_token

    db_session.refresh(user)
    assert user.deletion_status == DeletionStatus.GRACE
    assert user.deletion_requested_at is not None


def test_verify_invalid_token_raises(db_session):
    service = AccountDeletionService(db_session)

    with pytest.raises(InvalidOrExpiredTokenError):
        service.verify("not-a-real-token")


def test_verify_expired_token_raises(db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)
    raw_token = service.request_public(user.email)

    request = db_session.query(DeletionRequest).filter_by(user_id=user.id).one()
    # Backdate creation past the verification-token TTL.
    request.created_at = utcnow_aware() - timedelta(hours=48)
    db_session.flush()

    with pytest.raises(InvalidOrExpiredTokenError):
        service.verify(raw_token)


def test_verify_reapplies_last_admin_guard(db_session):
    """Tenant composition can change between request and verify (e.g. the
    co-admin who justified the request left in the meantime)."""
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    service = AccountDeletionService(db_session)
    # Force a public pending_verification row directly (bypassing the
    # request_self last-admin guard, to simulate the tenant composition
    # changing after the request was created).
    _, token_hash = create_refresh_token()
    request = DeletionRequest(
        user_id=admin.id,
        tenant_id=tenant.id,
        scope=DeletionScope.SELF_USER,
        status=DeletionStatus.PENDING_VERIFICATION,
        requested_via=DeletionRequestedVia.PUBLIC,
        verification_token_hash=token_hash,
    )
    db_session.add(request)
    db_session.flush()
    raw_token, request.verification_token_hash = create_refresh_token()
    db_session.flush()

    with pytest.raises(LastAdminError):
        service.verify(raw_token)

    db_session.refresh(request)
    assert request.status == DeletionStatus.PENDING_VERIFICATION  # untouched


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_in_grace_restores_active(db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)
    raw_token = service.request_public(user.email)
    request = service.verify(raw_token)
    raw_cancel_token = request.raw_cancel_token

    service.cancel(raw_cancel_token)

    db_session.refresh(request)
    assert request.status == DeletionStatus.ACTIVE
    assert request.scheduled_purge_at is None
    assert request.cancel_token_hash is None

    db_session.refresh(user)
    assert user.deletion_status == DeletionStatus.ACTIVE
    assert user.deletion_requested_at is None


def test_cancel_by_user_in_grace_restores_active(db_session):
    tenant = _make_tenant(db_session)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)
    service.request_self(agent)

    service.cancel(agent)

    db_session.refresh(agent)
    assert agent.deletion_status == DeletionStatus.ACTIVE


def test_cancel_after_purged_raises(db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(
        db_session, tenant, role=UserRole.SUPPORT_AGENT, deletion_status=DeletionStatus.PURGED
    )
    _, token_hash = create_refresh_token()
    request = DeletionRequest(
        user_id=user.id,
        tenant_id=tenant.id,
        scope=DeletionScope.SELF_USER,
        status=DeletionStatus.PURGED,
        requested_via=DeletionRequestedVia.PUBLIC,
        cancel_token_hash=token_hash,
    )
    db_session.add(request)
    db_session.flush()
    service = AccountDeletionService(db_session)

    raw_token, request.cancel_token_hash = create_refresh_token()
    db_session.flush()

    with pytest.raises(AlreadyPurgedError):
        service.cancel(raw_token)


def test_cancel_invalid_token_raises(db_session):
    service = AccountDeletionService(db_session)

    with pytest.raises(InvalidOrExpiredTokenError):
        service.cancel("not-a-real-token")


# ---------------------------------------------------------------------------
# request_tenant
# ---------------------------------------------------------------------------


def test_request_tenant_creates_tenant_scoped_grace_request(db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    service = AccountDeletionService(db_session)

    request = service.request_tenant(admin)

    assert request.scope == DeletionScope.TENANT
    assert request.status == DeletionStatus.GRACE
    assert request.tenant_id == tenant.id
    db_session.refresh(admin)
    assert admin.deletion_status == DeletionStatus.GRACE


def test_request_tenant_non_admin_raises(db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    service = AccountDeletionService(db_session)

    with pytest.raises(NotTenantAdminError):
        service.request_tenant(agent)

    assert db_session.query(DeletionRequest).filter_by(user_id=agent.id).count() == 0
