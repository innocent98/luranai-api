"""Tests for VisitorService.list_public_businesses sort + category filter (T6)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models.tenant import Tenant, TenantStatus
from app.schemas.explore import TenantCategory
from app.services.visitor_service import VisitorService


def _seed_tenants(db, fixtures):
    """fixtures: list of (display_name, is_featured, category, days_old)."""
    now = datetime.now(UTC)
    created = []
    for name, featured, category, days in fixtures:
        slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
        t = Tenant(
            name=f"{name}_{uuid.uuid4().hex[:8]}",
            display_name=name,
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=slug,
            is_publicly_listed=True,
            is_featured=featured,
            category=category,
            created_at=now - timedelta(days=days),
        )
        db.add(t)
        created.append(t)
    db.commit()
    return created


class TestListPublicBusinesses:
    def test_sort_order_featured_first_then_recent_then_alpha(self, db):
        # Insert 5 tenants:
        # Alpha:   not featured, old (60d)  → tier 4
        # Bravo:   not featured, new (5d)   → tier 3
        # Able:    featured,     old (60d)  → tier 2 (sorts before Charlie alphabetically)
        # Charlie: featured,     old (60d)  → tier 2
        # Delta:   featured,     new (5d)   → tier 1
        _seed_tenants(
            db,
            [
                ("Alpha", False, None, 60),
                ("Bravo", False, None, 5),
                ("Able", True, None, 60),
                ("Charlie", True, None, 60),
                ("Delta", True, None, 5),
            ],
        )

        svc = VisitorService(db)
        resp = svc.list_public_businesses(query=None, page=1, page_size=24, category=None)

        names = [b.display_name for b in resp.businesses]
        # Expected: Delta (featured+new), Able (featured+old, alpha-first),
        #           Charlie (featured+old), Bravo (unfeatured+new), Alpha (unfeatured+old)
        assert names == ["Delta", "Able", "Charlie", "Bravo", "Alpha"]

    def test_category_filter_returns_only_matching(self, db):
        _seed_tenants(
            db,
            [
                ("RetailCo", False, "retail_ecommerce", 5),
                ("SoftCo", False, "software_saas", 5),
                ("FoodCo", False, "food_beverage", 5),
            ],
        )
        svc = VisitorService(db)
        resp = svc.list_public_businesses(category="retail_ecommerce")
        assert [b.display_name for b in resp.businesses] == ["RetailCo"]

    def test_category_filter_combined_with_search(self, db):
        _seed_tenants(
            db,
            [
                ("Lagos Retail", False, "retail_ecommerce", 5),
                ("Abuja Retail", False, "retail_ecommerce", 5),
                ("Lagos Tech", False, "software_saas", 5),
            ],
        )
        svc = VisitorService(db)
        resp = svc.list_public_businesses(query="lagos", category="retail_ecommerce")
        assert {b.display_name for b in resp.businesses} == {"Lagos Retail"}

    def test_unlisted_tenants_never_appear(self, db):
        slug = f"listed-{uuid.uuid4().hex[:6]}"
        listed = Tenant(
            name=f"Listed_{uuid.uuid4().hex[:8]}",
            display_name="Listed",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=slug,
            is_publicly_listed=True,
        )
        db.add(listed)

        unlisted = Tenant(
            name=f"Hidden_{uuid.uuid4().hex[:8]}",
            display_name="Hidden",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            # no public_slug — also not publicly listed
            is_publicly_listed=False,
        )
        db.add(unlisted)
        db.commit()

        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        assert [b.display_name for b in resp.businesses] == ["Listed"]

    def test_inactive_status_tenants_never_appear(self, db):
        _seed_tenants(db, [("Active", False, None, 5)])

        inactive = Tenant(
            name=f"Inactive_{uuid.uuid4().hex[:8]}",
            display_name="Inactive",
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=f"inactive-{uuid.uuid4().hex[:6]}",
            is_publicly_listed=True,
            status=TenantStatus.SUSPENDED,
        )
        db.add(inactive)
        db.commit()

        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        assert [b.display_name for b in resp.businesses] == ["Active"]

    def test_response_includes_category_and_created_at(self, db):
        _seed_tenants(db, [("Item", True, "retail_ecommerce", 5)])
        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        item = resp.businesses[0]
        assert item.category == TenantCategory.RETAIL_ECOMMERCE
        assert item.created_at is not None

    def test_pagination_count_matches_listing(self, db):
        _seed_tenants(db, [(f"Co {i:02d}", False, None, 5) for i in range(15)])
        svc = VisitorService(db)
        page1 = svc.list_public_businesses(page=1, page_size=10)
        page2 = svc.list_public_businesses(page=2, page_size=10)
        assert page1.total == 15
        assert page1.total_pages == 2
        assert len(page1.businesses) == 10
        assert len(page2.businesses) == 5
