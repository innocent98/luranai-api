"""Tests for VisitorService.list_public_businesses sort + category filter (T6)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models.tenant import Tenant, TenantStatus
from app.schemas.explore import TenantCategory
from app.services.visitor_service import VisitorService


def _seed_tenants(db, fixtures):
    """fixtures: list of (display_name, is_featured, category, days_old) or
    (display_name, is_featured, category, days_old, is_publicly_listed)."""
    now = datetime.now(UTC)
    created = []
    for row in fixtures:
        name, featured, category, days, *rest = row
        is_publicly_listed = rest[0] if rest else True
        slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
        t = Tenant(
            name=f"{name}_{uuid.uuid4().hex[:8]}",
            display_name=name,
            api_key_hash=f"hash_{uuid.uuid4().hex}",
            public_slug=slug,
            is_publicly_listed=is_publicly_listed,
            is_featured=featured,
            category=category,
            description=f"{name} description",
            support_headline=f"{name} headline",
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

    def test_private_tenant_appears_with_redacted_fields(self, db):
        # Public + private both with slug. Private should appear with is_private=True
        # but no description / category / headline / is_featured.
        _seed_tenants(
            db,
            [
                ("PublicCo", True, "retail_ecommerce", 5, True),
                ("PrivateCo", True, "software_saas", 5, False),
            ],
        )
        svc = VisitorService(db)
        resp = svc.list_public_businesses()

        items = {b.display_name: b for b in resp.businesses}
        assert {"PublicCo", "PrivateCo"} <= set(items)
        private = items["PrivateCo"]
        assert private.is_private is True
        assert private.description is None
        assert private.category is None
        assert private.support_headline is None
        # Featured is suppressed for private tenants — "featured" is a public
        # marketing concept that doesn't apply when descriptive fields are hidden.
        assert private.is_featured is False

    def test_public_tenants_order_before_private(self, db):
        # Reordering: even when a private tenant is featured + recent, public
        # tenants should win the top of the list. Verifies the new
        # `desc(Tenant.is_publicly_listed)` clause is the primary sort key.
        _seed_tenants(
            db,
            [
                ("PrivateFeatured", True, None, 1, False),
                ("PublicPlain", False, None, 60, True),
            ],
        )
        svc = VisitorService(db)
        resp = svc.list_public_businesses()
        names = [
            b.display_name
            for b in resp.businesses
            if b.display_name in {"PrivateFeatured", "PublicPlain"}
        ]
        assert names == ["PublicPlain", "PrivateFeatured"]

    def test_search_by_description_does_not_match_private(self, db):
        # A unique word in the description should match only public tenants —
        # private descriptions are never queried, to avoid soft-leaking
        # content the tenant chose to keep private.
        now = datetime.now(UTC)
        db.add_all(
            [
                Tenant(
                    name=f"PubMatch_{uuid.uuid4().hex[:8]}",
                    display_name="PublicMatcher",
                    api_key_hash=f"hash_{uuid.uuid4().hex}",
                    public_slug=f"pubmatch-{uuid.uuid4().hex[:6]}",
                    is_publicly_listed=True,
                    description="uniquetokenxyz appears here",
                    created_at=now - timedelta(days=5),
                ),
                Tenant(
                    name=f"PrivMatch_{uuid.uuid4().hex[:8]}",
                    display_name="PrivateMatcher",
                    api_key_hash=f"hash_{uuid.uuid4().hex}",
                    public_slug=f"privmatch-{uuid.uuid4().hex[:6]}",
                    is_publicly_listed=False,
                    description="uniquetokenxyz also here",
                    created_at=now - timedelta(days=5),
                ),
            ]
        )
        db.commit()

        svc = VisitorService(db)
        resp = svc.list_public_businesses(query="uniquetokenxyz")
        names = {b.display_name for b in resp.businesses}
        assert "PublicMatcher" in names
        assert "PrivateMatcher" not in names
