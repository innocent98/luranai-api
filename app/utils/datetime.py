"""Datetime helpers for consistent UTC handling."""

from datetime import UTC, datetime


def utcnow_aware() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


def utcnow_naive() -> datetime:
    """
    Return current UTC time as naive datetime.

    We keep naive UTC for backward compatibility with existing DB columns and
    comparisons that currently expect naive timestamps.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def ensure_utc_aware(value: datetime | None) -> datetime | None:
    """
    Normalize a datetime to timezone-aware UTC.

    Treats naive datetimes as UTC for backward compatibility with legacy data.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
