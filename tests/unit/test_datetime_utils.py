from datetime import UTC, datetime

from app.utils.datetime import ensure_utc_aware


def test_ensure_utc_aware_with_naive_datetime() -> None:
    naive = datetime(2026, 2, 17, 12, 0, 0)
    normalized = ensure_utc_aware(naive)
    assert normalized is not None
    assert normalized.tzinfo == UTC
    assert normalized.hour == 12


def test_ensure_utc_aware_with_aware_datetime() -> None:
    aware = datetime(2026, 2, 17, 12, 0, 0, tzinfo=UTC)
    normalized = ensure_utc_aware(aware)
    assert normalized == aware


def test_ensure_utc_aware_with_none() -> None:
    assert ensure_utc_aware(None) is None
