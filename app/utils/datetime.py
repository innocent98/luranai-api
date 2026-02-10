"""Datetime helpers for consistent UTC handling."""

from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    """
    Return current UTC time as naive datetime.

    We keep naive UTC for backward compatibility with existing DB columns and
    comparisons that currently expect naive timestamps.
    """
    return datetime.now(UTC).replace(tzinfo=None)
