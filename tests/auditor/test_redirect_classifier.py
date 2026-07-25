import pytest

from app.auditor.discovery import classify_redirect


@pytest.mark.parametrize(
    "entered, final, expected",
    [
        ("https://luranai.com", "https://www.luranai.com/", "same_site"),
        ("https://www.x.com", "https://x.com/", "same_site"),
        ("http://x.com", "https://x.com/", "same_site"),
        ("https://x.com", "https://x.com/en", "same_site"),   # path only, host equal
        ("https://x.com", "https://app.x.com/", "different_host"),
        ("https://x.com", "https://y.com/", "different_host"),
        ("https://x.com", "https://another.example.com/", "different_host"),
    ],
)
def test_classify_redirect(entered, final, expected):
    assert classify_redirect(entered, final) == expected
