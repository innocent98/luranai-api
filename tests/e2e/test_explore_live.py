"""Live E2E coverage for the customer-facing explore surface.

Public browse endpoints run unauthenticated. The personal-businesses endpoint
requires a customer bearer token; tests that need one will skip if
LURAN_E2E_CUSTOMER_EMAIL/PASSWORD are not configured.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok


pytestmark = pytest.mark.e2e


@pytest.mark.smoke
def test_public_browse_returns_directory_shape(anonymous_client):
    payload = assert_ok(anonymous_client.get("/public/businesses"))

    assert "businesses" in payload
    assert "total" in payload
    assert "page" in payload
    assert "page_size" in payload
    assert "total_pages" in payload
    assert payload["page"] == 1


def test_public_browse_rejects_invalid_category_with_422(anonymous_client):
    response = anonymous_client.get(
        "/public/businesses", params={"category": "not-a-real-category"}
    )
    assert response.status_code == 422, response.text


def test_public_browse_accepts_valid_category(anonymous_client):
    payload = assert_ok(
        anonymous_client.get("/public/businesses", params={"category": "software_saas"})
    )
    assert "businesses" in payload
    for item in payload["businesses"]:
        # Coerce-on-unknown contract: category is either the requested enum or omitted.
        if item.get("category") is not None:
            assert item["category"] == "software_saas"


def test_public_browse_paginates_within_bounds(anonymous_client):
    payload = assert_ok(
        anonymous_client.get("/public/businesses", params={"page": 1, "page_size": 5})
    )
    assert payload["page"] == 1
    assert payload["page_size"] == 5
    assert len(payload["businesses"]) <= 5


def test_personal_businesses_requires_auth(anonymous_client):
    response = anonymous_client.get("/customer/explore/personal-businesses")
    assert response.status_code == 401, response.text


def test_personal_businesses_returns_well_formed_response(customer_client):
    response = customer_client.get("/customer/explore/personal-businesses")
    payload = assert_ok(response)

    assert "businesses" in payload
    assert isinstance(payload["businesses"], list)
    assert len(payload["businesses"]) <= 8

    cache_control = response.headers.get("cache-control", "")
    assert "private" in cache_control
    assert "max-age=120" in cache_control
    assert "stale-while-revalidate=300" in cache_control

    for item in payload["businesses"]:
        assert "public_slug" in item
        assert "display_name" in item
        assert "is_featured" in item
        assert "created_at" in item
        assert "relationship" in item
        rel = item["relationship"]
        assert "is_followed" in rel
        assert "last_activity_at" in rel
        if item.get("category") is not None:
            assert isinstance(item["category"], str)


def test_operator_token_is_rejected_for_customer_endpoint(pro_client):
    """An operator (tenant-side) bearer token must NOT authorize the customer endpoint."""
    response = pro_client.get("/customer/explore/personal-businesses")
    assert response.status_code in (401, 403), response.text
