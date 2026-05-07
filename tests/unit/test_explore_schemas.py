from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.explore import (
    ConversationSource,
    PersonalBusinessesResponse,
    PersonalBusinessItem,
    PersonalBusinessRelationship,
    SourceMeta,
    TenantCategory,
)


class TestTenantCategory:
    def test_all_thirteen_values_are_defined(self):
        expected = {
            "retail_ecommerce",
            "software_saas",
            "food_beverage",
            "health_wellness",
            "beauty_personal_care",
            "education_coaching",
            "professional_services",
            "financial_insurance",
            "travel_hospitality",
            "real_estate",
            "media_arts_entertainment",
            "nonprofit",
            "other",
        }
        assert {c.value for c in TenantCategory} == expected

    def test_values_are_str_subclass(self):
        # Required so Pydantic + SQLAlchemy treat them as strings.
        assert isinstance(TenantCategory.RETAIL_ECOMMERCE, str)
        assert TenantCategory.RETAIL_ECOMMERCE == "retail_ecommerce"


class TestConversationSource:
    def test_all_six_values_are_defined(self):
        expected = {
            "widget",
            "explore_personal",
            "explore_browse",
            "explore_search",
            "direct",
            "unknown",
        }
        assert {s.value for s in ConversationSource} == expected

    def test_values_are_str_subclass(self):
        # Required so Pydantic + DB column comparisons work as strings.
        assert isinstance(ConversationSource.WIDGET, str)
        assert ConversationSource.WIDGET == "widget"


class TestSourceMeta:
    def test_accepts_documented_keys(self):
        meta = SourceMeta(
            category_filter="retail_ecommerce",
            search_query_present=True,
            position=4,
            page=2,
        )
        assert meta.position == 4

    def test_unknown_keys_silently_dropped(self):
        # Pydantic v2 with extra="ignore" drops unknowns silently.
        meta = SourceMeta.model_validate({"position": 1, "totally_made_up": "x"})
        assert not hasattr(meta, "totally_made_up")

    def test_all_keys_optional(self):
        SourceMeta()  # must not raise

    def test_category_filter_validated_against_enum(self):
        with pytest.raises(ValidationError):
            SourceMeta(category_filter="not_a_real_category")


class TestPersonalBusinessModels:
    def test_relationship_minimal_construction(self):
        # Only required fields — defaults must let nullable fields be omitted.
        r = PersonalBusinessRelationship(
            is_followed=False,
            last_activity_at=datetime.now(UTC),
        )
        assert r.followed_at is None
        assert r.last_chat_at is None

    def test_relationship_requires_is_followed_and_last_activity(self):
        with pytest.raises(ValidationError):
            PersonalBusinessRelationship()  # missing both required fields

    def test_personal_business_item_minimal_construction(self):
        # Only the required fields populated; nullable fields default to None.
        item = PersonalBusinessItem(
            public_slug="test-co",
            display_name="Test Co",
            is_featured=False,
            created_at=datetime.now(UTC),
            relationship=PersonalBusinessRelationship(
                is_followed=True,
                last_activity_at=datetime.now(UTC),
            ),
        )
        assert item.description is None
        assert item.category is None
        assert item.relationship.is_followed is True

    def test_response_envelope_with_empty_list(self):
        resp = PersonalBusinessesResponse(businesses=[])
        assert resp.businesses == []

    def test_response_envelope_default_empty_list(self):
        # Should be possible to construct without explicitly passing businesses=[].
        resp = PersonalBusinessesResponse()
        assert resp.businesses == []
