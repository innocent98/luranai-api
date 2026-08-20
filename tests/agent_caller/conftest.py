"""Fixtures for the Agent Caller API tests."""

import uuid

import pytest

from app.core.security import create_access_token
from app.db.models.tenant import Tenant, TenantStatus
from app.db.models.user import AvailabilityStatus, User, UserRole


@pytest.fixture
def agent_tenant(db):
    tenant = Tenant(
        name=f"acme-{uuid.uuid4().hex[:6]}",
        display_name="Acme Insurance",
        status=TenantStatus.ACTIVE,
        api_key_hash=f"hash-{uuid.uuid4().hex}",
        is_publicly_listed=False,
        subscription_status="active",
        subscription_tier="growth",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_user(db, tenant, email, role, availability=AvailabilityStatus.AVAILABLE):
    user = User(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0].title(),
        role=role,
        tenant_id=tenant.id,
        is_active=True,
        availability_status=availability,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def agent_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "sam@acme.com", UserRole.SUPPORT_AGENT)


@pytest.fixture
def admin_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "boss@acme.com", UserRole.TENANT_ADMIN)


@pytest.fixture
def viewer_user(db, agent_tenant):
    return _make_user(db, agent_tenant, "view@acme.com", UserRole.VIEWER)


def _token_for(user):
    return create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )


@pytest.fixture
def auth_headers(agent_user):
    """Authorization header for the support agent (mobile app sends the header,
    not the cookie — deps.extract_token falls back to it)."""
    return {"Authorization": f"Bearer {_token_for(agent_user)}"}
