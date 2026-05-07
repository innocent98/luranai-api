"""Integration tests for GET /api/v1/customer/explore/personal-businesses."""

import uuid
from datetime import UTC, datetime

from app.core.security import create_access_token
from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant


def _create_customer(db) -> CustomerUser:
    customer = CustomerUser(
        full_name="Test Customer",
        email=f"test+{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _customer_headers(customer: CustomerUser) -> dict[str, str]:
    token = create_access_token(
        subject=str(customer.id),
        additional_claims={"auth_type": "customer"},
    )
    return {"Authorization": f"Bearer {token}"}


def _make_tenant(db, name: str, listed: bool = True) -> Tenant:
    t = Tenant(
        name=f"{name}_{uuid.uuid4().hex[:8]}",
        display_name=name,
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=listed,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_personal_businesses_unauthenticated_returns_401(client):
    resp = client.get("/api/v1/customer/explore/personal-businesses")
    assert resp.status_code == 401


def test_personal_businesses_returns_empty_for_new_customer(db, client):
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    resp = client.get("/api/v1/customer/explore/personal-businesses", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"businesses": []}


def test_personal_businesses_returns_followed_tenant(db, client):
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    t = _make_tenant(db, "X")

    f = CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id)
    db.add(f)
    db.commit()

    resp = client.get("/api/v1/customer/explore/personal-businesses", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["businesses"]) == 1
    assert payload["businesses"][0]["display_name"] == "X"
    assert payload["businesses"][0]["relationship"]["is_followed"] is True


def test_personal_businesses_caps_at_eight(db, client):
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    for i in range(12):
        t = _make_tenant(db, f"T{i:02d}")
        db.add(CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id))
    db.commit()

    resp = client.get("/api/v1/customer/explore/personal-businesses", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["businesses"]) == 8


def test_response_includes_relationship_block(db, client):
    customer = _create_customer(db)
    headers = _customer_headers(customer)
    t = _make_tenant(db, "X")
    db.add(CustomerBusinessFollow(customer_user_id=customer.id, tenant_id=t.id))
    db.commit()

    resp = client.get("/api/v1/customer/explore/personal-businesses", headers=headers)
    item = resp.json()["businesses"][0]
    assert "relationship" in item
    assert "is_followed" in item["relationship"]
    assert "last_activity_at" in item["relationship"]
