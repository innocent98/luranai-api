"""API tests for GET /api/v1/public/businesses — category filter."""

import uuid

import pytest

from app.db.models.tenant import Tenant


@pytest.fixture
def seeded_tenants(db):
    tenants = [
        Tenant(
            name=f"A_{uuid.uuid4().hex[:8]}",
            display_name="A",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=f"a-{uuid.uuid4().hex[:6]}",
            is_publicly_listed=True,
            is_featured=False,
            category="retail_ecommerce",
        ),
        Tenant(
            name=f"B_{uuid.uuid4().hex[:8]}",
            display_name="B",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=f"b-{uuid.uuid4().hex[:6]}",
            is_publicly_listed=True,
            is_featured=False,
            category="software_saas",
        ),
        Tenant(
            name=f"C_{uuid.uuid4().hex[:8]}",
            display_name="C",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=f"c-{uuid.uuid4().hex[:6]}",
            is_publicly_listed=True,
            is_featured=False,
            category="retail_ecommerce",
        ),
    ]
    db.add_all(tenants)
    db.commit()
    return tenants


def test_browse_filters_by_category(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses?category=retail_ecommerce")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 2
    assert {b["display_name"] for b in payload["businesses"]} == {"A", "C"}


def test_browse_invalid_category_returns_422(client):
    resp = client.get("/api/v1/public/businesses?category=not_a_real_category")
    assert resp.status_code == 422


def test_browse_no_category_returns_all_listed(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses")
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


def test_browse_response_has_category_field(client, seeded_tenants):
    resp = client.get("/api/v1/public/businesses")
    items = resp.json()["businesses"]
    cats = {item["category"] for item in items}
    assert cats == {"retail_ecommerce", "software_saas"}
