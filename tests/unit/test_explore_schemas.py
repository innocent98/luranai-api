import pytest
from pydantic import ValidationError

from app.schemas.explore import (
    ConversationSource,
    PersonalBusinessItem,
    PersonalBusinessRelationship,
    PersonalBusinessesResponse,
    SourceMeta,
    TenantCategory,
)


class TestTenantCategory:
    def test_all_thirteen_values_are_defined(self):
        expected = {
            "retail_ecommerce", "software_saas", "food_beverage",
            "health_wellness", "beauty_personal_care", "education_coaching",
            "professional_services", "financial_insurance", "travel_hospitality",
            "real_estate", "media_arts_entertainment", "nonprofit", "other",
        }
        assert {c.value for c in TenantCategory} == expected

    def test_values_are_str_subclass(self):
        # Required so Pydantic + SQLAlchemy treat them as strings.
        assert isinstance(TenantCategory.RETAIL_ECOMMERCE, str)
        assert TenantCategory.RETAIL_ECOMMERCE == "retail_ecommerce"


class TestConversationSource:
    def test_all_six_values_are_defined(self):
        expected = {
            "widget", "explore_personal", "explore_browse",
            "explore_search", "direct", "unknown",
        }
        assert {s.value for s in ConversationSource} == expected


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
