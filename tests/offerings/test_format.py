"""The one wording of a price, and the text cleaner every tenant-authored field passes through."""

import pytest

from app.services.offering_service import clean_text, format_price


@pytest.mark.parametrize(
    "mode,price,note,currency,expected",
    [
        ("fixed", 12000, None, "NGN", "₦12,000"),
        ("fixed", 1_250_000, None, "NGN", "₦1,250,000"),
        ("from", 5000, "per session", "NGN", "From ₦5,000 per session"),
        ("on_request", None, None, "NGN", "Price on request"),
        ("on_request", None, "per pair", "NGN", "Price on request"),
        ("fixed", 40, None, "USD", "$40"),
        ("fixed", 40, None, "XAF", "XAF 40"),
    ],
)
def test_format_price(mode, price, note, currency, expected):
    assert format_price(mode, price, note, currency) == expected


def test_clean_text_removes_controls_and_bidi_overrides():
    assert clean_text("a\x00b\x1bc") == "abc"
    assert clean_text("evil‮txt.exe") == "eviltxt.exe"
    assert clean_text("  padded  ") == "padded"
    assert clean_text("   ") is None and clean_text(None) is None


def test_newlines_survive_only_in_multiline_fields():
    assert clean_text("a\nb", multiline=True) == "a\nb"
    assert clean_text("a\nb\tc") == "a b c"
