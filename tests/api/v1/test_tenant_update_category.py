"""Integration tests for category field on PUT /api/v1/tenant."""

import uuid

import pytest

from app.core.security import create_access_token
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole


def _create_tenant(db) -> Tenant:
    t = Tenant(
        name=f"cat_test_{uuid.uuid4().hex[:8]}",
        display_name="Category Test Tenant",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"cat-test-{uuid.uuid4().hex[:8]}",
        is_publicly_listed=False,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _create_tenant_admin(db, tenant_id) -> User:
    user = User(
        tenant_id=tenant_id,
        email=f"admin+{uuid.uuid4().hex[:8]}@example.com",
        full_name="Tenant Admin",
        role=UserRole.TENANT_ADMIN,
        hashed_password="hashed",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _tenant_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        additional_claims={
            "email": user.email,
            "role": user.role.value,
            "tenant_id": str(user.tenant_id),
        },
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def authed_tenant_operator(db):
    tenant = _create_tenant(db)
    operator = _create_tenant_admin(db, tenant.id)
    return operator, _tenant_headers(operator)


def test_patch_tenant_accepts_category(authed_tenant_operator, client):
    """Setting category on the tenant via PUT succeeds and persists."""
    operator, headers = authed_tenant_operator
    resp = client.put(
        "/api/v1/tenant",
        json={"category": "retail_ecommerce"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["category"] == "retail_ecommerce"


def test_patch_tenant_rejects_invalid_category(authed_tenant_operator, client):
    """Bad enum value returns 422."""
    operator, headers = authed_tenant_operator
    resp = client.put(
        "/api/v1/tenant",
        json={"category": "not_a_real_category"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_patch_tenant_clears_category_on_null(authed_tenant_operator, client):
    """Setting category=null clears the previously-set value."""
    operator, headers = authed_tenant_operator
    # First set
    set_resp = client.put(
        "/api/v1/tenant",
        json={"category": "software_saas"},
        headers=headers,
    )
    assert set_resp.status_code == 200
    # Then clear
    clear_resp = client.put(
        "/api/v1/tenant",
        json={"category": None},
        headers=headers,
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["category"] is None
