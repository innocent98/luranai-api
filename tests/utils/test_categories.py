"""Unit tests for app.utils.categories helpers."""

from app.schemas.explore import TenantCategory
from app.utils.categories import coerce_tenant_category


class TestCoerceTenantCategory:
    def test_none_returns_none(self):
        assert coerce_tenant_category(None) is None

    def test_known_value_returns_enum(self):
        result = coerce_tenant_category("retail_ecommerce")
        assert result is TenantCategory.RETAIL_ECOMMERCE

    def test_unknown_value_returns_none(self):
        # Unknown categories should drop silently, not raise.
        assert coerce_tenant_category("totally_made_up") is None

    def test_empty_string_returns_none(self):
        assert coerce_tenant_category("") is None

    def test_all_enum_values_round_trip(self):
        for member in TenantCategory:
            assert coerce_tenant_category(member.value) is member
