"""Unit tests for app.utils.attribution helpers."""
from app.utils.attribution import coerce_source, normalize_source_meta


class TestCoerceSource:
    def test_none_returns_unknown(self):
        assert coerce_source(None) == "unknown"

    def test_known_value_returns_same_string(self):
        assert coerce_source("explore_browse") == "explore_browse"

    def test_unknown_value_coerces_to_unknown(self):
        assert coerce_source("totally_made_up") == "unknown"

    def test_empty_string_coerces_to_unknown(self):
        # Empty string isn't a valid enum member
        assert coerce_source("") == "unknown"


class TestNormalizeSourceMeta:
    def test_none_returns_none(self):
        assert normalize_source_meta(None) is None

    def test_dict_with_extra_keys_drops_them(self):
        result = normalize_source_meta({"position": 2, "totally_made_up": "drop_me"})
        assert result is not None
        assert result["position"] == 2
        assert "totally_made_up" not in result

    def test_dict_only_known_keys_preserved(self):
        result = normalize_source_meta({"position": 1, "page": 3, "category_filter": "retail_ecommerce"})
        assert result is not None
        assert result["position"] == 1
        assert result["page"] == 3
        assert result["category_filter"] == "retail_ecommerce"

    def test_sourcemeta_instance_passes_through(self):
        from app.schemas.explore import SourceMeta
        meta = SourceMeta(position=5)
        result = normalize_source_meta(meta)
        assert result is not None
        assert result["position"] == 5
