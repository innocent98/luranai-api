"""Offerings plan limits: resolver, usage meter, GET /tenant/entitlements, downgrade guard."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingImage,
    OfferingKind,
    OfferingPriceMode,
    OfferingStatus,
)
from app.services.entitlements import FEATURES, get_entitlements
from app.services.subscription_validation_service import SubscriptionValidationService
from tests.entitlements.helpers import TIERS, caps_of, make_tenant, make_user

OFFERINGS = {"trial": 25, "lite": 100, "professional": 500, "growth": 2000, "enterprise": None}
PHOTOS = {"trial": 4, "lite": 4, "professional": 8, "growth": 8, "enterprise": 8}


def _items(db, tenant, n, *, status=OfferingStatus.ACTIVE, start=1):
    for i in range(start, start + n):
        db.add(
            Offering(
                tenant_id=tenant.id,
                seq=i,
                kind=OfferingKind.PRODUCT,
                name=f"Item {i}",
                price_mode=OfferingPriceMode.FIXED,
                price=1000,
                availability=OfferingAvailability.IN_STOCK,
                status=status,
            )
        )
    db.commit()


def _photos(db, tenant, item, n):
    for pos in range(n):
        db.add(
            OfferingImage(
                offering_id=item.id,
                tenant_id=tenant.id,
                url=f"https://res.cloudinary.com/x/{uuid4().hex}.jpg",
                cloudinary_public_id=uuid4().hex,
                position=pos,
            )
        )
    db.commit()


@pytest.mark.parametrize("tier", TIERS)
def test_every_plan_includes_offerings_with_its_cap_and_photo_limit(db_session, tier):
    ent = get_entitlements(make_tenant(db_session, tier), db_session)
    assert ent.enabled("offerings") is True
    assert ent.limit("offerings") == OFFERINGS[tier]
    assert ent.value("max_offering_images") == PHOTOS[tier]


def test_the_feature_is_declared_with_its_limit_column():
    feature = FEATURES["offerings"]
    assert feature.limit == "max_offerings" and feature.flag is None


def test_usage_counts_active_items_only_and_only_this_tenants(db_session):
    tenant, other = make_tenant(db_session, "lite"), make_tenant(db_session, "lite")
    _items(db_session, tenant, 3)
    _items(db_session, tenant, 2, status=OfferingStatus.ARCHIVED, start=10)
    _items(db_session, other, 4)
    assert get_entitlements(tenant, db_session).usage("offerings") == 3


def test_a_lapsed_tenant_has_the_feature_off(db_session):
    tenant = make_tenant(db_session, "lite")
    tenant.subscription_status = "active"
    tenant.subscription_ends_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    assert get_entitlements(tenant, db_session).enabled("offerings") is False


def test_an_admin_override_of_the_cap_survives(db_session):
    tenant = make_tenant(db_session, "lite")
    caps = caps_of(db_session, tenant)
    caps.max_offerings = 7
    caps.override_flags = {"max_offerings": True}
    db_session.commit()
    assert get_entitlements(tenant, db_session).limit("offerings") == 7


def test_entitlements_endpoint_reports_limit_and_used(db_session, pg_client):
    tenant = make_tenant(db_session, "trial")
    _items(db_session, tenant, 2)
    _, headers = make_user(db_session, tenant)
    r = pg_client.get("/api/v1/tenant/entitlements", headers=headers)
    assert r.status_code == 200, r.text
    entry = r.json()["features"]["offerings"]
    assert entry["enabled"] is True
    assert entry["limit"] == 25 and entry["used"] == 2
    assert entry["min_tier"] == "trial"


# --- downgrade guard ------------------------------------------------------------------------


def _violation(db, tenant, tier, field):
    result = SubscriptionValidationService(db).validate_tier_change(tenant.id, tier)
    return next((v for v in result.violations if v.field == field), None), result


def test_downgrade_below_the_active_item_count_is_blocked(db_session):
    tenant = make_tenant(db_session, "lite")
    _items(db_session, tenant, 30)
    violation, result = _violation(db_session, tenant, "trial", "offerings")
    assert result.valid is False
    assert (violation.current_value, violation.allowed_value, violation.difference) == (30, 25, 5)
    assert "Archive or delete 5 items" in violation.action_required


def test_archived_items_do_not_block_a_downgrade(db_session):
    tenant = make_tenant(db_session, "lite")
    _items(db_session, tenant, 20)
    _items(db_session, tenant, 40, status=OfferingStatus.ARCHIVED, start=100)
    violation, _ = _violation(db_session, tenant, "trial", "offerings")
    assert violation is None


def test_unlimited_target_plan_never_blocks(db_session):
    tenant = make_tenant(db_session, "growth")
    _items(db_session, tenant, 150)
    violation, _ = _violation(db_session, tenant, "enterprise", "offerings")
    assert violation is None


def test_exactly_at_the_new_cap_is_allowed(db_session):
    tenant = make_tenant(db_session, "lite")
    _items(db_session, tenant, 25)
    violation, _ = _violation(db_session, tenant, "trial", "offerings")
    assert violation is None


def test_downgrade_with_more_photos_on_an_item_than_the_new_limit_is_blocked(db_session):
    tenant = make_tenant(db_session, "professional")
    _items(db_session, tenant, 2)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id, seq=1).one()
    _photos(db_session, tenant, item, 6)
    violation, result = _violation(db_session, tenant, "lite", "offering_images")
    assert result.valid is False
    assert (violation.current_value, violation.allowed_value, violation.difference) == (6, 4, 2)
    assert "Remove 2 photos" in violation.action_required


def test_photo_check_passes_when_every_item_fits(db_session):
    tenant = make_tenant(db_session, "professional")
    _items(db_session, tenant, 1)
    item = db_session.query(Offering).filter_by(tenant_id=tenant.id).one()
    _photos(db_session, tenant, item, 4)
    violation, _ = _violation(db_session, tenant, "lite", "offering_images")
    assert violation is None
