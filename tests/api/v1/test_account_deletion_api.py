"""Tests for the account-deletion request/verify/cancel/tenant endpoints (Task 4).

Covers the working-agreement scenarios: authed self-serve request (200 +
grace + confirmation email), sole-admin -> 409 last_admin, public
existing-vs-nonexistent email -> byte-identical 200 body (email sent only
when it exists), the public endpoint's rate limit, verify/cancel happy +
error paths, tenant deletion (admin + confirm required, blacklists the
caller's own jti), and the login gate (grace/pending users hard-blocked at
login AND on `get_current_user`, normal active users unaffected).

Uses `pg_client`/`db_session` (real Postgres, savepoint-rolled-back per
test) — same infra as `tests/services/test_account_deletion_service.py`,
since this exercises the same enum-backed `DeletionRequest`/`User` rows.
"""

import uuid
from unittest.mock import patch

import pytest

from app.core.security import create_access_token, get_password_hash
from app.db.models.deletion_request import DeletionRequest
from app.db.models.tenant import Tenant
from app.db.models.user import DeletionStatus, User, UserRole

pytestmark = pytest.mark.usefixtures("db_session")

_PASSWORD = "correct horse battery staple 1"

_VERIFY_EMAIL_PATCH = (
    "app.api.v1.endpoints.account_deletion.email_service.send_account_deletion_verification_email"
)
_CONFIRM_EMAIL_PATCH = (
    "app.api.v1.endpoints.account_deletion.email_service.send_account_deletion_confirmation_email"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_tenant(db_session) -> Tenant:
    unique = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{unique}",
        display_name=f"Tenant {unique}",
        api_key_hash=uuid.uuid4().hex,
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
    is_active: bool = True,
    password: str = _PASSWORD,
) -> User:
    unique = uuid.uuid4().hex[:8]
    user = User(
        email=f"user_{unique}@example.com",
        hashed_password=get_password_hash(password),
        full_name=f"Test User {unique}",
        role=role,
        tenant_id=tenant.id,
        is_active=is_active,
        deletion_status=deletion_status,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)
    return user


def _token_for(user: User) -> str:
    return create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )


# ---------------------------------------------------------------------------
# Authed self-serve request
# ---------------------------------------------------------------------------


def test_authed_self_serve_request_returns_grace_and_sends_confirmation_email(
    pg_client, db_session
):
    tenant = _make_tenant(db_session)
    _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)  # co-admin
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()
    token = _token_for(agent)

    with patch(_CONFIRM_EMAIL_PATCH, return_value=True) as mock_email:
        resp = pg_client.post("/api/v1/account/deletion-request", headers=_auth(token))

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "grace"
    assert "scheduled_purge_at" in body

    mock_email.assert_called_once()
    call_kwargs = mock_email.call_args.kwargs
    assert call_kwargs["email"] == agent.email
    assert call_kwargs["cancel_token"]

    db_session.refresh(agent)
    assert agent.deletion_status == DeletionStatus.GRACE


def test_authed_self_serve_sole_admin_returns_409_last_admin(pg_client, db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    db_session.commit()
    token = _token_for(admin)

    resp = pg_client.post("/api/v1/account/deletion-request", headers=_auth(token))

    assert resp.status_code == 409, resp.json()
    detail = resp.json()["detail"]
    assert detail["code"] == "last_admin"
    assert detail["options"] == ["transfer_ownership", "delete_tenant"]

    db_session.refresh(admin)
    assert admin.deletion_status == DeletionStatus.ACTIVE


def test_authed_self_serve_request_requires_valid_token(pg_client):
    resp = pg_client.post("/api/v1/account/deletion-request", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Public request — anti-enumeration
# ---------------------------------------------------------------------------


def test_public_request_existing_and_nonexistent_email_return_byte_identical_body(
    pg_client, db_session
):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()

    with patch(_VERIFY_EMAIL_PATCH, return_value=True) as mock_email:
        resp_existing = pg_client.post(
            "/api/v1/account/deletion-request", json={"email": user.email}
        )
    with patch(_VERIFY_EMAIL_PATCH, return_value=True) as mock_email_absent:
        resp_absent = pg_client.post(
            "/api/v1/account/deletion-request",
            json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com"},
        )

    assert resp_existing.status_code == 200
    assert resp_absent.status_code == 200
    assert resp_existing.content == resp_absent.content
    assert resp_existing.json() == {
        "message": "If an account exists, a verification email has been sent."
    }

    mock_email.assert_called_once()
    mock_email_absent.assert_not_called()


def test_public_request_no_bearer_no_email_is_a_client_error(pg_client):
    resp = pg_client.post("/api/v1/account/deletion-request", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Public request — rate limit
# ---------------------------------------------------------------------------


def test_public_request_is_rate_limited(pg_client, db_session, monkeypatch):
    from app.utils.http_rate_limiter import account_deletion_public_rate_limiter

    monkeypatch.setattr(account_deletion_public_rate_limiter, "max_calls", 2)
    monkeypatch.setattr(account_deletion_public_rate_limiter, "_redis", None)

    with patch(_VERIFY_EMAIL_PATCH, return_value=True):
        resp1 = pg_client.post(
            "/api/v1/account/deletion-request",
            json={"email": f"rl-{uuid.uuid4().hex[:8]}@example.com"},
        )
        resp2 = pg_client.post(
            "/api/v1/account/deletion-request",
            json={"email": f"rl-{uuid.uuid4().hex[:8]}@example.com"},
        )
        resp3 = pg_client.post(
            "/api/v1/account/deletion-request",
            json={"email": f"rl-{uuid.uuid4().hex[:8]}@example.com"},
        )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 429


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_valid_token_returns_grace_and_sends_cancel_email(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()

    from app.services.account_deletion_service import AccountDeletionService

    service = AccountDeletionService(db_session)
    raw_token = service.request_public(user.email)
    db_session.commit()

    with patch(_CONFIRM_EMAIL_PATCH, return_value=True) as mock_email:
        resp = pg_client.post("/api/v1/account/deletion-request/verify", json={"token": raw_token})

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "grace"
    mock_email.assert_called_once()

    db_session.refresh(user)
    assert user.deletion_status == DeletionStatus.GRACE


def test_verify_invalid_token_returns_generic_400(pg_client):
    resp = pg_client.post(
        "/api/v1/account/deletion-request/verify", json={"token": "not-a-real-token"}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_or_expired_token"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_with_token_restores_active(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()

    from app.services.account_deletion_service import AccountDeletionService

    service = AccountDeletionService(db_session)
    raw_verify_token = service.request_public(user.email)
    db_session.commit()
    deletion_request = service.verify(raw_verify_token)
    raw_cancel_token = deletion_request.raw_cancel_token
    db_session.commit()

    resp = pg_client.post(
        "/api/v1/account/deletion-request/cancel", json={"token": raw_cancel_token}
    )

    assert resp.status_code == 200, resp.json()
    db_session.refresh(user)
    assert user.deletion_status == DeletionStatus.ACTIVE


def test_cancel_invalid_token_returns_400(pg_client):
    resp = pg_client.post(
        "/api/v1/account/deletion-request/cancel", json={"token": "not-a-real-token"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_or_expired_token"


def test_cancel_already_purged_returns_409(pg_client, db_session):
    from app.core.security import create_refresh_token
    from app.db.models.deletion_request import DeletionRequestedVia, DeletionScope

    tenant = _make_tenant(db_session)
    user = _make_user(
        db_session, tenant, role=UserRole.SUPPORT_AGENT, deletion_status=DeletionStatus.PURGED
    )
    raw_token, token_hash = create_refresh_token()
    request = DeletionRequest(
        user_id=user.id,
        tenant_id=tenant.id,
        scope=DeletionScope.SELF_USER,
        status=DeletionStatus.PURGED,
        requested_via=DeletionRequestedVia.PUBLIC,
        cancel_token_hash=token_hash,
    )
    db_session.add(request)
    db_session.commit()

    resp = pg_client.post("/api/v1/account/deletion-request/cancel", json={"token": raw_token})

    assert resp.status_code == 409, resp.json()
    assert resp.json()["detail"]["code"] == "already_purged"


# ---------------------------------------------------------------------------
# Tenant deletion
# ---------------------------------------------------------------------------


def test_tenant_deletion_requires_admin(pg_client, db_session):
    tenant = _make_tenant(db_session)
    agent = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()
    token = _token_for(agent)

    resp = pg_client.post(
        "/api/v1/account/deletion-request/tenant",
        json={"confirm": True},
        headers=_auth(token),
    )

    assert resp.status_code == 403


def test_tenant_deletion_requires_explicit_confirm(pg_client, db_session):
    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    db_session.commit()
    token = _token_for(admin)

    resp = pg_client.post(
        "/api/v1/account/deletion-request/tenant",
        json={"confirm": False},
        headers=_auth(token),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "confirmation_required"


def test_tenant_deletion_succeeds_and_blacklists_caller_jti(pg_client, db_session):
    from jose import jwt as jose_jwt

    from app.api.deps import is_token_revoked
    from app.core.config import settings

    tenant = _make_tenant(db_session)
    admin = _make_user(db_session, tenant, role=UserRole.TENANT_ADMIN)
    db_session.commit()
    token = _token_for(admin)
    jti = jose_jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])["jti"]

    with patch(_CONFIRM_EMAIL_PATCH, return_value=True) as mock_email:
        resp = pg_client.post(
            "/api/v1/account/deletion-request/tenant",
            json={"confirm": True},
            headers=_auth(token),
        )

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "grace"
    mock_email.assert_called_once()

    assert is_token_revoked(jti) is True

    db_session.refresh(admin)
    assert admin.deletion_status == DeletionStatus.GRACE


# ---------------------------------------------------------------------------
# Login gate — grace/pending users hard-blocked
# ---------------------------------------------------------------------------


def test_login_blocked_for_user_in_grace(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(
        db_session, tenant, role=UserRole.SUPPORT_AGENT, deletion_status=DeletionStatus.GRACE
    )
    db_session.commit()

    resp = pg_client.post("/api/v1/auth/login", json={"email": user.email, "password": _PASSWORD})

    assert resp.status_code == 403, resp.json()
    assert resp.json()["detail"]["code"] == "account_pending_deletion"


def test_login_blocked_for_user_pending_verification(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(
        db_session,
        tenant,
        role=UserRole.SUPPORT_AGENT,
        deletion_status=DeletionStatus.PENDING_VERIFICATION,
    )
    db_session.commit()

    resp = pg_client.post("/api/v1/auth/login", json={"email": user.email, "password": _PASSWORD})

    assert resp.status_code == 403, resp.json()
    assert resp.json()["detail"]["code"] == "account_pending_deletion"


def test_login_unaffected_for_normal_active_user(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()

    resp = pg_client.post("/api/v1/auth/login", json={"email": user.email, "password": _PASSWORD})

    assert resp.status_code == 200, resp.json()


def test_get_current_user_blocked_for_user_in_grace(pg_client, db_session):
    """A still-valid, non-blacklisted access token minted before grace started
    must still be rejected by `get_current_user` on the next request."""
    tenant = _make_tenant(db_session)
    user = _make_user(
        db_session, tenant, role=UserRole.SUPPORT_AGENT, deletion_status=DeletionStatus.ACTIVE
    )
    db_session.commit()
    token = _token_for(user)

    # Enter grace *after* minting the token — simulates a token issued before
    # deletion was requested that hasn't naturally expired yet.
    user.deletion_status = DeletionStatus.GRACE
    db_session.commit()

    resp = pg_client.get("/api/v1/auth/me", headers=_auth(token))

    assert resp.status_code == 403, resp.json()
    assert resp.json()["detail"]["code"] == "account_pending_deletion"


def test_get_current_user_unaffected_for_normal_active_user(pg_client, db_session):
    tenant = _make_tenant(db_session)
    user = _make_user(db_session, tenant, role=UserRole.SUPPORT_AGENT)
    db_session.commit()
    token = _token_for(user)

    resp = pg_client.get("/api/v1/auth/me", headers=_auth(token))

    assert resp.status_code == 200, resp.json()
